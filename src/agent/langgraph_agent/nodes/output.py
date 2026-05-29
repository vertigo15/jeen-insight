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

import json
import logging
import time
from typing import Any, Dict, Optional

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)


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

    return {"formatted_response": formatted}


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
                )
            elif sql:
                rows = query_result.get("rows") or []
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="success",
                    execution_time_ms=exec_time_ms,
                    row_count=len(rows),
                    result_preview=rows[:10] if rows else None,
                    error_message=None,
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

    # Human-readable summary + machine-parseable JSON in one line
    logger.info(
        "QUERY_EVENT %s",
        json.dumps(event, default=str),
    )
    return {}
