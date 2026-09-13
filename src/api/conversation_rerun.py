"""Re-execute a stored turn's query without the LLM.

Used by the hybrid restore path: when a turn's snapshot was never stored
(``too_large``), was dropped by retention (``pruned``) or predates the feature,
the browser asks for the rows to be produced again from the saved SQL/DAX.

Dispatch by source type:

* SQL connectors go through the existing protected ``SqlRunner.run_sql``
  (read-only validation, row cap, statement timeout).
* Power BI goes through ``PowerBiDaxClient.execute_dax`` under the caller's
  delegated grant, using the same token-provider factory the DAX agent uses.

Authorization order (mirrors the API contract): the caller must own the
conversation (404 otherwise), the connection must still exist and be active
(409), and for Power BI a current delegated grant is required (403).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict
from uuid import UUID

from fastapi import HTTPException

from src.agent.conversation_artifacts import (
    SNAPSHOT_STORED,
    build_result_snapshot,
)
from src.agent.conversation_history import ConversationHistoryService
from src.api import state
from src.api.dependencies import get_connection_service
from src.api.result_cache import result_cache
from src.config import settings
from src.connections import ConnectionNotFound

logger = logging.getLogger(__name__)

_UPSTREAM_FAILURE = "The data source could not run the stored query."
_UPSTREAM_TIMEOUT = "The stored query timed out. Try again or narrow the question."


async def rerun_turn(
    history: ConversationHistoryService,
    *,
    conversation_id: UUID,
    turn_id: UUID,
    user_id: str,
) -> Dict[str, Any]:
    """Return the fields of ``RerunTurnResponse`` or raise an HTTPException."""
    turn = await history.get_turn_for_rerun(
        conversation_id=conversation_id, turn_id=turn_id, user_id=user_id
    )
    if turn is None:
        raise HTTPException(status_code=404, detail="Conversation or turn not found")
    query_text = (turn.get("sql") or "").strip()
    if not query_text:
        raise HTTPException(status_code=409, detail="turn_has_no_query")

    source_key = turn["source_key"]
    connection_service = get_connection_service()
    started = time.monotonic()

    try:
        try:
            connection = await connection_service.get_connection(source_key)
        except ConnectionNotFound:
            raise HTTPException(status_code=409, detail="connection_unavailable")
        if not getattr(connection, "is_active", False):
            raise HTTPException(status_code=409, detail="connection_unavailable")

        from src.metadata.runtime_settings import get_runtime_settings

        runtime = await get_runtime_settings()

        if getattr(connection, "is_power_bi", False):
            result = await _run_dax(
                connection=connection,
                dax=query_text,
                user_id=user_id,
                max_rows=int(runtime.max_result_rows),
            )
        else:
            result = await _run_sql(
                connection_service=connection_service,
                source_key=source_key,
                sql=query_text,
                max_rows=int(runtime.max_result_rows),
                statement_timeout_ms=int(runtime.db_statement_timeout_ms),
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        # Connection lookup, runtime settings, driver or transport failures:
        # details stay in the server log; the browser only learns that the
        # upstream source failed.
        logger.exception("rerun_turn: upstream execution failed for turn %s", turn_id)
        raise HTTPException(status_code=502, detail=_UPSTREAM_FAILURE)
    execution_time_ms = int((time.monotonic() - started) * 1000)

    if result.get("error"):
        etype = str(result.get("error_type") or "")
        if etype in ("timeout", "query_timeout"):
            raise HTTPException(status_code=504, detail=_UPSTREAM_TIMEOUT)
        # run_sql / execute_dax already sanitise their messages for end users
        # (the same text is shown on a live failed query), so pass them through.
        raise HTTPException(status_code=502, detail=str(result["error"])[:500])

    snapshot, snapshot_status, size, _row_count = build_result_snapshot(
        result,
        max_rows=settings.CONVERSATION_SNAPSHOT_MAX_ROWS,
        max_bytes=settings.CONVERSATION_SNAPSHOT_MAX_BYTES,
    )
    if getattr(history, "persistence_enabled", False) is True:
        await history.store_rerun_snapshot(
            turn_id=turn_id,
            user_id=user_id,
            result_snapshot=snapshot,
            snapshot_status=snapshot_status,
            snapshot_bytes=size,
        )
    # The browser gets the rows regardless of whether they were persisted, so a
    # too-large result is still viewable; only the durable copy is skipped.
    envelope = snapshot if snapshot is not None else _envelope_for_browser(result)
    try:
        result_cache.put(
            user_id=user_id,
            connection=source_key,
            query_id=str(turn_id),
            dataset=envelope,
        )
    except Exception:  # noqa: BLE001
        logger.debug("result_cache put failed after rerun", exc_info=True)

    return {
        "turn_id": str(turn_id),
        "results": envelope,
        "chart_spec": None,
        "chart_config": None,
        "snapshot_status": snapshot_status,
        "snapshot_at": _now_iso() if snapshot_status == SNAPSHOT_STORED else None,
        "execution_time_ms": execution_time_ms,
    }


def _envelope_for_browser(result: Dict[str, Any]) -> Dict[str, Any]:
    from src.agent.conversation_artifacts import coerce_json_safe_rows, json_safe_value

    rows = result.get("rows")
    if rows is None:
        rows = result.get("data") or []
    envelope: Dict[str, Any] = {
        "columns": [str(c) for c in (result.get("columns") or [])],
        "rows": coerce_json_safe_rows(rows),
        "row_count": len(rows),
    }
    for key in ("truncated", "cap", "total", "total_row_count", "capped"):
        if key in result and result[key] is not None:
            envelope[key] = json_safe_value(result[key])
    return envelope


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _run_sql(
    *,
    connection_service: Any,
    source_key: str,
    sql: str,
    max_rows: int,
    statement_timeout_ms: int,
) -> Dict[str, Any]:
    try:
        runner = await connection_service.get_runner(source_key)
    except ConnectionNotFound:
        raise HTTPException(status_code=409, detail="connection_unavailable")
    except Exception:  # noqa: BLE001
        logger.exception("rerun_turn: runner unavailable for %s", source_key)
        raise HTTPException(status_code=502, detail=_UPSTREAM_FAILURE)
    return await runner.run_sql(
        sql,
        limit=None,
        max_rows=max_rows,
        statement_timeout_ms=statement_timeout_ms,
    )


async def _run_dax(
    *,
    connection: Any,
    dax: str,
    user_id: str,
    max_rows: int,
) -> Dict[str, Any]:
    from src.connectors.powerbi import PowerBiDaxClient
    from src.connectors.powerbi_token import PowerBiTokenError

    factory = state.powerbi_token_provider_factory
    provider = factory() if callable(factory) else None
    if provider is None:
        raise HTTPException(
            status_code=409,
            detail="Power BI connectors are not configured on this deployment.",
        )
    try:
        client = PowerBiDaxClient(
            workspace_id=connection.workspace_id or "",
            dataset_id=connection.dataset_id or "",
            api_base=settings.POWERBI_API_BASE,
            timeout=settings.POWERBI_EXECUTE_TIMEOUT_SECONDS,
        )
    except ValueError:
        logger.exception("rerun_turn: Power BI connection misconfigured")
        raise HTTPException(status_code=409, detail="connection_unavailable")

    try:
        token = await provider.get_token_for_auth_user(user_id, force_refresh=False)
    except PowerBiTokenError as exc:
        # A missing/expired delegated grant is the caller's to fix (reconnect).
        raise HTTPException(status_code=403, detail="grant_required") from exc

    result = await client.execute_dax(dax, token.access_token, max_rows=max_rows)
    if result.get("error") and result.get("error_type") == "auth":
        try:
            token = await provider.get_token_for_auth_user(user_id, force_refresh=True)
        except PowerBiTokenError as exc:
            raise HTTPException(status_code=403, detail="grant_required") from exc
        result = await client.execute_dax(dax, token.access_token, max_rows=max_rows)
    if result.get("error") and result.get("error_type") == "auth":
        raise HTTPException(status_code=403, detail="grant_required")
    return result
