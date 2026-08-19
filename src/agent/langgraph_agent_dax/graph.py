"""LangGraph state-graph builder for the Jeen Insights text-to-DAX agent.

``build_dax_graph`` is the single factory called by ``DaxInsightsAgent``. It is a
**distinct** graph from the text-to-SQL one: the query core (plan → generate →
static-validate → execute → repair) is DAX-specific, while everything after rows
return (memory, router, insights/eval, response formatting, persistence,
observability) reuses the shared, engine-agnostic nodes **imported read-only**
from ``src.agent.langgraph_agent`` — no DAX change alters their behaviour, so
the SQL flow cannot regress.

Graph topology (``fmt`` = response_formatter, ``feedback`` = dax_feedback_router):

    START → memory_shrink_check ─(over budget)→ memory_summarizer ─┐
                                └─(within budget)──────────────────┴→ fused_router

    fused_router ─(greeting | out_of_scope | unsafe)────────────────→ fmt
                 ├(from_memory)→ memory_answer_generator ─(answered)→ fmt
                 │                                       └(needs_query)┐
                 └(needs_query)───────────────────────────────────────┴→ dax_catalog_lookup

    dax_catalog_lookup    ─(blocked)→ fmt          └→ dax_query_planner
    dax_query_planner     ─(clarify)→ fmt          └→ dax_entity_resolver
    dax_entity_resolver   ─(clarify)→ fmt          └→ dax_prompt_builder
    dax_prompt_builder                             └→ dax_generator
    dax_generator         ─(clarify | empty)→ fmt  └→ dax_static_validate
    dax_static_validate   ─(blocked)→ fmt
                          ├(repairable)→ dax_repair → dax_static_validate
                          └→ pbi_execute_query
    pbi_execute_query     ─(terminal)→ fmt  ├(error)→ feedback
                                            └→ result_integrity_check
    result_integrity_check ─(empty diag)→ feedback └→ trivial_result_check
    trivial_result_check  ─(trivial | eval off)→ fmt └→ fused_eval_analytics
    fused_eval_analytics  ─(semantic mismatch)→ feedback └→ fmt

    feedback → dax_repair | dax_generator | dax_query_planner |
               dax_entity_resolver | dax_catalog_lookup | fmt

    fmt → save_to_memory → observability_log → END
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent.nodes.eval import make_fused_eval_analytics
from src.agent.langgraph_agent.nodes.execution import trivial_result_check
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
from src.agent.langgraph_agent.nodes.sql_gen import make_memory_answer_generator
from src.agent.langgraph_agent_dax.nodes.catalog import (
    make_dax_catalog_lookup,
    make_dax_prompt_builder,
)
from src.agent.langgraph_agent_dax.nodes.dax_execute import (
    make_pbi_execute_query,
    result_integrity_check,
)
from src.agent.langgraph_agent_dax.nodes.dax_gen import make_dax_generator
from src.agent.langgraph_agent_dax.nodes.dax_validate import (
    make_dax_repair,
    make_dax_static_validate,
)
from src.agent.langgraph_agent_dax.nodes.entity_resolver import (
    make_dax_entity_resolver,
    make_probe_factory,
)
from src.agent.langgraph_agent_dax.nodes.feedback import make_dax_feedback_router
from src.agent.langgraph_agent_dax.nodes.planner import make_dax_query_planner
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.agent.llm_service import LangChainLlmService
from src.connectors.powerbi_token import TokenProviderFactory
from src.metadata import MetadataLoader

logger = logging.getLogger(__name__)

# The DAX graph uses multiple nodes for each bounded repair cycle
# (feedback → repair/regenerate → validation → execution). This must exceed
# LangGraph's default of 25 so the DAX retry budget, not graph recursion, ends
# a request.
_DAX_GRAPH_RECURSION_LIMIT = 64


_NODE_META: dict[str, tuple[str, str]] = {
    "memory_shrink_check":     ("🧠", "logic"),
    "memory_summarizer":       ("🤏", "llm"),
    "fused_router":            ("🔀", "llm"),
    "memory_answer_generator": ("💬", "llm"),
    "dax_catalog_lookup":      ("📦", "db"),
    "dax_query_planner":       ("🗺", "llm"),
    "dax_entity_resolver":     ("🔍", "db"),
    "dax_prompt_builder":      ("🔧", "logic"),
    "dax_generator":           ("🧠", "llm"),
    "dax_static_validate":     ("✅", "logic"),
    "dax_repair":              ("🩹", "llm"),
    "pbi_execute_query":       ("▶", "db"),
    "result_integrity_check":  ("🔎", "logic"),
    "trivial_result_check":    ("⚡", "logic"),
    "fused_eval_analytics":    ("📊", "llm"),
    "dax_feedback_router":     ("🔁", "logic"),
    "response_formatter":      ("📋", "logic"),
    "save_to_memory":          ("💾", "db"),
    "observability_log":       ("🪵", "logic"),
}


def _timed(name: str, fn: Any) -> Any:
    """Wrap a node so it appends a timing event to ``trace`` (like the SQL graph)."""
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

    def _sync_wrapper(state):
        t0 = time.monotonic()
        result = fn(state)
        elapsed = round((time.monotonic() - t0) * 1000)
        out = {} if result is None else dict(result)
        out["trace"] = [{"node": name, "elapsed_ms": elapsed, "icon": icon, "type": ntype}]
        return out
    return _sync_wrapper


def build_dax_graph(
    *,
    llm: LangChainLlmService,
    router_llm: LangChainLlmService,
    metadata_loader: MetadataLoader,
    history_service: ConversationHistoryService,
    prompt_loader: DaxPromptLoader,
    deployment_name: str,
    max_retries: int = 4,
    max_history_tokens: int = 3000,
    dlp_enabled: bool = True,
    dax_validation_enabled: bool = True,
    eval_analytics_enabled: bool = True,
    require_catalog_for_query: bool = True,
    dlp_governed_columns=None,
    entity_resolution_enabled: bool = True,
    entity_max_domain_values: int = 1000,
    entity_match_threshold: float = 78.0,
    entity_cross_column_enabled: bool = True,
    token_provider_factory: Optional[TokenProviderFactory] = None,
) -> Any:
    """Build and compile the text-to-DAX LangGraph.

    ``token_provider_factory`` is the single source of delegated Power BI
    tokens for the two nodes that talk to Power BI. Composition supplies it
    (see ``src/api/lifespan.py``); a graph built without one cannot reach Power
    BI at all, which is what tests and connector-less deployments want.
    """
    builder: StateGraph = StateGraph(DaxAgentState)

    def n(name, fn):
        builder.add_node(name, _timed(name, fn))

    # Shared, engine-agnostic nodes (imported read-only).
    n("memory_shrink_check",     make_memory_shrink_check(max_history_tokens))
    n("memory_summarizer",       make_memory_summarizer(router_llm, prompt_loader))
    n("fused_router",            make_fused_router(router_llm, prompt_loader))
    n("memory_answer_generator", make_memory_answer_generator(router_llm, prompt_loader))
    n("trivial_result_check",    trivial_result_check)
    n("fused_eval_analytics",    make_fused_eval_analytics(llm, prompt_loader))
    n("response_formatter",      response_formatter)
    n("save_to_memory",          make_save_to_memory(history_service, deployment_name))
    n("observability_log",       observability_log)

    # DAX-specific query core.
    n("dax_catalog_lookup",      make_dax_catalog_lookup(metadata_loader, require_catalog_for_query))
    n("dax_query_planner",       make_dax_query_planner(llm, prompt_loader))
    n("dax_entity_resolver",     make_dax_entity_resolver(
        entity_resolution_enabled,
        dlp_enabled=dlp_enabled,
        dlp_governed_columns=dlp_governed_columns,
        max_domain_values=entity_max_domain_values,
        match_threshold=entity_match_threshold,
        cross_column_enabled=entity_cross_column_enabled,
        probe_factory=make_probe_factory(token_provider_factory),
    ))
    n("dax_prompt_builder",      make_dax_prompt_builder(prompt_loader))
    n("dax_generator",           make_dax_generator(llm, prompt_loader))
    n("dax_static_validate",     make_dax_static_validate(
        dax_validation_enabled, require_catalog_for_query, dlp_enabled, dlp_governed_columns))
    n("dax_repair",              make_dax_repair(llm, prompt_loader))
    n("pbi_execute_query",       make_pbi_execute_query(token_provider_factory))
    n("result_integrity_check",  result_integrity_check)
    n("dax_feedback_router",     make_dax_feedback_router(max_retries))

    # Edges.
    builder.add_edge(START, "memory_shrink_check")
    builder.add_conditional_edges(
        "memory_shrink_check",
        lambda s: "memory_summarizer" if s.get("is_over_budget") else "fused_router",
    )
    builder.add_edge("memory_summarizer", "fused_router")
    builder.add_conditional_edges("fused_router", _route_from_router)
    builder.add_conditional_edges("memory_answer_generator", _route_from_memory_answer)

    builder.add_conditional_edges("dax_catalog_lookup", _route_from_catalog)
    builder.add_conditional_edges("dax_query_planner", _route_from_planner)
    builder.add_conditional_edges("dax_entity_resolver", _route_from_entity_resolver)
    builder.add_edge("dax_prompt_builder", "dax_generator")
    builder.add_conditional_edges("dax_generator", _route_from_generator)
    builder.add_conditional_edges("dax_static_validate", _route_from_validate)
    builder.add_edge("dax_repair", "dax_static_validate")
    builder.add_conditional_edges("pbi_execute_query", _route_from_execute)
    builder.add_conditional_edges("result_integrity_check", _route_from_integrity)
    builder.add_conditional_edges(
        "trivial_result_check", _make_route_from_trivial(eval_analytics_enabled)
    )
    builder.add_conditional_edges("fused_eval_analytics", _route_from_eval)
    builder.add_conditional_edges("dax_feedback_router", _route_from_feedback)

    builder.add_edge("response_formatter", "save_to_memory")
    builder.add_edge("save_to_memory", "observability_log")
    builder.add_edge("observability_log", END)

    compiled = builder.compile().with_config(
        {"recursion_limit": _DAX_GRAPH_RECURSION_LIMIT}
    )
    logger.info("✅ DAX LangGraph agent compiled — %d nodes", len(builder.nodes))
    return compiled


# ── Routing functions ─────────────────────────────────────────────────────────


def _route_from_router(state: DaxAgentState) -> str:
    route = state.get("route", "needs_query")
    if route == "from_memory":
        return "memory_answer_generator"
    if route in ("out_of_scope", "unsafe", "greeting"):
        return "response_formatter"
    return "dax_catalog_lookup"


def _route_from_memory_answer(state: DaxAgentState) -> str:
    if state.get("route") == "needs_query":
        return "dax_catalog_lookup"
    return "response_formatter"


def _route_from_catalog(state: DaxAgentState) -> str:
    if state.get("catalog_blocked"):
        return "response_formatter"
    return "dax_query_planner"


def _route_from_planner(state: DaxAgentState) -> str:
    if state.get("clarification_required"):
        return "response_formatter"
    return "dax_entity_resolver"


def _route_from_entity_resolver(state: DaxAgentState) -> str:
    # A filter literal that matches nothing (or several things) is a question for
    # the user, not a query to run: executing it would return an empty table and
    # hide the real problem.
    if state.get("clarification_required"):
        return "response_formatter"
    return "dax_prompt_builder"


def _route_from_generator(state: DaxAgentState) -> str:
    if state.get("generated_dax"):
        return "dax_static_validate"
    return "response_formatter"  # clarification or empty


def _route_from_validate(state: DaxAgentState) -> str:
    if state.get("dax_validation_error") or state.get("dlp_blocked"):
        return "response_formatter"
    if state.get("dax_repairable_error"):
        return "dax_repair"
    return "pbi_execute_query"


def _route_from_execute(state: DaxAgentState) -> str:
    if state.get("dax_terminal"):
        return "response_formatter"
    if state.get("exec_error"):
        return "dax_feedback_router"
    return "result_integrity_check"


def _route_from_integrity(state: DaxAgentState) -> str:
    if state.get("integrity_action") == "empty_diagnostic":
        return "dax_feedback_router"
    return "trivial_result_check"


def _make_route_from_trivial(eval_enabled: bool):
    def _route_from_trivial(state: DaxAgentState) -> str:
        per_request = state.get("eval_analytics_override")
        effective_eval = per_request if per_request is not None else eval_enabled
        if state.get("is_trivial") or not effective_eval:
            return "response_formatter"
        return "fused_eval_analytics"
    return _route_from_trivial


def _route_from_eval(state: DaxAgentState) -> str:
    eval_result = state.get("eval_result") or {}
    return (
        "response_formatter"
        if eval_result.get("answers_intent", True)
        else "dax_feedback_router"
    )


def _route_from_feedback(state: DaxAgentState) -> str:
    action = state.get("dax_feedback_action")
    if action == "local_repair":
        return "dax_repair"
    if action == "regenerate":
        return "dax_generator"
    if action == "replan":
        return "dax_query_planner"
    if action == "resolve_entities":
        return "dax_entity_resolver"
    if action == "refresh_catalog":
        return "dax_catalog_lookup"
    return "response_formatter"  # clarify | exhausted


__all__ = ["build_dax_graph"]
