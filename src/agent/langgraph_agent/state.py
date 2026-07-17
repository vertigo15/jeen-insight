"""Shared state schema for the Jeen Insights LangGraph agent.

Every node receives the full ``AgentState`` dict and returns a partial dict of
the fields it updates.  LangGraph merges those updates into the running state
before advancing to the next node (last-writer-wins for each key).

Field groups:
  Input        — set once before graph invocation.
  Connection   — set once before graph invocation from the Connection object.
  Audit        — set by process_question and updated by LLM / execution nodes.
  Memory       — populated by memory_shrink_check / memory_summarizer nodes.
  Routing      — written by fused_router.
  Catalog      — written by catalog_lookup + prompt_builder.
  SQL loop     — written/updated across sql_generator + retry nodes.
  Validation   — written by sqlglot_validate + dlp_check.
  Execution    — written by execute_query + trivial_result_check.
  Evaluation   — written by fused_eval_analytics.
  Feedback     — written by feedback_classifier.
  Output       — written by response_formatter.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional
from uuid import UUID

from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # ── Input ─────────────────────────────────────────────────────────────
    question: str
    session_id: Optional[UUID]
    source_key: str
    user_context: Dict[str, Any]
    limit: Optional[int]
    temperature: Optional[float]

    # ── Connection info ───────────────────────────────────────────────────
    connection_display_name: str
    database_type: str
    connection_database: Optional[str]
    connection_catalog: Optional[str]
    connection_schema: Optional[str]

    # ── Audit ─────────────────────────────────────────────────────────────
    query_id: Optional[UUID]
    user_id: Optional[str]
    start_time: float
    llm_call_count: int
    llm_latency_ms: int          # cumulative ms across all LLM calls
    token_usage: Dict[str, int]  # {input_tokens, output_tokens, total_tokens}

    # ── Memory ────────────────────────────────────────────────────────────
    conversation_history: List[Dict[str, Any]]
    memory_summary: Optional[str]
    is_over_budget: bool

    # ── Routing ───────────────────────────────────────────────────────────
    # Values: needs_query | from_memory | out_of_scope | unsafe
    route: str
    route_reason: str

    # ── Catalog / prompt ──────────────────────────────────────────────────
    metadata_bundle: Dict[str, str]
    system_prompt: str
    structured_prompt: Dict[str, Any]   # forwarded as-is to the UI
    dialect_rules: str
    known_tables: List[str]             # lower-cased table names from catalog
    known_columns: List[str]            # lower-cased column names from catalog (flat)
    table_columns: Dict[str, List[str]] # lower-cased {table: [columns]} from catalog
    catalog_source_used: str            # 'mcp' | 'db' — provider that served the catalog
    catalog_cache: Optional[str]        # 'hit' | 'miss' | None (DB path has no MCP cache)
    catalog_load_ms: int                # wall time spent loading the catalog bundle
    catalog_available: bool             # True when the catalog loaded with >=1 table
    catalog_error: Optional[str]        # set when the catalog failed to load
    catalog_blocked: bool               # True → deny-by-default (no usable catalog)

    # ── SQL generation loop ───────────────────────────────────────────────
    retry_count: int
    generated_sql: Optional[str]
    clarification: Optional[str]        # LLM asked a clarifying question
    error_context: Optional[str]        # fed back into sql_generator on retry

    # ── Validation ────────────────────────────────────────────────────────
    sqlglot_error: Optional[str]
    dlp_blocked: bool
    governance_error: Optional[str]

    # ── Execution ─────────────────────────────────────────────────────────
    query_result: Optional[Dict[str, Any]]
    exec_error: Optional[str]
    execution_time_ms: Optional[int]

    # ── Evaluation ────────────────────────────────────────────────────────
    is_trivial: bool
    # {answers_intent: bool, summary: str, insights: [...], follow_up_questions: [str, ...]}
    eval_result: Optional[Dict[str, Any]]

    # ── Feedback ──────────────────────────────────────────────────────────
    # Values: syntax | missing_table | exec | semantic | exhausted
    feedback_type: Optional[str]

    # ── Per-request overrides (set by process_question from API request) ────
    # None = use compiled graph / server defaults.
    eval_analytics_override: Optional[bool]   # overrides EVAL_ANALYTICS_ENABLED
    llm_timeout_seconds: Optional[int]        # overrides LLM_TIMEOUT_SECONDS per call
    max_result_rows: Optional[int]            # hard row cap passed to run_sql
    statement_timeout_ms: Optional[int]       # per-statement DB timeout for run_sql

    # ── Node prompts (captured for developer inspection) ──────────────────
    # Each LLM node stores its rendered prompt here so the dev panel can
    # surface the full prompt used at every stage.
    # node_name → rendered prompt text
    node_prompts: Dict[str, str]

    # ── Execution trace (accumulates across every node) ────────────────────
    # Each node appends one event dict.  ``operator.add`` tells LangGraph to
    # concatenate lists rather than overwrite, so the full history is preserved.
    trace: Annotated[List[Dict[str, Any]], operator.add]

    # ── Output ────────────────────────────────────────────────────────────────────────
    answer: Optional[str]
    formatted_response: Dict[str, Any]
    error: Optional[str]


# ──────────────────────────────────────────────────────────────────────────────
# Insights eval subgraph state (used by build_insights_eval_graph / run_eval)
# ──────────────────────────────────────────────────────────────────────────────

class InsightsState(TypedDict, total=False):
    """Lightweight state for the standalone insights eval subgraph.

    Used by ``build_insights_eval_graph`` / ``run_eval`` when the insights
    API endpoint calls the eval node directly (outside the full pipeline).
    """
    # inputs
    question: str
    sql: str
    results: List[Any]
    row_count: int
    # full-data statistics block (computed over the whole result set, not just
    # the sample) so the eval node can reason over all the data
    statistics: str
    # eval outputs
    answers_intent: bool
    summary: str
    insights: List[str]
    follow_up_questions: List[str]
    error: Optional[str]
    # recommended actions from the eval node (0-2 actionable suggestions)
    suggestions: List[str]
    # prompt used by the eval node (returned for developer inspection)
    prompt_text: Optional[str]
