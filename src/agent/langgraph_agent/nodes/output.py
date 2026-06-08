"""Output pipeline nodes.

response_formatter   Pure-Python sync node.  Assembles the final API response
                     dict, preserving the exact key schema expected by the UI.
save_to_memory       Async node.  Persists SQL, token usage, and execution
                     result back to the ConversationHistoryService.
observability_log    Pure-Python sync node.  Emits a structured JSON event so
                     any log aggregator (Azure Monitor, Grafana/Loki, etc.) can
                     parse it.  Grep for QUERY_EVENT to filter these lines.
"""

from __future__ import annotations

import decimal
import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)


def _coerce_decimals(rows: List[Dict]) -> List[Dict]:
    """Convert Decimal values to float so rows are JSON-serialisable.

    PostgreSQL returns ``decimal.Decimal`` for NUMERIC/DECIMAL columns.
    This causes ``json.dumps`` to raise ``TypeError`` when the history
    service tries to store the result preview.
    """
    out = []
    for row in rows:
        out.append(
            {
                k: float(v) if isinstance(v, decimal.Decimal) else v
                for k, v in row.items()
            }
        )
    return out


# ── response_formatter ────────────────────────────────────────────────────────


def response_formatter(state: AgentState) -> Dict[str, Any]:
    """Assemble the API response dict.

    Preserves the key contract the UI expects:
    question, query_id, session_id, sql, results, answer, prompt, error, metrics.

    Additionally attaches ``insights`` and ``follow_up`` when available.
    """
    route = state.get("route", "needs_query")
    eval_result = state.get("eval_result") or {}

    answer: Optional[str] = state.get("answer")

    # Override answer based on terminal state
    if state.get("clarification"):
        answer = state["clarification"]
    elif state.get("dlp_blocked"):
        answer = state.get("governance_error") or (
            "This query references governed data and has been blocked."
        )
    elif route == "unsafe":
        answer = "I can only execute read-only SELECT queries. I cannot modify data."
    elif route in ("out_of_scope", "greeting") and not answer:
        # greeting: answer is already set by fused_router; only use fallback if missing.
        # out_of_scope: always use the canned message.
        if route == "out_of_scope":
            display = state.get("connection_display_name") or "the database"
            answer = (
                f"I'm here to assist with data queries for {display}. "
                "This question appears to be outside my scope."
            )
    elif eval_result.get("summary"):
        answer = eval_result["summary"]
    elif state.get("is_trivial") and not answer:
        # Format single-value / small results without an extra LLM call.
        rows = (state.get("query_result") or {}).get("rows") or []
        if rows:
            row = rows[0]
            parts = []
            for col, val in row.items():
                label = col.replace("_", " ").title()
                if isinstance(val, (int, float)) and val == int(val):
                    parts.append(f"{label}: {int(val):,}")
                else:
                    parts.append(f"{label}: {val}")
            answer = " | ".join(parts) if parts else None
    elif not state.get("generated_sql") and not answer:
        # Only fall back to clarification / error_context when no answer has been set.
        # (from_memory route already populated `answer` via memory_answer_generator.)
        answer = state.get("clarification") or state.get("error_context")

    error = (
        state.get("error")
        or state.get("exec_error")
        or state.get("governance_error")
    )

    formatted: Dict[str, Any] = {
        "question": state.get("question", ""),
        "query_id": state.get("query_id"),
        "session_id": state.get("session_id"),
        "sql": state.get("generated_sql"),
        "results": state.get("query_result"),
        "answer": answer,
        "prompt": state.get("structured_prompt"),
        "error": error,
        "metrics": {
            "input_tokens": (state.get("token_usage") or {}).get("input_tokens"),
            "output_tokens": (state.get("token_usage") or {}).get("output_tokens"),
            "total_tokens": (state.get("token_usage") or {}).get("total_tokens"),
            "llm_latency_ms": state.get("llm_latency_ms"),
            "execution_time_ms": state.get("execution_time_ms"),
            "retry_count": state.get("retry_count", 0),
            "llm_call_count": state.get("llm_call_count", 0),
            "route": route,
        },
    }

    if eval_result.get("insights"):
        formatted["insights"] = eval_result["insights"]
    if eval_result.get("follow_up"):
        formatted["follow_up"] = eval_result["follow_up"]

    # ── Execution trace ───────────────────────────────────────────────────────────────────
    # NOTE: the trace is intentionally NOT attached here. response_formatter
    # runs before the tail nodes (save_to_memory, observability_log), so the
    # trace at this point is incomplete. The agent attaches the COMPLETE,
    # enriched trace from the final graph state once every node has executed
    # (see JeenInsightsAgent.process_question → _enrich_trace).

    # ── Node prompts ──────────────────────────────────────────────────────────────
    # Collected prompts for each LLM node, surfaced to the developer panel.
    node_prompts = state.get("node_prompts") or {}
    if node_prompts:
        formatted["node_prompts"] = node_prompts

    return {"formatted_response": formatted}


def _enrich_trace(events: list, state: "AgentState") -> None:  # type: ignore[name-defined]
    """Mutate each event in-place with human-readable detail from state."""
    result = state.get("query_result") or {}
    eval_result = state.get("eval_result") or {}
    node_prompts = state.get("node_prompts") or {}

    for ev in events:
        node = ev.get("node", "")

        if node == "memory_shrink_check":
            ev["detail"] = "over budget — summarising" if state.get("is_over_budget") else "within budget"

        elif node == "memory_summarizer":
            s = state.get("memory_summary") or ""
            ev["detail"] = s[:80] + ("…" if len(s) > 80 else "")

        elif node == "fused_router":
            route = state.get("route", "?")
            reason = state.get("route_reason", "")
            ev["detail"] = f"route = {route}" + (f" — {reason[:60]}" if reason else "")
            ev["route"] = route

        elif node == "memory_answer_generator":
            ans = state.get("answer")
            if isinstance(ans, list):
                # Fragment array — render as plain text for the trace detail line
                plain = "".join(f.get("t", "") for f in ans)
                ev["detail"] = plain[:80] + "\u2026" if len(plain) > 80 else plain
            else:
                ev["detail"] = ans[:80] + "\u2026" if (ans and len(ans) > 80) else (ans or "escape hatch \u2192 needs_query")

        elif node == "catalog_lookup":
            known = state.get("known_tables") or []
            src = state.get("catalog_source_used") or "db"
            src_label = "MCP" if src == "mcp" else "metadata DB"
            detail = f"{len(known)} tables · via {src_label}"
            cache = state.get("catalog_cache")
            if cache:
                detail += f" (cache {cache.upper()})"
            load_ms = state.get("catalog_load_ms")
            if isinstance(load_ms, int):
                detail += f" · {load_ms}ms"
            ev["detail"] = detail

        elif node == "prompt_builder":
            sp = state.get("structured_prompt") or {}
            conn = (sp.get("connection") or {}).get("display_name", "")
            ev["detail"] = f"system prompt built ({conn})"

        elif node == "sql_generator":
            sql = state.get("generated_sql")
            clarif = state.get("clarification")
            retry = state.get("retry_count", 0)
            prefix = f"retry #{retry} — " if retry else ""
            if sql:
                short = sql.replace("\n", " ")[:100]
                ev["detail"] = prefix + short + ("…" if len(sql) > 100 else "")
                ev["sql"] = sql
            elif clarif:
                ev["detail"] = prefix + f"clarification: {clarif[:80]}"
            else:
                ev["detail"] = prefix + "no SQL or clarification"
                ev["status"] = "warn"

        elif node == "sqlglot_validate":
            err = state.get("sqlglot_error")
            if err:
                ev["detail"] = err[:100]
                ev["status"] = "error"
            else:
                ev["detail"] = "SQL passed validation"

        elif node == "dlp_check":
            if state.get("dlp_blocked"):
                ev["detail"] = state.get("governance_error", "blocked")[:80]
                ev["status"] = "blocked"
            else:
                ev["detail"] = "passed governance check"

        elif node == "execute_query":
            err = state.get("exec_error")
            if err:
                ev["detail"] = err[:100]
                ev["status"] = "error"
            else:
                rc = result.get("row_count", len(result.get("rows") or []))
                cols = len(result.get("columns") or [])
                ev["detail"] = f"{rc} rows × {cols} cols"

        elif node == "trivial_result_check":
            is_t = state.get("is_trivial")
            ev["detail"] = "trivial — skipping eval" if is_t else "not trivial — running eval"

        elif node == "fused_eval_analytics":
            intent = eval_result.get("answers_intent", "?")
            summary = eval_result.get("summary", "")
            # summary may be a fragment list — flatten to plain text for the trace
            if isinstance(summary, list):
                summary = "".join(f.get("t", "") for f in summary)
            ev["detail"] = f"answers_intent={intent}" + (f" — {summary[:60]}" if summary else "")
            ev["answers_intent"] = intent

        elif node == "feedback_classifier":
            fb = state.get("feedback_type", "?")
            retry = state.get("retry_count", 0)
            ev["detail"] = f"type={fb}  retry={retry}"
            ev["feedback_type"] = fb
            if fb in ("syntax", "exec", "semantic", "missing_table"):
                ev["status"] = "retry"

        elif node == "response_formatter":
            route = state.get("route", "?")
            ev["detail"] = f"route={route}"

        elif node == "save_to_memory":
            qid = state.get("query_id")
            ev["detail"] = f"query_id={str(qid)[:8]}…" if qid else "skipped (no query_id)"

        elif node == "observability_log":
            ev["detail"] = "QUERY_EVENT logged"

        # Attach captured prompt for LLM nodes so the UI can show full text
        if node in node_prompts:
            ev["prompt"] = node_prompts[node]


# ── save_to_memory ────────────────────────────────────────────────────────────


def make_save_to_memory(history_service: ConversationHistoryService, deployment_name: str):
    """Return an async ``save_to_memory`` node."""

    async def save_to_memory(state: AgentState) -> Dict[str, Any]:
        query_id = state.get("query_id")
        if not query_id:
            return {}

        sql = state.get("generated_sql")
        exec_time_ms = state.get("execution_time_ms")
        query_result = state.get("query_result") or {}
        exec_error = state.get("exec_error")
        token_usage = state.get("token_usage") or {}
        llm_latency_ms = state.get("llm_latency_ms") or 0
        start_time = state.get("start_time")
        graph_time_ms = int((time.monotonic() - start_time) * 1000) if start_time else None

        try:
            if sql:
                await history_service.update_llm_response(
                    query_id=query_id,
                    generated_sql=sql,
                    llm_model=deployment_name,
                    llm_latency_ms=llm_latency_ms,
                    tokens_used=token_usage.get("total_tokens", 0),
                )

            if exec_error:
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="error",
                    execution_time_ms=exec_time_ms,
                    row_count=0,
                    result_preview=None,
                    error_message=exec_error,
                    graph_time_ms=graph_time_ms,
                )
            elif sql:
                rows = query_result.get("rows") or []
                # Coerce Decimal → float so the preview is JSON-serialisable.
                # PostgreSQL returns Decimal for NUMERIC/DECIMAL columns.
                safe_preview = _coerce_decimals(rows[:10]) if rows else None
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="success",
                    execution_time_ms=exec_time_ms,
                    row_count=len(rows),
                    result_preview=safe_preview,
                    error_message=None,
                    graph_time_ms=graph_time_ms,
                )

        except Exception:  # noqa: BLE001
            logger.exception(
                "save_to_memory: failed to update history for query_id=%s", query_id
            )

        return {}

    return save_to_memory


# ── observability_log ─────────────────────────────────────────────────────────


def observability_log(state: AgentState) -> Dict[str, Any]:
    """Emit a structured JSON event at the end of every graph run.

    Log line format::

        QUERY_EVENT {"event": "query_completed", "route": "needs_query", ...}

    Parsing examples::

        # Live tail in the container:
        docker logs jeen-insights-api -f | grep QUERY_EVENT

        # Parse with jq:
        docker logs jeen-insights-api | grep QUERY_EVENT | sed 's/.*QUERY_EVENT //' | jq .
    """
    start = state.get("start_time") or time.monotonic()
    elapsed_ms = int((time.monotonic() - start) * 1000)
    has_error = bool(state.get("error") or state.get("exec_error"))

    event = {
        "event": "query_completed",
        "source_key": state.get("source_key"),
        "route": state.get("route", "?"),
        "retry_count": state.get("retry_count", 0),
        "llm_call_count": state.get("llm_call_count", 0),
        "total_tokens": (state.get("token_usage") or {}).get("total_tokens", 0),
        "input_tokens": (state.get("token_usage") or {}).get("input_tokens", 0),
        "llm_latency_ms": state.get("llm_latency_ms", 0),
        "execution_time_ms": state.get("execution_time_ms"),
        "elapsed_ms": elapsed_ms,
        "has_error": has_error,
        "query_id": str(state.get("query_id") or ""),
    }

    # Human-readable summary + machine-parseable JSON in one line.
    # Use default=str to handle Decimal and other non-serialisable types
    # that Postgres occasionally returns (e.g. Decimal('1234.56')).
    logger.info(
        "QUERY_EVENT %s",
        json.dumps(event, default=str),
    )
    return {}
