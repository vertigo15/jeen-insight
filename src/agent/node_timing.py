"""Shared node instrumentation for the SQL and DAX LangGraph agents.

``timed(name, fn)`` wraps a node so that it

* announces ``node_started`` / ``node_finished`` / ``node_failed`` to the
  request's progress callback, and
* returns, next to the node's own updates, one trace event with the node's
  elapsed time and token usage. The ``trace`` field has an ``operator.add``
  reducer, so those single-event lists concatenate into the full history.

Both graphs register every node through this wrapper; keeping one copy means a
change to the trace contract cannot drift between them.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, Tuple

from src.agent.langgraph_agent.nodes.output import stamp_customer_table
from src.agent.progress import emit_progress
from src.agent.token_usage import usage_delta

# (icon, type)  type is one of: llm | db | logic | ml
NODE_META: Dict[str, Tuple[str, str]] = {
    # Shared by both graphs.
    "context_composer":          ("🧠", "logic"),
    "fused_router":              ("🔀", "llm"),
    "capability_answer":         ("💡", "llm"),
    "catalog_help_answer":       ("📖", "logic"),
    "memory_answer_generator":   ("💬", "llm"),
    "history_search":            ("🗂", "db"),
    "trivial_result_check":      ("⚡", "logic"),
    "fused_eval_analytics":      ("📊", "llm"),
    "response_formatter":        ("📋", "logic"),
    "save_to_memory":            ("💾", "db"),
    "observability_log":         ("🪵", "logic"),
    # Text-to-SQL.
    "catalog_lookup":            ("📦", "db"),
    "filter_planner":            ("🎯", "llm"),
    "filter_grounder":           ("🔎", "db"),
    "prior_data_binder":         ("🧷", "llm"),
    "prompt_builder":            ("🔧", "logic"),
    "sql_generator":             ("🧠", "llm"),
    "sql_repair":                ("🩹", "llm"),
    "sqlglot_validate":          ("✅", "logic"),
    "dlp_check":                 ("🛡", "logic"),
    "execute_query":             ("▶", "db"),
    "empty_filter_result_check": ("🧭", "logic"),
    "empty_result_check":        ("🩺", "llm"),
    "feedback_classifier":       ("🔁", "logic"),
    # ML skills branch.
    "analysis_planner":          ("🧪", "llm"),
    "analysis_guard":            ("🚧", "db"),
    "analysis_sql":              ("🔧", "logic"),
    "analysis_run":              ("🧮", "ml"),
    # Text-to-DAX.
    "dax_catalog_lookup":        ("📦", "db"),
    "dax_query_planner":         ("🗺", "llm"),
    "dax_entity_resolver":       ("🔍", "db"),
    "dax_prompt_builder":        ("🔧", "logic"),
    "dax_generator":             ("🧠", "llm"),
    "dax_static_validate":       ("✅", "logic"),
    "dax_repair":                ("🩹", "llm"),
    "pbi_execute_query":         ("▶", "db"),
    "result_integrity_check":    ("🔎", "logic"),
    "dax_feedback_router":       ("🔁", "logic"),
}


def timed(
    name: str,
    fn: Callable[..., Any],
    meta: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Callable[..., Any]:
    """Wrap a graph node with progress events and a timing/usage trace event."""
    icon, ntype = (meta or NODE_META).get(name, ("●", "logic"))

    def _start(state: Any) -> float:
        emit_progress(state, node=name, status="node_started", icon=icon, node_type=ntype)
        return time.monotonic()

    def _failed(state: Any, t0: float, exc: BaseException) -> None:
        emit_progress(
            state,
            node=name,
            status="node_failed",
            icon=icon,
            node_type=ntype,
            elapsed_ms=round((time.monotonic() - t0) * 1000),
            error=str(exc),
        )

    def _finish(state: Any, t0: float, result: Any) -> Any:
        elapsed = round((time.monotonic() - t0) * 1000)
        emit_progress(
            state,
            node=name,
            status="node_finished",
            icon=icon,
            node_type=ntype,
            elapsed_ms=elapsed,
        )
        return stamp_customer_table(result, {
            "node": name, "elapsed_ms": elapsed, "icon": icon, "type": ntype,
            **usage_delta(state, {} if result is None else result),
        })

    if asyncio.iscoroutinefunction(fn):
        async def _async_wrapper(state):
            t0 = _start(state)
            try:
                result = await fn(state)
            except Exception as exc:
                _failed(state, t0, exc)
                raise
            return _finish(state, t0, result)
        return _async_wrapper

    def _sync_wrapper(state):
        t0 = _start(state)
        try:
            result = fn(state)
        except Exception as exc:
            _failed(state, t0, exc)
            raise
        return _finish(state, t0, result)
    return _sync_wrapper


__all__ = ["NODE_META", "timed"]
