"""LangGraph state-graph builder for the Jeen Insights text-to-SQL agent.

``build_graph`` is the single factory function called by ``JeenInsightsAgent.__init__``.
It wires all node factories with their service dependencies and returns a compiled
LangGraph ``CompiledStateGraph`` that can be invoked with ``await graph.ainvoke(state)``.

Graph topology (simplified):
    START
      └─ memory_shrink_check ──(over budget)──► memory_summarizer ─┐
                              └─(within)──────────────────────────┘
                                                                    │
                                                               fused_router
                                ┌──(from_memory)─────────────────► memory_answer_generator
                                │                                       │  (answer ready)
                                │                                       ▼
                                │                              response_formatter ◄──────┐
                                ├──(out_of_scope / unsafe) ──────────────────────────►  │
                                └──(needs_query) ──► catalog_lookup ──► prompt_builder   │
                                                            │                            │
                                                       sql_generator ◄──────────────────┤
                                                            │                    feedback│
                                                    ┌───────┴─────────┐     classifier  │
                                               (SQL)|                 |(clarif)         │
                                          sqlglot_validate        response_formatter    │
                                         ┌────┴────┐                                   │
                                    (valid)|      (error)                               │
                                       dlp_check   └──► feedback_classifier ───────────┘
                                      ┌──┴──┐
                                (safe)|   (blocked)
                               execute_query ──(error)──► feedback_classifier
                                     │ (rows)
                              trivial_result_check
                                  ┌───┴───┐
                            (yes) │       │ (no)
                       response_formatter  fused_eval_analytics ──(wrong)──► feedback_classifier
                                                │ (correct)
                                        response_formatter
                                                │
                                         save_to_memory
                                                │
                                       observability_log
                                                │
                                              END
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent.nodes.catalog import make_catalog_lookup, make_prompt_builder
from src.agent.langgraph_agent.nodes.eval import make_fused_eval_analytics
from src.agent.langgraph_agent.nodes.execution import make_execute_query, trivial_result_check
from src.agent.langgraph_agent.nodes.feedback import make_feedback_classifier
from src.agent.langgraph_agent.nodes.memory import (
    make_memory_shrink_check,
    make_memory_summarizer,
)
from src.agent.langgraph_agent.nodes.output import (
    make_save_to_memory,
    observability_log,
    response_formatter,
)
from src.agent.langgraph_agent.nodes.router import make_fused_router
from src.agent.langgraph_agent.nodes.sql_gen import (
    make_memory_answer_generator,
    make_sql_generator,
)
from src.agent.langgraph_agent.nodes.validation import make_dlp_check, make_sqlglot_validate
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import AzureOpenAILlmService
from src.metadata import MetadataLoader
from src.tools.sql_tool import PostgresSqlRunner

logger = logging.getLogger(__name__)


# ── Node metadata for the trace panel ────────────────────────────────────────
# (icon, type)  type is one of: llm | db | logic
_NODE_META: dict[str, tuple[str, str]] = {
    "memory_shrink_check":     ("🧠", "logic"),
    "memory_summarizer":       ("🤏", "llm"),
    "fused_router":            ("🔀", "llm"),
    "memory_answer_generator": ("💬", "llm"),
    "catalog_lookup":          ("📦", "db"),
    "prompt_builder":          ("🔧", "logic"),
    "sql_generator":           ("🧠", "llm"),
    "sqlglot_validate":        ("✅", "logic"),
    "dlp_check":               ("🛡", "logic"),
    "execute_query":           ("▶", "db"),
    "trivial_result_check":    ("⚡", "logic"),
    "fused_eval_analytics":    ("📊", "llm"),
    "feedback_classifier":     ("🔁", "logic"),
    "response_formatter":      ("📋", "logic"),
    "save_to_memory":          ("💾", "db"),
    "observability_log":       ("🪵", "logic"),
}


def _timed(name: str, fn: Any) -> Any:
    """Wrap a node function so it appends a timing event to ``AgentState.trace``.

    Each wrapped node returns ``{"trace": [event]}`` in addition to its own
    updates. LangGraph's ``operator.add`` reducer on the ``trace`` field
    concatenates these single-event lists into the full execution history.
    """
    icon, ntype = _NODE_META.get(name, ("●", "logic"))

    if asyncio.iscoroutinefunction(fn):
        async def _async_wrapper(state):
            t0 = time.monotonic()
            result = await fn(state)
            elapsed = round((time.monotonic() - t0) * 1000)
            out = {} if result is None else dict(result)
            out["trace"] = [{"node": name, "elapsed_ms": elapsed, "icon": icon, "type": ntype}]
            return out
        return _async_wrapper
    else:
        def _sync_wrapper(state):
            t0 = time.monotonic()
            result = fn(state)
            elapsed = round((time.monotonic() - t0) * 1000)
            out = {} if result is None else dict(result)
            out["trace"] = [{"node": name, "elapsed_ms": elapsed, "icon": icon, "type": ntype}]
            return out
        return _sync_wrapper


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_graph(
    *,
    llm: AzureOpenAILlmService,
    router_llm: AzureOpenAILlmService,
    sql_runner: PostgresSqlRunner,
    metadata_loader: MetadataLoader,
    history_service: ConversationHistoryService,
    prompt_loader: PromptLoader,
    deployment_name: str,
    max_retries: int = 3,
    max_history_tokens: int = 3000,
    dlp_enabled: bool = True,
    sqlglot_validation_enabled: bool = True,
    eval_analytics_enabled: bool = True,
) -> Any:
    """Build and compile the LangGraph text-to-SQL agent.

    Parameters
    ----------
    llm:
        Primary (large-model) LLM service — used for sql_generator and
        fused_eval_analytics.
    router_llm:
        Router/summarizer LLM service — used for fused_router, memory nodes,
        and memory_answer_generator.  May be the same object as ``llm`` when
        no separate cheaper deployment is configured.
    sql_runner:
        PostgreSQL query runner (already enforces read-only safety).
    metadata_loader:
        Loads and caches curated metadata from the metadata DB.
    history_service:
        Persists query + execution records for conversation context.
    prompt_loader:
        Template loader for all LLM prompt files.
    deployment_name:
        Azure OpenAI deployment name stored in the query history record.
    max_retries:
        Maximum number of SQL repair attempts before giving up.
    max_history_tokens:
        Estimated token budget for conversation history before summarisation.
    dlp_enabled:
        When True, DLP patterns are checked before executing any SQL.
    sqlglot_validation_enabled:
        When True, SQL is parsed and table names are checked before execution.

    Returns
    -------
    CompiledStateGraph
        A compiled LangGraph graph ready for ``await graph.ainvoke(state)``.
    """
    builder: StateGraph = StateGraph(AgentState)

    # ── Register nodes (all wrapped with _timed for execution trace) ────────
    def n(name, fn):  # shorthand: wrap + register
        builder.add_node(name, _timed(name, fn))

    n("memory_shrink_check",     make_memory_shrink_check(max_history_tokens))
    n("memory_summarizer",       make_memory_summarizer(router_llm, prompt_loader))
    n("fused_router",            make_fused_router(router_llm, prompt_loader))
    n("memory_answer_generator", make_memory_answer_generator(router_llm, prompt_loader))
    n("catalog_lookup",          make_catalog_lookup(metadata_loader))
    n("prompt_builder",          make_prompt_builder(prompt_loader))
    n("sql_generator",           make_sql_generator(llm, prompt_loader))
    n("sqlglot_validate",        make_sqlglot_validate(sqlglot_validation_enabled))
    n("dlp_check",               make_dlp_check(dlp_enabled))
    n("execute_query",           make_execute_query(sql_runner))
    n("trivial_result_check",    trivial_result_check)
    n("fused_eval_analytics",    make_fused_eval_analytics(llm, prompt_loader))
    n("feedback_classifier",     make_feedback_classifier(max_retries))
    n("response_formatter",      response_formatter)
    n("save_to_memory",          make_save_to_memory(history_service, deployment_name))
    n("observability_log",       observability_log)

    # ── Edges ─────────────────────────────────────────────────────────────
    builder.add_edge(START, "memory_shrink_check")

    builder.add_conditional_edges(
        "memory_shrink_check",
        lambda s: "memory_summarizer" if s.get("is_over_budget") else "fused_router",
    )
    builder.add_edge("memory_summarizer", "fused_router")

    builder.add_conditional_edges("fused_router", _route_from_router)
    builder.add_conditional_edges("memory_answer_generator", _route_from_memory_answer)

    builder.add_edge("catalog_lookup", "prompt_builder")
    builder.add_edge("prompt_builder", "sql_generator")

    builder.add_conditional_edges("sql_generator", _route_from_sql_gen)
    builder.add_conditional_edges("sqlglot_validate", _route_from_sqlglot)
    builder.add_conditional_edges("dlp_check", _route_from_dlp)
    builder.add_conditional_edges("execute_query", _route_from_execute)
    builder.add_conditional_edges(
        "trivial_result_check",
        _make_route_from_trivial(eval_analytics_enabled),
    )
    builder.add_conditional_edges("fused_eval_analytics", _route_from_eval)
    builder.add_conditional_edges("feedback_classifier", _route_from_feedback)

    builder.add_edge("response_formatter", "save_to_memory")
    builder.add_edge("save_to_memory", "observability_log")
    builder.add_edge("observability_log", END)

    compiled = builder.compile()
    logger.info("✅ LangGraph agent compiled — %d nodes", 16)
    return compiled


# ── Routing functions ─────────────────────────────────────────────────────────
# Each returns the name of the next node to execute.


def _route_from_router(state: AgentState) -> str:
    route = state.get("route", "needs_query")
    if route == "from_memory":
        return "memory_answer_generator"
    if route in ("out_of_scope", "unsafe", "greeting"):
        return "response_formatter"
    return "catalog_lookup"  # needs_query (default)


def _route_from_memory_answer(state: AgentState) -> str:
    # If the memory-answer node set escape hatch, run a real query
    if state.get("route") == "needs_query":
        return "catalog_lookup"
    return "response_formatter"


def _route_from_sql_gen(state: AgentState) -> str:
    if state.get("generated_sql"):
        return "sqlglot_validate"
    return "response_formatter"  # clarification or empty


def _route_from_sqlglot(state: AgentState) -> str:
    return "feedback_classifier" if state.get("sqlglot_error") else "dlp_check"


def _route_from_dlp(state: AgentState) -> str:
    return "response_formatter" if state.get("dlp_blocked") else "execute_query"


def _route_from_execute(state: AgentState) -> str:
    return "feedback_classifier" if state.get("exec_error") else "trivial_result_check"


def _make_route_from_trivial(eval_enabled: bool):
    """Return a routing function that respects both the compiled flag and per-request override."""
    def _route_from_trivial(state: AgentState) -> str:
        # Per-request override (from UI) takes priority over the compiled default.
        per_request = state.get("eval_analytics_override")
        effective_eval = per_request if per_request is not None else eval_enabled
        if state.get("is_trivial") or not effective_eval:
            return "response_formatter"
        return "fused_eval_analytics"
    return _route_from_trivial


def _route_from_trivial(state: AgentState) -> str:  # kept for direct test imports
    return "response_formatter" if state.get("is_trivial") else "fused_eval_analytics"


def _route_from_eval(state: AgentState) -> str:
    eval_result = state.get("eval_result") or {}
    return (
        "response_formatter"
        if eval_result.get("answers_intent", True)
        else "feedback_classifier"
    )


def _route_from_feedback(state: AgentState) -> str:
    feedback = state.get("feedback_type")
    if feedback == "exhausted":
        return "response_formatter"
    if feedback == "missing_table":
        return "catalog_lookup"
    return "sql_generator"  # syntax | exec | semantic
