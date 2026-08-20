"""Text-to-SQL query + table/schema introspection endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

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
    """Compatibility JSON query endpoint."""

    agent, user_id, user_context = await _prepare_query(request, principal)
    try:
        return await _execute_query(
            request=request,
            principal=principal,
            agent=agent,
            user_id=user_id,
            user_context=user_context,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error processing question")
        raise HTTPException(status_code=500, detail=str(e)) from e


async def _prepare_query(
    request: QueryRequest, principal: Principal
) -> tuple[Any, str, dict[str, Any]]:
    """Validate ownership and resolve the request's agent and trusted identity."""

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
    user_context = dict(request.user_context or {})
    user_context["user_id"] = user_id
    return agent, user_id, user_context


async def _execute_query(
    *,
    request: QueryRequest,
    principal: Principal,
    agent: Any,
    user_id: str,
    user_context: dict[str, Any],
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    result_callback: Optional[Callable[[QueryResponse], None]] = None,
) -> QueryResponse:
    """Run one query with the same guards and post-processing for JSON/SSE."""

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
            user_context=user_context,
            limit=request.limit,
            temperature=request.temperature,
            eval_analytics=request.eval_analytics,
            llm_timeout=request.llm_timeout,
            progress_callback=progress_callback,
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
        if result_callback is not None:
            try:
                # Surface the completed query before optional connector snapshot
                # and tool-planning work. Those best-effort enrichments can be
                # slow and must not hold the dataset out of the workspace.
                result_callback(QueryResponse(**result))
            except Exception:  # noqa: BLE001
                logger.debug("early query result callback failed", exc_info=True)
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
    finally:
        await query_limiter.release(user_id)


def _sse(event: str, data: Any) -> str:
    payload = json.dumps(data, default=str, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/query/stream")
async def query_database_stream(
    body: QueryRequest,
    http_request: Request,
    principal: Principal = Depends(get_principal),
):
    """Stream real LangGraph node progress followed by the normal final result."""

    agent, user_id, user_context = await _prepare_query(body, principal)
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def on_progress(event: dict[str, Any]) -> None:
        events.put_nowait({"event": "node", "data": event})

    def on_result_ready(result: QueryResponse) -> None:
        events.put_nowait(
            {"event": "result", "data": result.model_dump(mode="json")}
        )

    async def event_stream():
        result_sent = False
        task = asyncio.create_task(
            _execute_query(
                request=body,
                principal=principal,
                agent=agent,
                user_id=user_id,
                user_context=user_context,
                progress_callback=on_progress,
                result_callback=on_result_ready,
            )
        )
        try:
            yield _sse("open", {"status": "connected"})
            while True:
                if await http_request.is_disconnected():
                    break
                if task.done() and events.empty():
                    try:
                        result = await task
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Streaming query failed")
                        detail = (
                            exc.detail
                            if isinstance(exc, HTTPException)
                            else str(exc)
                        )
                        yield _sse("error", {"detail": detail})
                    else:
                        payload = result.model_dump(mode="json")
                        if not result_sent:
                            yield _sse("result", payload)
                        else:
                            enrichment = {
                                key: payload.get(key)
                                for key in ("result_handle", "tool_proposal")
                                if payload.get(key) is not None
                            }
                            if enrichment:
                                yield _sse("enrichment", enrichment)
                    break

                event_wait = asyncio.create_task(events.get())
                try:
                    done, _ = await asyncio.wait(
                        {event_wait, task},
                        timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if event_wait in done:
                        queued = event_wait.result()
                        event_name = queued.get("event", "node")
                        yield _sse(event_name, queued.get("data", {}))
                        if event_name == "result":
                            result_sent = True
                    elif task not in done:
                        yield ": heartbeat\n\n"
                    # When the query task wins, loop immediately instead of
                    # waiting for a heartbeat. The top of the loop drains any
                    # final queued node event before emitting the result.
                finally:
                    if not event_wait.done():
                        event_wait.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await event_wait
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
