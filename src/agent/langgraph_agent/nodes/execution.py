"""SQL execution nodes.

execute_query         Async node.  Runs the SQL via PostgresSqlRunner and records
                      execution time.  The runner already enforces read-only safety.
trivial_result_check  Sync node.  Flags single-value / small result sets so the
                      expensive eval/analytics LLM call is skipped.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from src.agent.langgraph_agent.state import AgentState
from src.tools.sql_tool import PostgresSqlRunner

logger = logging.getLogger(__name__)

# Results with at most this many rows AND columns are considered trivial.
_TRIVIAL_MAX_ROWS = 1
_TRIVIAL_MAX_COLS = 5


# ── execute_query ─────────────────────────────────────────────────────────────


def make_execute_query(sql_runner: PostgresSqlRunner):
    """Return an async ``execute_query`` node."""

    async def execute_query(state: AgentState) -> Dict[str, Any]:
        sql = state.get("generated_sql") or ""
        limit = state.get("limit") or 100
        max_rows = state.get("max_result_rows") or 10000
        statement_timeout_ms = state.get("statement_timeout_ms")
        if statement_timeout_ms is None:
            statement_timeout_ms = 30000

        logger.info(
            "execute_query: running SQL (limit=%d, max_rows=%d, timeout_ms=%d)",
            limit, max_rows, statement_timeout_ms,
        )
        t0 = time.monotonic()
        result = await sql_runner.run_sql(
            sql,
            limit=limit,
            max_rows=max_rows,
            statement_timeout_ms=statement_timeout_ms,
        )
        exec_time_ms = int((time.monotonic() - t0) * 1000)

        if "error" in result:
            error_msg = result["error"]
            logger.warning("execute_query: error in %dms — %s", exec_time_ms, error_msg)
            return {
                "query_result": result,
                "exec_error": error_msg,
                # Cumulative across all SQL runs in this request (incl. retries),
                # mirroring how llm_latency_ms sums every LLM call.
                "execution_time_ms": (state.get("execution_time_ms") or 0) + exec_time_ms,
                "error_context": f"SQL execution error: {error_msg}",
            }

        row_count = len(result.get("rows") or [])
        logger.info("execute_query: %d row(s) in %dms", row_count, exec_time_ms)
        return {
            "query_result": result,
            "exec_error": None,
            # Cumulative across all SQL runs in this request (incl. retries).
            "execution_time_ms": (state.get("execution_time_ms") or 0) + exec_time_ms,
            "error_context": None,
        }

    return execute_query


# ── trivial_result_check ──────────────────────────────────────────────────────


def trivial_result_check(state: AgentState) -> Dict[str, Any]:
    """Pure-Python node: set ``is_trivial=True`` to skip fused_eval_analytics."""
    result = state.get("query_result") or {}
    rows = result.get("rows") or []
    cols = result.get("columns") or []
    trivial = len(rows) <= _TRIVIAL_MAX_ROWS and len(cols) <= _TRIVIAL_MAX_COLS
    logger.info(
        "trivial_result_check: rows=%d cols=%d → is_trivial=%s",
        len(rows),
        len(cols),
        trivial,
    )
    return {"is_trivial": trivial}
