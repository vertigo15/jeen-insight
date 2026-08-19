"""State schema for the text-to-DAX LangGraph agent.

``DaxAgentState`` is a **superset** of the SQL ``AgentState``: it inherits every
common field (input, connection, audit, memory, routing, evaluation, output,
trace, per-request overrides) so the shared, engine-agnostic post-data nodes
(``fused_router``, ``memory_*``, ``trivial_result_check``,
``fused_eval_analytics``, ``response_formatter``, ``save_to_memory``,
``observability_log``) read the exact same keys they read for SQL — unchanged.

On top of that it adds the DAX-specific fields the query core needs: a typed
query plan, the DAX catalog (measures kept separate from columns, relationships,
date table), generation/validation artifacts, Power BI execution/integrity
results, and the separate DAX retry-budget counters.

Security: the delegated Power BI OAuth token is **never** stored here. The
execution node resolves a request-scoped token, uses it for one call, and
discards it. Only non-secret fields (workspace/dataset ids, model version) live
in state.
"""

from __future__ import annotations

# NOTE: TypedDict flattens every inherited field's annotation into this subclass'
# ``__annotations__``, and ``get_type_hints(DaxAgentState)`` (called by LangGraph
# when it builds the state graph) evaluates ALL of them — including the base
# ``AgentState`` fields — against THIS module's globals. So the names those base
# annotations reference (``UUID``, ``Annotated``, ``operator``) must be importable
# here even though DaxAgentState's own fields don't use them; otherwise graph
# construction raises ``NameError: name 'UUID' is not defined``.
import operator  # noqa: F401  (needed to resolve inherited ``trace`` annotation)
from typing import Annotated, Any, Dict, List, Optional  # noqa: F401  (Annotated: inherited)
from uuid import UUID  # noqa: F401  (needed to resolve inherited session_id/query_id)

from src.agent.langgraph_agent.state import AgentState


class DaxAgentState(AgentState, total=False):
    # ── Power BI connection target ──────────────────────────────────────────
    workspace_id: Optional[str]
    dataset_id: Optional[str]
    model_version: Optional[str]

    # ── DAX catalog (measures kept SEPARATE from columns) ───────────────────
    known_measures: List[str]                 # lower-cased measure names
    measure_home_tables: Dict[str, str]       # measure -> home table (display)
    measure_dependencies: Dict[str, List[str]]  # measure -> [referenced names]
    measures_available: bool                  # True when >=1 curated measure

    # ── Relationships / date model ──────────────────────────────────────────
    relationship_graph: List[Dict[str, Any]]  # [{from, to, active, cardinality}]
    date_table: Optional[str]
    date_column: Optional[str]
    is_marked_date_table: bool

    # ── Typed query plan (produced by dax_query_planner) ────────────────────
    query_plan: Optional[Dict[str, Any]]
    plan_grain: Optional[str]
    plan_assumptions: List[str]
    clarification_required: bool

    # ── Value (entity) linking, produced by dax_entity_resolver ─────────────
    # Filter literals confirmed against real column values:
    #   [{target, raw_value, value, status}]
    resolved_entities: List[Dict[str, Any]]
    # Literals that need the user to choose or that exist nowhere in the model:
    #   [{target, column, value, candidates, columns?}]
    entity_ambiguities: List[Dict[str, Any]]
    # Literals resolution could not verify either way (governed / non-text /
    # domain too large / probe failed). A non-empty list is what allows the
    # feedback router to retry resolution on an empty result.
    unresolved_entities: List[Dict[str, Any]]
    entity_resolution_attempts: int
    # Admin-tunable resolution controls, snapshotted once per question by the
    # agent so a mid-flight settings change cannot alter behaviour between
    # repair retries. Absent when a graph is driven directly (tests, evals), in
    # which case the node falls back to its construction-time defaults.
    entity_resolution_enabled: bool
    entity_max_domain_values: int
    entity_match_threshold: float
    entity_cross_column_enabled: bool

    # ── DAX generation / validation ─────────────────────────────────────────
    generated_dax: Optional[str]
    identifiers_used: List[Dict[str, Any]]     # resolved DaxIdentifier dicts
    defined_measures: List[str]
    expected_output_schema: List[str]
    expected_grain: Optional[str]
    dax_lint_errors: List[str]
    dax_validation_error: Optional[str]        # blocking (governance/read-only)
    dax_repairable_error: Optional[str]        # feeds the local repair loop
    resolved_symbols: Dict[str, Any]
    governed_lineage: List[str]

    # ── Power BI execution / integrity ──────────────────────────────────────
    http_status: Optional[int]
    pbi_error_code: Optional[str]
    pbi_error_message: Optional[str]
    error_location: Optional[str]
    is_partial: bool
    is_empty: bool
    returned_row_count: Optional[int]
    actual_schema: List[str]
    retry_after: Optional[float]
    integrity_action: Optional[str]

    # ── DAX retry control (separate transport vs repair/replan budgets) ─────
    dax_error_category: Optional[str]
    repair_attempts_by_category: Dict[str, int]
    transport_attempts: int
    plan_regenerations: int
    catalog_refresh_count: int
    empty_diagnostics: int
    previous_dax_hashes: List[str]
    # Values mirror the SQL feedback_type contract for the shared formatter:
    #   local_repair | regenerate | replan | refresh_catalog | clarify |
    #   transport_retry | exhausted
    dax_feedback_action: Optional[str]

    # ── Needs-connect (delegated OAuth) ─────────────────────────────────────
    needs_connect: bool
    # Set by the execution node when the flow must stop immediately (connect
    # required / misconfiguration) rather than entering the repair/retry loop.
    dax_terminal: bool


__all__ = ["DaxAgentState"]
