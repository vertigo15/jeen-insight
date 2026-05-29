"""Insights evaluation subgraph  (graph.py).

Builds a compiled LangGraph ``StateGraph`` containing the ``eval`` node.
The full text-to-SQL pipeline (memory → router → sql_gen → execution →
validation → eval → output) will be wired here incrementally; today only
the eval node is active so the graph can be used immediately for the
post-execution analytics phase.

Exports
-------
build_insights_eval_graph(llm_service, prompt_cache)
    Compile and return the graph.  Call once at startup and store the
    result on ``src.api.state.insights_eval_graph``.

run_eval(graph, *, question, sql, results, row_count)
    Thin async helper that invokes the graph and returns the final state.
    The caller only needs to await this; all LangGraph plumbing is hidden.
"""
from __future__ import annotations

import logging
from typing import Any, List

from langgraph.graph import StateGraph, END

from .state import InsightsState
from .nodes.eval import make_fused_eval_analytics

logger = logging.getLogger(__name__)


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_insights_eval_graph(llm_service: Any, prompt_cache: Any):
    """Compile and return the insights eval ``StateGraph``.

    Parameters
    ----------
    llm_service:
        Shared ``LangChainLlmService`` instance.
    prompt_cache:
        Shared ``PromptCache`` instance (used by the eval node to load the
        ``fused_eval_analytics`` template).

    Returns
    -------
    CompiledStateGraph
        A LangGraph compiled graph ready for ``await graph.ainvoke(…)``.
    """
    eval_node = make_fused_eval_analytics(llm_service, prompt_cache)

    g: StateGraph = StateGraph(InsightsState)
    g.add_node("eval", eval_node)
    g.set_entry_point("eval")
    g.add_edge("eval", END)

    compiled = g.compile()
    logger.info("insights_eval_graph: compiled successfully (nodes: eval)")
    return compiled


# ── Convenience helper ─────────────────────────────────────────────────────────

async def run_eval(
    graph,
    *,
    question: str,
    sql: str,
    results: List[Any],
    row_count: int,
) -> dict:
    """Invoke the eval graph and return the final state.

    Parameters
    ----------
    graph:
        Compiled graph returned by :func:`build_insights_eval_graph`.
    question:
        Original user question.
    sql:
        SQL that was executed (pass empty string if unavailable).
    results:
        Raw query rows as a list of dicts (``[{col: val, ...}, ...]``).
    row_count:
        Total number of rows (may differ from ``len(results)`` if rows were
        capped by a limit).

    Returns
    -------
    dict
        Final ``InsightsState`` after the eval node ran.  Keys of interest:
        ``answers_intent``, ``summary``, ``insights``,
        ``follow_up_questions``, ``error``.
    """
    initial_state: InsightsState = {
        "question":  question,
        "sql":       sql,
        "results":   results,
        "row_count": row_count,
    }
    return await graph.ainvoke(initial_state)
