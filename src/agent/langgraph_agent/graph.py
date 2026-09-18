"""LangGraph state-graph builder for the Jeen Insights text-to-SQL agent.

``build_graph`` is the single factory function called by ``JeenInsightsAgent.__init__``.
It wires all node factories with their service dependencies and returns a compiled
LangGraph ``CompiledStateGraph`` that can be invoked with ``await graph.ainvoke(state)``.

Graph topology (simplified; ``fmt`` = response_formatter):

    START ─(analysis resume)─► catalog_lookup
      └─► context_composer ─► fused_router
            ├─(greeting | out_of_scope | unsafe | clarify_route)─► fmt
            ├─(capability)─► capability_answer ─► fmt
            ├─(history_lookup)─► history_search ─► fmt
            ├─(from_memory)─► memory_answer_generator
            │        ├─(replay | prose answer)─► fmt
            │        ├─(computed table)─► trivial_result_check
            │        └─(needs_query)─► catalog_lookup
            └─(needs_query | needs_analysis)─► catalog_lookup
                    ├─(catalog_blocked)─► fmt
                    └─► filter_planner ─► filter_grounder
                              ├─(needs_analysis)─► analysis_planner ─► analysis_guard ─► analysis_sql ─┐
                              ├─(prior_refs)─► prior_data_binder ─► prompt_builder                  │
                              └─► prompt_builder ─► sql_generator                                     │
                                        ├─(clarification)─► fmt                                       │
                                        └─► sqlglot_validate ◄────────────────────────────────────────┘
                                                 ├─(error, SQL path)─► feedback_classifier
                                                 └─(valid)─► dlp_check ─(blocked)─► fmt
                                                                  └─(safe)─► execute_query
                                                                       ├─(error, SQL path)─► feedback_classifier
                                                                       ├─(rows, ML path)─► analysis_run ─► trivial_result_check
                                                                       └─(rows)─► empty_filter_result_check
                                                                                  ├─(reground)─► feedback_classifier
                                                                                  └─► trivial_result_check
                                                                                         ├─(trivial | eval off)─► fmt
                                                                                         └─► fused_eval_analytics
                                                                                                ├─(wrong, SQL path)─► feedback_classifier
                                                                                                └─► fmt
    feedback_classifier ─► sql_generator | catalog_lookup | filter_grounder | fmt
    fmt ─► save_to_memory ─► observability_log ─► END

Conversation memory: ``context_composer`` builds the turn ledger (question, SQL,
answer, result shape and data availability of the last N turns) that the
router, the SQL generator and the memory nodes read; rows are recovered by
reference (``PriorResultStore``) and computed over in the metadata Postgres as
``jsonb_to_recordset`` CTEs (``SnapshotSqlEngine``, no tables created), never
pasted into prompts. See ``nodes/context.py``, ``nodes/memory_answer.py``,
``nodes/binder.py`` and ``nodes/history.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional

from langgraph.graph import END, START, StateGraph

from src.agent.conversation_history import ConversationHistoryService
from src.agent.langgraph_agent.nodes.analysis import (
    make_analysis_sql,
    make_analysis_guard,
    make_analysis_planner,
    make_analysis_run,
    on_analysis_branch,
)
from src.agent.langgraph_agent.nodes.binder import make_prior_data_binder
from src.agent.langgraph_agent.nodes.capability import make_capability_answer
from src.agent.langgraph_agent.nodes.catalog import make_catalog_lookup, make_prompt_builder
from src.agent.langgraph_agent.nodes.context import context_composer
from src.agent.langgraph_agent.nodes.eval import make_fused_eval_analytics
from src.agent.langgraph_agent.nodes.execution import make_execute_query, trivial_result_check
from src.agent.langgraph_agent.nodes.feedback import make_feedback_classifier
from src.agent.langgraph_agent.nodes.filtering import (
    empty_filter_result_check,
    make_filter_grounder,
    make_filter_planner,
)
from src.agent.langgraph_agent.nodes.history import make_history_search
from src.agent.langgraph_agent.nodes.memory_answer import (
    make_memory_answer_generator,
    memory_needs_eval,
    on_memory_branch,
)
from src.agent.langgraph_agent.nodes.output import (
    make_save_to_memory,
    observability_log,
    response_formatter,
)
from src.agent.langgraph_agent.nodes.router import make_fused_router
from src.agent.langgraph_agent.nodes.sql_gen import make_sql_generator
from src.agent.langgraph_agent.nodes.validation import make_dlp_check, make_sqlglot_validate
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.prior_results import PriorResultStore, make_sql_rerun
from src.agent.progress import emit_progress
from src.agent.snapshot_sql import SnapshotSqlEngine
from src.metadata import MetadataLoader
from src.connectors import SqlRunner

logger = logging.getLogger(__name__)

# Retry budgets — not LangGraph's default 25 supersteps — must be the effective
# bound for a legitimate recovery path. The longest path that honours every
# budget (max_retries=3, one filter reground) is:
#   prefix   8  context_composer → fused_router → memory_answer_generator (escape
#               hatch) → catalog_lookup → filter_planner → filter_grounder →
#               prior_data_binder → prompt_builder
#   reground 9  sql_generator … empty_filter_result_check → feedback_classifier →
#               filter_grounder → prior_data_binder → prompt_builder
#   attempts 32 4 × (sql_generator, sqlglot_validate, dlp_check, execute_query,
#               empty_filter_result_check, trivial_result_check,
#               fused_eval_analytics, feedback_classifier)
#   tail     3  response_formatter → save_to_memory → observability_log
#   = 52 supersteps. 64 leaves headroom (the DAX graph uses the same value).
_GRAPH_LONGEST_LEGAL_PATH = 52
_GRAPH_RECURSION_LIMIT = 64


# ── Node metadata for the trace panel ────────────────────────────────────────
# (icon, type)  type is one of: llm | db | logic
_NODE_META: dict[str, tuple[str, str]] = {
    "context_composer":        ("🧠", "logic"),
    "fused_router":            ("🔀", "llm"),
    "capability_answer":       ("💡", "llm"),
    "memory_answer_generator": ("💬", "llm"),
    "history_search":          ("🗂", "db"),
    "catalog_lookup":          ("📦", "db"),
    "filter_planner":          ("🎯", "llm"),
    "filter_grounder":         ("🔎", "db"),
    "prior_data_binder":       ("🧷", "llm"),
    "prompt_builder":          ("🔧", "logic"),
    "sql_generator":           ("🧠", "llm"),
    "sqlglot_validate":        ("✅", "logic"),
    "dlp_check":               ("🛡", "logic"),
    "execute_query":           ("▶", "db"),
    "empty_filter_result_check": ("🧭", "logic"),
    "trivial_result_check":    ("⚡", "logic"),
    "fused_eval_analytics":    ("📊", "llm"),
    "feedback_classifier":     ("🔁", "logic"),
    "response_formatter":      ("📋", "logic"),
    "save_to_memory":          ("💾", "db"),
    "observability_log":       ("🪵", "logic"),
    # ML skills branch
    "analysis_planner":        ("🧪", "llm"),
    "analysis_guard":          ("🚧", "db"),
    "analysis_sql":            ("🔧", "logic"),
    "analysis_run":            ("🧮", "ml"),
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
            emit_progress(
                state, node=name, status="node_started", icon=icon, node_type=ntype
            )
            t0 = time.monotonic()
            try:
                result = await fn(state)
            except Exception as exc:
                elapsed = round((time.monotonic() - t0) * 1000)
                emit_progress(
                    state,
                    node=name,
                    status="node_failed",
                    icon=icon,
                    node_type=ntype,
                    elapsed_ms=elapsed,
                    error=str(exc),
                )
                raise
            elapsed = round((time.monotonic() - t0) * 1000)
            emit_progress(
                state,
                node=name,
                status="node_finished",
                icon=icon,
                node_type=ntype,
                elapsed_ms=elapsed,
            )
            out = {} if result is None else dict(result)
            out["trace"] = [{"node": name, "elapsed_ms": elapsed, "icon": icon, "type": ntype}]
            return out
        return _async_wrapper
    else:
        def _sync_wrapper(state):
            emit_progress(
                state, node=name, status="node_started", icon=icon, node_type=ntype
            )
            t0 = time.monotonic()
            try:
                result = fn(state)
            except Exception as exc:
                elapsed = round((time.monotonic() - t0) * 1000)
                emit_progress(
                    state,
                    node=name,
                    status="node_failed",
                    icon=icon,
                    node_type=ntype,
                    elapsed_ms=elapsed,
                    error=str(exc),
                )
                raise
            elapsed = round((time.monotonic() - t0) * 1000)
            emit_progress(
                state,
                node=name,
                status="node_finished",
                icon=icon,
                node_type=ntype,
                elapsed_ms=elapsed,
            )
            out = {} if result is None else dict(result)
            out["trace"] = [{"node": name, "elapsed_ms": elapsed, "icon": icon, "type": ntype}]
            return out
        return _sync_wrapper


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_graph(
    *,
    llm: LangChainLlmService,
    router_llm: LangChainLlmService,
    sql_runner: SqlRunner,
    metadata_loader: MetadataLoader,
    history_service: ConversationHistoryService,
    prompt_loader: PromptLoader,
    deployment_name: str,
    max_retries: int = 3,
    dlp_enabled: bool = True,
    sqlglot_validation_enabled: bool = True,
    eval_analytics_enabled: bool = True,
    require_catalog_for_query: bool = True,
    enforce_schema_qualifier: bool = True,
    dlp_governed_columns: Optional[List[str]] = None,
    filter_resolution_enabled: bool = True,
    filter_max_domain_values: int = 1000,
    filter_match_threshold: float = 78.0,
    filter_lookup_timeout_ms: int = 5000,
    filter_cache_ttl_seconds: int = 900,
    value_store_provider: Optional[Any] = None,
    ml_skills_enabled: bool = False,
    analysis_store: Any = None,
    analysis_runner_provider: Optional[Any] = None,
    analysis_limiter: Optional[Any] = None,
    analysis_audit: Optional[Any] = None,
    analysis_max_series_rows: int = 1500,
    analysis_max_entity_rows: int = 50_000,
    memory_compute_max_rows: int = 2000,
    memory_max_bound_values: int = 100,
    snapshot_engine: Optional[SnapshotSqlEngine] = None,
) -> Any:
    """Build and compile the LangGraph text-to-SQL agent.

    Parameters
    ----------
    llm:
        Primary (large-model) LLM service — used for sql_generator and
        fused_eval_analytics.
    router_llm:
        Router LLM service — used for fused_router, filter_planner,
        memory_answer_generator and prior_data_binder.  May be the same object
        as ``llm`` when no separate cheaper deployment is configured.
    sql_runner:
        Query runner for the selected data source (already enforces read-only safety).
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
    dlp_enabled:
        When True, DLP patterns are checked before executing any SQL.
    sqlglot_validation_enabled:
        When True, SQL is parsed and table names are checked before execution.
    ml_skills_enabled:
        Enables the ``needs_analysis`` route and the analysis branch
        (planner → guard → sql → run). ``analysis_store`` persists proposals
        and consent; ``analysis_runner_provider`` returns the configured
        ``AnalysisRunner`` (None → skills disabled at run time).
    memory_compute_max_rows / memory_max_bound_values:
        Row ceiling for computing over a prior result's stored rows, and the
        most values a prior result may contribute to a new query's ``IN`` list.
    snapshot_engine:
        Runs the memory computations (a SELECT in the metadata Postgres over the
        stored rows exposed as ``jsonb_to_recordset`` CTEs; no tables are created).
        Defaults to an engine on ``history_service.pool``; tests inject a double.

    Returns
    -------
    CompiledStateGraph
        A compiled LangGraph graph ready for ``await graph.ainvoke(state)``.
    """
    builder: StateGraph = StateGraph(AgentState)

    # ── Register nodes (all wrapped with _timed for execution trace) ────────
    def n(name, fn):  # shorthand: wrap + register
        builder.add_node(name, _timed(name, fn))

    # Prior results are recovered cache → stored snapshot → re-run at the source,
    # and computed over in the metadata Postgres as jsonb_to_recordset CTEs.
    prior_results = PriorResultStore(
        history_service=history_service,
        rerun=make_sql_rerun(sql_runner),
        max_rows=memory_compute_max_rows,
    )
    engine = snapshot_engine or SnapshotSqlEngine(getattr(history_service, "pool", None))

    n("context_composer",        context_composer)
    n("fused_router",            make_fused_router(router_llm, prompt_loader, ml_skills_enabled=ml_skills_enabled))
    n("capability_answer",       make_capability_answer(llm, prompt_loader))
    n("memory_answer_generator", make_memory_answer_generator(
        router_llm, prompt_loader, store=prior_results, engine=engine, max_rows=memory_compute_max_rows))
    n("history_search",          make_history_search(history_service))
    n("catalog_lookup",          make_catalog_lookup(metadata_loader, require_catalog_for_query))
    # Metadata-first value evidence (Schema Modeler profiles / captured values)
    # shared by the planner's reverse lookup and the grounder's tiers.
    from src.agent.langgraph_agent.value_store_provider import value_store_for  # noqa: PLC0415

    store_provider = value_store_provider or value_store_for
    n("filter_planner",          make_filter_planner(
        router_llm, prompt_loader,
        value_store_provider=store_provider, match_threshold=filter_match_threshold,
        governed_columns=dlp_governed_columns))
    n(
        "filter_grounder",
        make_filter_grounder(
            sql_runner,
            enabled=filter_resolution_enabled,
            max_domain_values=filter_max_domain_values,
            match_threshold=filter_match_threshold,
            lookup_timeout_ms=filter_lookup_timeout_ms,
            cache_ttl_seconds=filter_cache_ttl_seconds,
            governed_columns=dlp_governed_columns,
            value_store_provider=store_provider,
        ),
    )
    n("prior_data_binder",       make_prior_data_binder(
        router_llm, prompt_loader, store=prior_results, engine=engine,
        max_values=memory_max_bound_values, max_rows=memory_compute_max_rows))
    n("prompt_builder",          make_prompt_builder(prompt_loader))
    n("sql_generator",           make_sql_generator(llm, prompt_loader))
    n("sqlglot_validate",        make_sqlglot_validate(sqlglot_validation_enabled, require_catalog_for_query, enforce_schema_qualifier))
    n("dlp_check",               make_dlp_check(dlp_enabled, dlp_governed_columns))
    n("execute_query",           make_execute_query(sql_runner))
    n("empty_filter_result_check", empty_filter_result_check)
    n("trivial_result_check",    trivial_result_check)
    n("fused_eval_analytics",    make_fused_eval_analytics(llm, prompt_loader))
    n("feedback_classifier",     make_feedback_classifier(max_retries))
    n("response_formatter",      response_formatter)
    n("save_to_memory",          make_save_to_memory(history_service, deployment_name))
    n("observability_log",       observability_log)

    # ── ML skills branch ──────────────────────────────────────────────────
    # Registered unconditionally so a compiled graph always has the nodes; the
    # router gate (ml_skills_enabled) decides whether any question reaches them.
    n("analysis_planner",        make_analysis_planner(router_llm, prompt_loader, analysis_store))
    n("analysis_guard",          make_analysis_guard(sql_runner, analysis_store, limiter=analysis_limiter,
                                                     max_entity_rows=analysis_max_entity_rows))
    n("analysis_sql",            make_analysis_sql(max_series_rows=analysis_max_series_rows,
                                                   max_entity_rows=analysis_max_entity_rows))
    n(
        "analysis_run",
        make_analysis_run(
            analysis_runner_provider or (lambda: None),
            analysis_store,
            audit=analysis_audit,
            max_series_rows=analysis_max_series_rows,
            max_entity_rows=analysis_max_entity_rows,
        ),
    )

    # ── Edges ─────────────────────────────────────────────────────────────
    # A confirmed analysis (re-entered from /api/analysis/run with
    # server-validated params) skips memory, routing and filter planning and
    # goes straight to the catalog so the guard has the schema it needs.
    builder.add_conditional_edges(
        START,
        lambda s: "catalog_lookup" if (s.get("analysis_resume") or s.get("analysis_confirmed")) else "context_composer",
    )
    builder.add_edge("context_composer", "fused_router")

    builder.add_conditional_edges("fused_router", _route_from_router)
    builder.add_edge("capability_answer", "response_formatter")
    builder.add_edge("history_search", "response_formatter")
    builder.add_conditional_edges("memory_answer_generator", _route_from_memory_answer)

    builder.add_conditional_edges("catalog_lookup", _route_from_catalog)
    builder.add_conditional_edges("filter_planner", _route_from_filter_planner)
    builder.add_conditional_edges("filter_grounder", _route_from_filter_grounder)
    builder.add_conditional_edges("prior_data_binder", _route_from_binder)
    builder.add_edge("prompt_builder", "sql_generator")

    builder.add_conditional_edges("analysis_planner", _route_from_analysis_planner)
    builder.add_conditional_edges("analysis_guard", _route_from_analysis_guard)
    builder.add_conditional_edges("analysis_sql", _route_from_analysis_sql)
    builder.add_conditional_edges("analysis_run", _route_from_analysis_run)

    builder.add_conditional_edges("sql_generator", _route_from_sql_gen)
    builder.add_conditional_edges("sqlglot_validate", _route_from_sqlglot)
    builder.add_conditional_edges("dlp_check", _route_from_dlp)
    builder.add_conditional_edges("execute_query", _route_from_execute)
    builder.add_conditional_edges("empty_filter_result_check", _route_from_empty_filter)
    builder.add_conditional_edges(
        "trivial_result_check",
        _make_route_from_trivial(eval_analytics_enabled),
    )
    builder.add_conditional_edges("fused_eval_analytics", _route_from_eval)
    builder.add_conditional_edges("feedback_classifier", _route_from_feedback)

    builder.add_edge("response_formatter", "save_to_memory")
    builder.add_edge("save_to_memory", "observability_log")
    builder.add_edge("observability_log", END)

    compiled = builder.compile().with_config(
        {"recursion_limit": _GRAPH_RECURSION_LIMIT}
    )
    logger.info("✅ LangGraph agent compiled — %d nodes", len(builder.nodes))
    return compiled


# ── Routing functions ─────────────────────────────────────────────────────────
# Each returns the name of the next node to execute.


def _route_from_router(state: AgentState) -> str:
    route = state.get("route", "needs_query")
    if route == "from_memory":
        return "memory_answer_generator"
    if route == "history_lookup":
        return "history_search"
    if route == "capability":
        return "capability_answer"
    # clarify_route: the SQL-vs-ML choice was ambiguous — the router already set
    # the question + options as the answer, so go straight to formatting.
    if route in ("out_of_scope", "unsafe", "greeting", "clarify_route"):
        return "response_formatter"
    return "catalog_lookup"  # needs_query (default)


def _route_from_memory_answer(state: AgentState) -> str:
    # If the memory-answer node set escape hatch, run a real query
    if state.get("route") == "needs_query":
        return "catalog_lookup"
    # A table computed over stored rows gets the same narration as a live
    # result; a replay already carries its original answer, prose is final.
    if memory_needs_eval(state):
        return "trivial_result_check"
    return "response_formatter"


def _route_from_catalog(state: AgentState) -> str:
    # Deny-by-default: when no usable catalog is available, skip SQL generation
    # entirely and return a clear error rather than querying blindly.
    if state.get("catalog_blocked"):
        return "response_formatter"
    # Re-entry from /api/analysis/run: params are already validated, so the
    # branch starts at the guard (planner and filter planning are skipped).
    if (state.get("analysis_resume") or state.get("analysis_confirmed")) and on_analysis_branch(state):
        return "analysis_guard"
    return "filter_planner"


def _route_from_filter_planner(state: AgentState) -> str:
    return "response_formatter" if state.get("filter_clarification_required") else "filter_grounder"


def _route_from_filter_grounder(state: AgentState) -> str:
    if state.get("filter_clarification_required"):
        return "response_formatter"
    # Branch here (not at catalog_lookup) so grounded literal filters are
    # available to the planner for free.
    if state.get("route") == "needs_analysis":
        return "analysis_planner"
    # The question builds on a prior result → bind its values before the
    # prompt is rendered, so the SQL model receives them as a verified filter.
    if state.get("prior_refs"):
        return "prior_data_binder"
    return "prompt_builder"


def _route_from_binder(state: AgentState) -> str:
    # Too many values to carry into an IN list → ask the user to narrow it.
    return "response_formatter" if state.get("filter_clarification_required") else "prompt_builder"


def _route_from_analysis_planner(state: AgentState) -> str:
    if state.get("analysis_clarification"):
        return "response_formatter"
    if not on_analysis_branch(state):
        return "prompt_builder"  # planner fell back to the SQL path
    return "analysis_guard"


def _route_from_analysis_guard(state: AgentState) -> str:
    if (
        state.get("analysis_guard_failure")
        or state.get("analysis_confirm_required")
        or state.get("analysis_error")
    ):
        return "response_formatter"
    return "analysis_sql"


def _route_from_analysis_sql(state: AgentState) -> str:
    return "response_formatter" if state.get("analysis_error") else "sqlglot_validate"


def _route_from_analysis_run(state: AgentState) -> str:
    if state.get("analysis_guard_failure") or state.get("analysis_error"):
        return "response_formatter"
    return "trivial_result_check"


def _route_from_sql_gen(state: AgentState) -> str:
    if state.get("generated_sql"):
        return "sqlglot_validate"
    return "response_formatter"  # clarification or empty


def _route_from_sqlglot(state: AgentState) -> str:
    if state.get("sqlglot_error"):
        # A deterministic builder cannot be "repaired" by the LLM loop.
        return "response_formatter" if on_analysis_branch(state) else "feedback_classifier"
    return "dlp_check"


def _route_from_dlp(state: AgentState) -> str:
    return "response_formatter" if state.get("dlp_blocked") else "execute_query"


def _route_from_execute(state: AgentState) -> str:
    if state.get("exec_error"):
        return "response_formatter" if on_analysis_branch(state) else "feedback_classifier"
    if on_analysis_branch(state):
        return "analysis_run"
    return "empty_filter_result_check"


def _route_from_empty_filter(state: AgentState) -> str:
    return "feedback_classifier" if state.get("needs_filter_reground") else "trivial_result_check"


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


def _route_from_eval(state: AgentState) -> str:
    eval_result = state.get("eval_result") or {}
    # An ML result was produced by a validated engine, and a memory result was
    # computed from stored rows; a doubtful narration must never trigger the
    # SQL repair loop for either (there is no system prompt to retry with).
    if on_analysis_branch(state) or on_memory_branch(state):
        return "response_formatter"
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
    if feedback == "resolve_filters":
        return "filter_grounder"
    return "sql_generator"  # syntax | exec | semantic


# ── Standalone insights eval subgraph ────────────────────────────────────────────
# Single eval node wired as a small independent graph; called directly by the
# insights API endpoint (/api/generate-insights) with question + SQL + results.

from src.agent.langgraph_agent.nodes.eval import make_fused_eval_analytics_subgraph  # noqa: E402
from src.agent.langgraph_agent.state import InsightsState  # noqa: E402


def build_insights_eval_graph(llm_service: Any, prompt_cache: Any):
    """Compile and return the insights eval ``StateGraph``.

    Builds a single-node graph (eval) that evaluates SQL results and returns
    a summary, key insights, and 3-5 follow-up questions.  Stored on
    ``src.api.state.insights_eval_graph`` at startup.
    """
    eval_node = make_fused_eval_analytics_subgraph(llm_service, prompt_cache)
    g: StateGraph = StateGraph(InsightsState)
    g.add_node("eval", eval_node)
    g.set_entry_point("eval")
    g.add_edge("eval", END)
    compiled = g.compile()
    logging.getLogger(__name__).info(
        "insights_eval_graph: compiled successfully (nodes: eval)"
    )
    return compiled


async def run_eval(
    graph,
    *,
    question: str,
    sql: str,
    results: list,
    row_count: int,
    statistics: str = "",
) -> dict:
    """Invoke the eval graph and return the final InsightsState."""
    return await graph.ainvoke(
        InsightsState(
            question=question,
            sql=sql,
            results=results,
            row_count=row_count,
            statistics=statistics,
        )
    )
