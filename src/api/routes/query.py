"""Text-to-SQL query + table/schema introspection endpoints."""

from __future__ import annotations

import logging

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
    try:
        result = await agent.process_question(
            question=request.question,
            session_id=request.session_id,
            user_context=request.user_context or {},
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
