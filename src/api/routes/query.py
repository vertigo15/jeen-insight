"""Text-to-SQL query + table/schema introspection endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import (
    get_catalog_provider,
    get_history_service,
    get_principal,
    resolve_agent,
)
from src.api.concurrency import ConcurrencyLimitExceeded, query_limiter
from src.api.models import QueryRequest, QueryResponse
from src.api.result_cache import result_cache
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query_database(
    request: QueryRequest,
    principal: Principal = Depends(get_principal),
):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    # Identity comes ONLY from the verified Principal — never from the request
    # body — so a durable result snapshot can never be owned by a spoofed user.
    user_id = principal.user_id
    if request.session_id:
        history = get_history_service()
        if not await history.session_belongs_to_user(
            session_id=request.session_id, user_id=user_id
        ):
            raise HTTPException(status_code=404, detail="Session not found for this user")
    agent = await resolve_agent(request.connection)

    # Cost governor: cap concurrent queries per user (prevents one user from
    # pinning the LLM / exhausting the DB pool). No-op when disabled.
    try:
        await query_limiter.acquire(user_id)
    except ConcurrencyLimitExceeded:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent queries. Please wait for the current one to finish.",
        )
    # Trusted identity propagation: the agent's user context is derived from the
    # verified Principal, never from the request body. A client cannot spoof a
    # user_id to read another user's history/cache or own a result snapshot.
    user_context = dict(request.user_context or {})
    user_context["user_id"] = user_id
    try:
        result = await agent.process_question(
            question=request.question,
            session_id=request.session_id,
            user_context=user_context,
            limit=request.limit,
            temperature=request.temperature,
            eval_analytics=request.eval_analytics,
            llm_timeout=request.llm_timeout,
        )
        # Cache the result so charts / describe / insights can reuse the full
        # rows (keyed by user+connection+query_id) instead of the browser
        # re-uploading them. Best-effort: never fail the query over a cache hiccup.
        try:
            result_cache.put(
                user_id=user_id,
                connection=request.connection,
                query_id=result.get("query_id"),
                dataset=result.get("results"),
            )
        except Exception:  # noqa: BLE001
            logger.debug("result_cache put failed", exc_info=True)
        # When the connector platform is enabled, persist a durable, encrypted
        # snapshot of the SERVER-produced result. Its id is the opaque handle used
        # to authorize outbound actions (never browser-submitted rows).
        await _maybe_snapshot(
            result, principal=principal, connection=request.connection
        )
        # Post-result agent tool planner (Phase 3): may propose exactly ONE
        # email-this-result action when agent tools are enabled and the user's
        # question expressed explicit intent. Best-effort — never fails the query.
        try:
            proposal = await _maybe_propose_tool(result, principal=principal)
            if proposal:
                result["tool_proposal"] = proposal
        except Exception:  # noqa: BLE001
            logger.debug("tool planner failed", exc_info=True)
        return QueryResponse(**result)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error processing question")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        await query_limiter.release(user_id)


async def _maybe_snapshot(result: dict, *, principal: Principal, connection: str) -> None:
    """Best-effort durable snapshot of a result when connectors are enabled.

    Ownership is bound to the verified Principal (user_id, and the canonical Entra
    identity_id when available) so only that principal can later authorize an
    export/action against this snapshot.
    """
    try:
        from src.security.app_flags import get_connectors_enabled

        if not await get_connectors_enabled():
            return
        results = result.get("results")
        if not isinstance(results, dict) or not (results.get("rows") or results.get("data")):
            return
        from src.api import state

        if state.snapshot_service is None:
            return
        from src.config import settings

        # Bind the canonical identity when the caller is an Entra principal
        # (best-effort: tenant-mismatch or non-SSO callers simply get user-only
        # ownership, which the action gate still enforces).
        identity_id = None
        if principal.is_entra:
            try:
                from src.api.dependencies import ensure_identity

                identity = await ensure_identity(principal)
                identity_id = identity["id"]
            except Exception:  # noqa: BLE001
                identity_id = None

        snap = await state.snapshot_service.create_snapshot(
            owner_user_id=principal.user_id,
            identity_id=identity_id,
            connection=connection,
            query_id=str(result.get("query_id")) if result.get("query_id") else None,
            sql=result.get("sql"),
            results=results,
            ttl_seconds=settings.CONNECTOR_SNAPSHOT_TTL_SECONDS,
        )
        if snap:
            result["result_handle"] = snap["id"]
    except Exception:  # noqa: BLE001 - snapshots are best-effort
        logger.debug("snapshot create failed", exc_info=True)


async def _maybe_propose_tool(result: dict, *, principal: Principal) -> Optional[dict]:
    """Best-effort post-result tool proposal (see src.agent.tool_planner).

    Returns a proposal dict for the UI confirm card, or None. At most ONE proposal
    per turn, tried in order: email → Slack → Jira (snapshot-bound writes), then a
    read (web_search) when the user explicitly asked to search the web. Each path
    independently requires the connector platform + agent tools enabled, an Entra
    principal, explicit intent, entitlement, and (for writes) a connected grant —
    otherwise silently None.
    """
    from src.api import state as api_state

    gate = api_state.action_gate
    registry = api_state.registry_service
    grants = api_state.grant_service
    identities = api_state.identity_service
    if not (gate and registry and grants and identities):
        return None
    from src.agent.tool_planner import (
        plan_jira_action,
        plan_post_result_action,
        plan_read_action,
        plan_slack_action,
    )
    from src.api.dependencies import ensure_identity

    # Snapshot-bound OAuth writes (each fires only on its own explicit intent).
    for planner in (plan_post_result_action, plan_slack_action, plan_jira_action):
        proposal = await planner(
            result=result,
            principal=principal,
            registry=registry,
            grants=grants,
            identities=identities,
            gate=gate,
            ensure_identity=ensure_identity,
        )
        if proposal is not None:
            return proposal

    # Read tool (api_key, no snapshot / no grant).
    return await plan_read_action(
        result=result,
        principal=principal,
        registry=registry,
        identities=identities,
        gate=gate,
        ensure_identity=ensure_identity,
    )


@router.get("/tables")
async def list_tables(
    connection: str = Query(..., description="source_key of the active connection"),
):
    agent = await resolve_agent(connection)
    try:
        tables = await agent.sql_runner.list_tables()
        return {"tables": tables}
    except Exception as e:  # noqa: BLE001
        logger.exception("Error listing tables")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tables-rich")
async def list_tables_rich(
    connection: str = Query(..., description="source_key of the active connection"),
):
    """Return all catalogued tables with descriptions and column counts.

    Sourced from the active catalog provider (MCP when ``catalog_source=mcp``,
    otherwise the metadata DB). Each entry includes ``name``, ``description``
    (nullable), and ``col_count``.
    """
    # Resolve the provider outside the try so its 503 (e.g. loader not yet
    # initialised) propagates intact instead of being masked as a 500.
    provider = await get_catalog_provider(connection)
    try:
        tables = await provider.load_tables_rich(connection)
        return {"tables": tables}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error listing rich tables for connection %r", connection)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/schema/{table_name}")
async def get_table_schema(
    table_name: str,
    connection: str = Query(..., description="source_key of the active connection"),
):
    agent = await resolve_agent(connection)
    try:
        schema = await agent.sql_runner.get_table_schema(table_name)
        if not schema:
            raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")
        return {"table": table_name, "schema": schema}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error getting schema")
        raise HTTPException(status_code=500, detail=str(e)) from e
