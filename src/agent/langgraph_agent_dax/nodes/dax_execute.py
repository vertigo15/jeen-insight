"""Power BI DAX execution + result-integrity nodes.

pbi_execute_query      Async node. Resolves the signed-in user's request-scoped
                       delegated token, runs the DAX via ``PowerBiDaxClient``, and
                       handles TRANSPORT failures inline (401 → refresh once and
                       replay; 429 → honor Retry-After; 5xx → bounded backoff).
                       Non-transport DAX errors are surfaced for the feedback
                       router. The token never enters graph state.
result_integrity_check Sync node. Annotates empty/partial results and decides
                       whether a one-shot empty-result diagnostic is warranted.

Semantic errors and DAX errors go to ``dax_feedback_router``; a valid (even
empty) result flows to the shared ``trivial_result_check``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.config import settings
from src.connectors.powerbi import PowerBiDaxClient
from src.connectors.powerbi_token import (
    PowerBiTokenError,
    PowerBiTokenProvider,
    provider_from_app_state,
)

logger = logging.getLogger(__name__)

# Inline transport-retry budget (401 refresh / 429 / 5xx). Semantic 400s never
# retry here — they go to the feedback router for a plan-aware repair.
_MAX_TRANSPORT_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 8.0
# At most one empty-result semantic diagnostic (a valid empty result is success).
_MAX_EMPTY_DIAGNOSTICS = 1


def _token_provider() -> Optional[PowerBiTokenProvider]:
    """Build a token provider from the live connector services (or None)."""
    return provider_from_app_state()


def make_pbi_execute_query():
    """Return an async ``pbi_execute_query`` node."""

    async def pbi_execute_query(state: DaxAgentState) -> Dict[str, Any]:
        dax = state.get("generated_dax") or state.get("generated_sql") or ""
        workspace_id = state.get("workspace_id") or ""
        dataset_id = state.get("dataset_id") or ""
        max_rows = state.get("max_result_rows") or 10000
        limit = state.get("limit")
        if limit and limit > 0:
            max_rows = min(max_rows, limit)

        provider = _token_provider()
        if provider is None:
            return _needs_connect(
                state,
                "Power BI connectors are not configured on this deployment. Ask an "
                "admin to enable connectors and add the Power BI connector.",
                hard=True,
            )

        try:
            client = PowerBiDaxClient(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                api_base=settings.POWERBI_API_BASE,
                timeout=settings.POWERBI_EXECUTE_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            return _fatal(state, f"Power BI connection is misconfigured: {exc}")

        auth_user_id = state.get("user_id")
        t0 = time.monotonic()
        transport_attempts = int(state.get("transport_attempts") or 0)
        refreshed = False
        result: Dict[str, Any] = {}

        for attempt in range(_MAX_TRANSPORT_ATTEMPTS):
            try:
                token = await provider.get_token_for_auth_user(
                    auth_user_id, force_refresh=refreshed
                )
            except PowerBiTokenError as exc:
                return _needs_connect(state, str(exc), hard=not exc.needs_connect)
            access_token = token.access_token

            result = await client.execute_dax(dax, access_token, max_rows=max_rows)
            etype = result.get("error_type")

            if not result.get("error"):
                break  # success

            transport_attempts += 1
            if etype == "auth" and not refreshed:
                # 401 → refresh the token once and replay the exact DAX.
                logger.info("pbi_execute_query: 401 → refreshing token and replaying")
                refreshed = True
                continue
            if etype == "throttled":
                delay = min(result.get("retry_after") or 2.0, _MAX_BACKOFF_SECONDS)
                logger.info("pbi_execute_query: 429 → waiting %.1fs", delay)
                if attempt < _MAX_TRANSPORT_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
                    continue
            if etype in ("service", "transport"):
                delay = min(1.5 * (2 ** attempt), _MAX_BACKOFF_SECONDS)
                logger.info("pbi_execute_query: %s → backoff %.1fs", etype, delay)
                if attempt < _MAX_TRANSPORT_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
                    continue
            break  # non-transport error (400/403/limit/execution/partial)

        exec_ms = int((time.monotonic() - t0) * 1000)
        cumulative_ms = (state.get("execution_time_ms") or 0) + exec_ms

        if result.get("error"):
            msg = result.get("error")
            logger.warning("pbi_execute_query: error (%s) — %s", result.get("error_type"), msg)
            return {
                "query_result": result,
                "exec_error": msg,
                "execution_time_ms": cumulative_ms,
                "transport_attempts": transport_attempts,
                "http_status": result.get("http_status"),
                "pbi_error_code": result.get("pbi_error_code"),
                "pbi_error_message": result.get("pbi_error_message") or msg,
                "error_location": result.get("error_location"),
                "retry_after": result.get("retry_after"),
                "is_partial": bool(result.get("is_partial")),
                "error_context": f"Power BI DAX error: {msg}",
            }

        rows = result.get("rows") or []
        logger.info("pbi_execute_query: %d row(s) in %dms", len(rows), exec_ms)
        return {
            "query_result": {
                "columns": result.get("columns") or [],
                "rows": rows,
                "row_count": result.get("row_count", len(rows)),
            },
            "exec_error": None,
            "execution_time_ms": cumulative_ms,
            "transport_attempts": transport_attempts,
            "http_status": result.get("http_status", 200),
            "returned_row_count": result.get("row_count", len(rows)),
            "actual_schema": result.get("columns") or [],
            "is_partial": bool(result.get("is_partial")),
            "error_context": None,
        }

    return pbi_execute_query


def _needs_connect(state: DaxAgentState, message: str, *, hard: bool) -> Dict[str, Any]:
    """Surface a connect/reconnect prompt (not a retryable execution error)."""
    return {
        "needs_connect": not hard,
        "dax_terminal": True,
        "exec_error": None,
        "answer": message,
        "error": message,
        "dax_feedback_action": "exhausted",
        "query_result": {"columns": [], "rows": [], "row_count": 0},
    }


def _fatal(state: DaxAgentState, message: str) -> Dict[str, Any]:
    return {
        "dax_terminal": True,
        "exec_error": None,
        "answer": message,
        "error": message,
        "dax_feedback_action": "exhausted",
        "query_result": {"columns": [], "rows": [], "row_count": 0},
    }


# ── result_integrity_check ──────────────────────────────────────────────────────


def result_integrity_check(state: DaxAgentState) -> Dict[str, Any]:
    """Annotate empty/partial results; request one empty diagnostic when useful."""
    result = state.get("query_result") or {}
    rows = result.get("rows") or []
    is_empty = len(rows) == 0
    empty_diagnostics = int(state.get("empty_diagnostics") or 0)

    updates: Dict[str, Any] = {
        "is_empty": is_empty,
        "returned_row_count": result.get("row_count", len(rows)),
        "actual_schema": result.get("columns") or [],
        "integrity_action": None,
    }

    # A single empty-result diagnostic: an empty set is often correct (filters /
    # RLS), so we regenerate at most once, then accept the empty result.
    if is_empty and empty_diagnostics < _MAX_EMPTY_DIAGNOSTICS:
        updates["integrity_action"] = "empty_diagnostic"
        updates["empty_diagnostics"] = empty_diagnostics + 1
        updates["dax_error_category"] = "EMPTY_OR_BLANK"
        updates["error_context"] = (
            "The query returned zero rows. Verify the filters, measure and date "
            "role against the plan; if an empty result is genuinely correct for "
            "these filters, keep it."
        )
        logger.info("result_integrity_check: empty result → one diagnostic regeneration")
    else:
        logger.info(
            "result_integrity_check: %d row(s), partial=%s",
            len(rows), bool(state.get("is_partial")),
        )
    return updates


__all__ = ["make_pbi_execute_query", "result_integrity_check"]
