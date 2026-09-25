"""SQL execution nodes.

execute_query         Async node.  Runs the SQL via SqlRunner and records
                      execution time.  The runner already enforces read-only safety.
trivial_result_check  Sync node.  Flags single-value / small result sets so the
                      expensive eval/analytics LLM call is skipped.
empty_result_check    Async node.  Diagnoses a genuinely empty (0-row) result: a
                      pure-Python heuristic gates a single fast LLM call that may
                      request one SQL regeneration and produces a likely-cause hint.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.progress import emit_partial
from src.agent.token_usage import merge_usage
from src.connectors import SqlRunner

logger = logging.getLogger(__name__)

# Results with at most this many rows AND columns are considered trivial.
_TRIVIAL_MAX_ROWS = 1
_TRIVIAL_MAX_COLS = 5


# ── execute_query ─────────────────────────────────────────────────────────────


def make_execute_query(sql_runner: SqlRunner):
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
    """Pure-Python node: set ``is_trivial=True`` to skip fused_eval_analytics.

    This is also the earliest point where the rows are accepted on every path
    (SQL after the empty/filter checks, memory compute), so it hands them to
    the request's ``partial_callback`` before the narration LLM call. ML
    results are excluded: their chart and persistence depend on the artifact
    that ``save_to_memory`` writes later.
    """
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
    out: Dict[str, Any] = {"is_trivial": trivial}
    if rows and not state.get("analysis_result") and callable(state.get("partial_callback")):
        revision = int(state.get("partial_revision") or 0)
        emit_partial(
            state,
            {
                "query_id": state.get("query_id"),
                "session_id": state.get("session_id"),
                "sql": state.get("generated_sql"),
                "results": result,
                "revision": revision,
                "provisional": True,
            },
        )
        out["partial_revision"] = revision + 1
    return out


# ── empty_result_check ─────────────────────────────────────────────────────────

# One diagnosis pass per request keeps the graph bounded and terminating.
_EMPTY_RECHECK_MAX = 1


def _is_suspicious_empty(sql: str, state: AgentState) -> bool:
    """Heuristic pre-gate: is a 0-row result unexpected for this query?

    Cheap (pure Python, no LLM). Suspicious only when the user did not ask to
    restrict the data AND the SQL shape can silently drop rows that exist —
    i.e. an aggregate / ``GROUP BY`` or an ``INNER`` join to a dimension. A
    query the user explicitly filtered ("sales in Antarctica", a future date
    range) can legitimately be empty, so it is left alone.
    """
    # An explicit user-intended filter makes emptiness plausible: skip the LLM.
    if state.get("resolved_filters") or state.get("unresolved_filters"):
        return False

    try:
        import sqlglot
        from sqlglot import expressions as exp

        expr = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:  # noqa: BLE001 — parsing is best-effort; fail safe
        return False
    if expr is None:
        return False

    has_group = expr.find(exp.Group) is not None
    has_agg = expr.find(exp.AggFunc) is not None
    # A join without an explicit LEFT/RIGHT/FULL side is INNER (or CROSS): it can
    # drop fact rows whose dimension key is NULL/absent.
    has_inner_join = any(
        not (j.args.get("side") or "").strip()
        for j in expr.find_all(exp.Join)
    )
    return bool(has_group or has_agg or has_inner_join)


def _column_stats_for(state: AgentState) -> str:
    """Catalog column statistics (null ratios) available before execution."""
    structured = state.get("structured_prompt") or {}
    stats = structured.get("column_statistics")
    if stats:
        return str(stats)
    bundle = state.get("metadata_bundle") or {}
    return str(bundle.get("column_statistics") or "")


def parse_empty_diagnosis(content: str) -> Optional[Dict[str, Any]]:
    """Extract the ``{plausible, reason, hint}`` object from the LLM reply."""
    if not content:
        return None
    text = content.strip()
    # Tolerate a fenced or prose-wrapped object by slicing the outermost braces.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


_EMPTY_RECHECK_FALLBACK = (
    "The query returned no rows, which is unexpected for this question. "
    "Re-examine the JOINs and WHERE filters — an INNER JOIN on a nullable "
    "dimension or an over-restrictive filter can drop rows that exist. Prefer "
    "LEFT JOIN and surface NULL/empty groups explicitly, then regenerate the "
    "query."
)


def make_empty_result_check(
    router_llm: LangChainLlmService,
    prompt_loader: PromptLoader,
    *,
    enabled: bool = True,
):
    """Return an async ``empty_result_check`` node.

    Runs only on a genuinely empty (0-row) result. A pure-Python heuristic
    decides whether the emptiness is suspicious; if so, a single fast LLM call
    judges plausibility. When the model says the empty result does not answer
    the question, ``needs_sql_recheck`` is set so the feedback classifier routes
    one regeneration. Either way the counter is spent so the pass runs at most
    once and the graph terminates.
    """

    async def empty_result_check(state: AgentState) -> Dict[str, Any]:
        if not enabled:
            return {}
        result = state.get("query_result") or {}
        rows = result.get("rows") or []
        if rows:
            return {}  # not empty — routing normally prevents this
        if state.get("needs_filter_reground"):
            return {}  # filter re-grounding owns this empty result
        if int(state.get("empty_result_diagnostics") or 0) >= _EMPTY_RECHECK_MAX:
            return {}
        sql = state.get("generated_sql") or ""
        if not sql or not _is_suspicious_empty(sql, state):
            return {}

        prompt = await prompt_loader.arender(
            "empty_result_diagnosis",
            question=state.get("question", ""),
            sql=sql,
            column_statistics=_column_stats_for(state) or "(none available)",
        )
        # Spending the budget here (even on error) guarantees the pass is
        # attempted at most once regardless of the LLM outcome.
        updates: Dict[str, Any] = {
            "empty_result_diagnostics": int(state.get("empty_result_diagnostics") or 0) + 1,
            "node_prompts": {**(state.get("node_prompts") or {}), "empty_result_check": prompt},
        }
        try:
            t0 = time.monotonic()
            response = await router_llm.generate(
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=400,
                timeout=state.get("llm_timeout_seconds"),
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
        except Exception:  # noqa: BLE001 — never fail the answer over the gate
            logger.warning("empty_result_check: LLM gate failed", exc_info=True)
            return updates

        updates["llm_call_count"] = (state.get("llm_call_count") or 0) + 1
        updates["llm_latency_ms"] = (state.get("llm_latency_ms") or 0) + latency_ms
        updates["token_usage"] = merge_usage(
            state.get("token_usage") or {}, response.get("usage") or {}
        )

        parsed = parse_empty_diagnosis(response.get("content") or "")
        reason = (parsed or {}).get("reason") if parsed else None
        hint = (parsed or {}).get("hint") if parsed else None
        if hint or reason:
            updates["empty_hint"] = str(hint or reason)

        if parsed is not None and parsed.get("plausible") is False:
            updates["needs_sql_recheck"] = True
            updates["empty_recheck_context"] = str(reason or _EMPTY_RECHECK_FALLBACK)
            logger.info("empty_result_check: implausible empty result — requesting SQL recheck")
        else:
            logger.info("empty_result_check: empty result accepted as plausible")
        return updates

    return empty_result_check
