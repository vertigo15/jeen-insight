"""Pydantic request/response schemas for the public API.

Kept as a single module because the schemas are small and frequently
referenced in pairs (request + response). Splitting per-feature would create
a lot of one-class files without buying much.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.chart_operations import (
    ChartOperation,
    SetBindingOperation,
    SetChartTypeOperation,
)

# Operations the deterministic rebuild endpoint accepts: anything that changes
# the chart *structure* and therefore needs the full cached result set.
ChartRebuildOperation = Annotated[
    Union[SetBindingOperation, SetChartTypeOperation],
    Field(discriminator="op"),
]


# ----------------------------------------------------------------------
# Query / data exploration
# ----------------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str
    connection: str
    session_id: Optional[UUID] = None
    user_context: Optional[Dict[str, Any]] = None
    # User-overridable runtime preferences. None = use server defaults.
    # Bounds are server-enforced so the UI can't widen them.
    limit: Optional[int] = Field(default=None, ge=1, le=10_000)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Per-request LangGraph overrides.
    # eval_analytics: None = use server default (EVAL_ANALYTICS_ENABLED);
    #   False = skip fused_eval_analytics so the caller can run it separately
    #   (UI shows table first, then requests insights in a background call).
    eval_analytics: Optional[bool] = None
    # llm_timeout: override LLM_TIMEOUT_SECONDS for this single request.
    llm_timeout: Optional[int] = Field(default=None, ge=0, le=300)
    # analysis: False = answer with SQL even if the question reads like an ML
    # request ("Answer with SQL instead" on a confirm card). None = server default.
    analysis: Optional[bool] = None
    # The user's answers to earlier filter clarifications in this session
    # ([{literal, table, column, any}]): which column a value belongs to, or
    # the exact value they meant. Honoured by the grounder before asking again.
    filter_choices: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=20)


class QueryResponse(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    question: str
    query_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    sql: Optional[str]
    results: Optional[Dict[str, Any]]
    # answer may be a plain string OR a fragment array [{"t": "...", "hl": "..."}, …]
    # — same format as insights.summary. Do NOT coerce to str.
    answer: Any = None
    prompt: Optional[Dict[str, Any]] = None
    error: Optional[str]
    # Per-request metrics surfaced to the UI:
    #   - input_tokens / output_tokens / total_tokens: from Azure OpenAI usage
    #   - llm_latency_ms: total time spent inside llm.generate (not TTFT;
    #     real TTFT requires streaming, which we don't do today)
    metrics: Optional[Dict[str, Any]] = None
    # Why this answer took the ML skill path or the text-to-SQL path:
    #   {route, path (ml|sql|…), source, reason, skill}. Declared here so
    #   Pydantic keeps it and the UI/tests can read it without the trace.
    routing: Optional[Dict[str, Any]] = None
    # When the SQL-vs-ML choice was ambiguous, the router asks instead of guessing:
    # {message, confidence, skill_hint}. The UI renders the two choices ("Run the
    # analysis" / "Answer directly"). Declared so Pydantic keeps it in the contract.
    route_clarification: Optional[Dict[str, Any]] = None
    # Filter grounding provenance {resolved, unverified, assumptions} and, when
    # the grounder had to ask which column / value was meant, a structured
    # question {kind, literal, message, options, allow_any, allow_other}.
    filters: Optional[Dict[str, Any]] = None
    filter_clarification: Optional[Dict[str, Any]] = None
    # history_lookup route ("did I ask about X last week?"): the matching past
    # turns [{query_id, session_id, question, answer, created_at}] so the UI can
    # link back to them. Declared so Pydantic keeps it in the contract.
    history_matches: Optional[List[Dict[str, Any]]] = None
    # Per-node execution trace. Each entry: {node, elapsed_ms, icon, type, detail, ...}
    trace: Optional[List[Dict[str, Any]]] = None
    # True on an answer streamed before its history writes ran; the stream's
    # final ``enrichment`` (``trace_tail``) follows once they have. Until then
    # the turn row is not final, so the UI does not offer Favorite.
    saving: Optional[bool] = None
    # Result analysis from the inline eval node, present only when the caller
    # asked for it (eval_analytics=true). Named to match
    # GenerateInsightsResponse so both endpoints expose the same shape; the
    # browser uses the /generate-insights/stream SSE path instead.
    findings: Optional[List[str]] = None
    suggestions: Optional[List[str]] = None
    followups: Optional[List[str]] = None
    # A successful query that returned zero rows. The UI shows an explicit
    # "no records" state instead of a silent empty grid; ``empty_hint`` is a
    # short likely-cause note (may arrive later via /api/empty-result-hint).
    empty_result: Optional[bool] = None
    empty_hint: Optional[str] = None
    # Opaque handle to a durable, server-held encrypted snapshot of this result.
    # Present only when the connector platform is enabled; used as the sole
    # authorization source for outbound actions (send/share).
    result_handle: Optional[str] = None
    # At most ONE agent-proposed tool action for this turn (Phase 3). Present only
    # when agent tool-calling is enabled AND the user's question expressed explicit
    # intent. The UI renders a confirm card and drives preview -> execute; the
    # proposal is already recorded server-side (origin=agent) and hash-bound.
    tool_proposal: Optional[Dict[str, Any]] = None
    # Delegated-OAuth data sources (Power BI text-to-DAX) set these when the
    # signed-in user must connect or reconnect the provider before the query can
    # run. Undeclared fields are dropped by Pydantic, so the UI connect prompt
    # only works while these stay part of the response contract.
    needs_connect: Optional[bool] = None
    connect_provider: Optional[str] = None
    # ── ML skills ──────────────────────────────────────────────────────────
    # status: "confirm" | "clarify" | "blocked" when the graph stopped to ask
    # (the proposal carries the chips / options / guard results); None or
    # "completed" for an ordinary answer.
    status: Optional[str] = None
    proposal: Optional[Dict[str, Any]] = None
    # ResultEnvelope.artifact_view(): method, params, validation, guard
    # results, engine, provenance, facts — never the rows (those are results).
    analysis: Optional[Dict[str, Any]] = None
    # True when a guard was overridden; must travel into history, pins, exports.
    low_confidence: Optional[bool] = None
    # Set on turns created by /api/analysis/run|rerun so the UI can show the
    # parameter diff against the parent.
    parent_query_id: Optional[UUID] = None


# ----------------------------------------------------------------------
# ML skills (confirm / run / re-run)
# ----------------------------------------------------------------------
class AnalysisRunRequest(BaseModel):
    """Resume a persisted proposal (confirm card, clarification pick, guard exit).

    The proposal id is the only source of ``{skill, params}``; ``params_patch``
    is an allowlisted partial override validated against the skill's model.
    ``override_guards`` runs past a refused guard and marks the result
    low-confidence.
    """

    connection: str
    proposal_id: UUID
    session_id: Optional[UUID] = None
    params_patch: Optional[Dict[str, Any]] = None
    override_guards: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    # Persist "don't ask again" for this skill on this connection.
    remember: bool = False
    eval_analytics: Optional[bool] = None
    llm_timeout: Optional[int] = Field(default=None, ge=0, le=300)


class AnalysisRerunRequest(BaseModel):
    """Re-run a completed ML turn with adjusted parameters, appending a child turn.

    Either a structured ``params_patch`` (from the chip row) or a natural-language
    ``instruction`` ("weekly instead of daily", "flag fewer") which the planner
    turns into a patch server-side. Never mutates the parent turn.
    """

    connection: str
    parent_query_id: UUID
    session_id: UUID
    instruction: Optional[str] = Field(default=None, max_length=500)
    params_patch: Optional[Dict[str, Any]] = None
    override_guards: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    eval_analytics: Optional[bool] = None
    llm_timeout: Optional[int] = Field(default=None, ge=0, le=300)


class SkillPrefPatch(BaseModel):
    connection: str
    skill: str
    remember: bool = True


class AnalysisAccuracyRequest(BaseModel):
    """Compare a captured forecast turn with the actuals that have since arrived."""

    connection: str
    query_id: UUID


class AnalysisChartRequest(BaseModel):
    """Deterministic band chart for an ML result — no LLM involved.

    The rows come from the server-side result cache (keyed by the verified
    user + connection + query_id); ``results`` is the cache-miss fallback the
    client re-sends. ``chart_spec`` is the envelope's role-based spec.
    """

    connection: str
    query_id: str
    chart_spec: Dict[str, Any]
    results: Optional[Dict[str, Any]] = None


class ColumnInfo(BaseModel):
    name: str = Field(max_length=256)
    type: str = Field(max_length=32)


# ----------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------
class GenerateChartRequest(BaseModel):
    connection: str
    # Cache coordinates: when the query result is still cached server-side we
    # build the chart from the full rows and the client can omit columns/data.
    query_id: Optional[str] = None
    user_id: Optional[str] = None
    # The natural-language question that produced the data — used as
    # "instructions" so the LLM can choose a chart that matches user intent.
    question: Optional[str] = None
    # Columns/data are optional: present only on a cache miss (the client
    # re-sends them as the fallback). all_data carries the FULL rows.
    columns: Optional[List[ColumnInfo]] = None
    column_names: Optional[List[str]] = None
    sample_data: Optional[List[List[Any]]] = None
    all_data: Optional[List[List[Any]]] = None
    chart_type: Optional[str] = "auto"
    x_column: Optional[str] = None
    y_column: Optional[str] = None
    series_column: Optional[str] = None
    # Explicit map bindings. These are optional so the LLM can choose from the
    # geo-role candidates on the first render; user changes always override it.
    location_column: Optional[str] = None
    latitude_column: Optional[str] = None
    longitude_column: Optional[str] = None
    location_parts: Optional[Dict[str, str]] = None
    value_column: Optional[str] = None
    value2_column: Optional[str] = None
    aggregate: Optional[str] = None


class GenerateChartResponse(BaseModel):
    # chart_config is the legacy full ECharts config (fallback path). New
    # clients prefer chart_spec and build the option from the full dataset
    # client-side, so chart_config may be empty.
    chart_config: Dict[str, Any] = Field(default_factory=dict)
    chart_type: str
    # Compact visualization spec the client renders from the full result set.
    chart_spec: Optional[Dict[str, Any]] = None
    prompt: Optional[str] = None
    system_message: Optional[str] = None


class EnhanceChartRequest(BaseModel):
    connection: str
    columns: List[ColumnInfo]
    sample_data: List[List[Any]]
    chart_type: str
    current_config: Dict[str, Any]


class ChatMessage(BaseModel):
    role: str = Field(max_length=16)  # "user" | "assistant"
    content: str = Field(max_length=2_000)


class ChartManifestSeries(BaseModel):
    """Data-free description of one browser-held ECharts series."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    identity: Optional[str] = Field(default=None, max_length=256)
    id: Optional[str] = Field(default=None, max_length=256)
    name: Optional[str] = Field(default=None, max_length=256)
    type: str = Field(max_length=32)
    jeen_role: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("jeenRole", "jeen_role", "role"),
        serialization_alias="jeenRole",
        max_length=64,
    )
    stack: Optional[Union[str, bool]] = None
    x_axis_index: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("xAxisIndex", "x_axis_index"),
        serialization_alias="xAxisIndex",
        ge=0,
        le=8,
    )
    y_axis_index: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("yAxisIndex", "axisIndex", "y_axis_index"),
        serialization_alias="yAxisIndex",
        ge=0,
        le=8,
    )
    point_count: int = Field(
        default=0,
        validation_alias=AliasChoices("pointCount", "point_count"),
        serialization_alias="pointCount",
        ge=0,
        le=1_000_000,
    )
    hidden: bool = False
    data_summary: Dict[str, Any] = Field(default_factory=dict)
    style: Dict[str, Any] = Field(default_factory=dict)
    locked: bool = False

    @model_validator(mode="before")
    @classmethod
    def _reject_series_data(cls, value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = {"data", "dataset", "rows", "source", "values"}
            overlap = forbidden.intersection(value)
            if overlap:
                raise ValueError(
                    "Chart manifest series must not contain data fields: "
                    + ", ".join(sorted(overlap))
                )
        return value

    @model_validator(mode="after")
    def _derive_point_count(self) -> "ChartManifestSeries":
        count = self.data_summary.get("count")
        if self.point_count == 0 and isinstance(count, int) and not isinstance(count, bool):
            self.point_count = min(max(count, 0), 1_000_000)
        return self


class ChartManifest(BaseModel):
    """Compact v2 chart context. It deliberately has no rows or data arrays."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    series: List[ChartManifestSeries] = Field(default_factory=list, max_length=100)
    axes: Union[List[Dict[str, Any]], Dict[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "axes", "axis", "axis_summaries", "axisSummaries"
        ),
        serialization_alias="axes",
    )
    toggles: Dict[str, bool] = Field(default_factory=dict)
    format: Optional[Dict[str, Any]] = None
    overlays: List[Dict[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices("overlays", "derived"),
        serialization_alias="overlays",
        max_length=20,
    )
    chart_spec: Optional[Dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("chart_spec", "chartSpec", "spec"),
        serialization_alias="chart_spec",
    )
    columns: List[ColumnInfo] = Field(
        default_factory=list,
        validation_alias=AliasChoices("columns", "sql_columns", "sqlColumns"),
        serialization_alias="columns",
        max_length=256,
    )
    locks: Dict[str, Any] = Field(default_factory=dict)
    # Active reference lines / highlights so the model can remove or amend them.
    annotations: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_top_level_data(cls, value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = {
                "all_data",
                "chart_config",
                "current_config",
                "data",
                "dataset",
                "rows",
                "sample_data",
                "source",
                "values",
            }
            overlap = forbidden.intersection(value)
            if overlap:
                raise ValueError(
                    "Chart manifest must not contain config or data fields: "
                    + ", ".join(sorted(overlap))
                )
        return value

    @model_validator(mode="after")
    def _bounded_manifest(self) -> "ChartManifest":
        import json

        def reject_data_arrays(value: Any) -> None:
            if isinstance(value, dict):
                forbidden = {
                    "all_data",
                    "chart_config",
                    "current_config",
                    "data",
                    "dataset",
                    "rows",
                    "sample_data",
                    "source",
                }
                overlap = forbidden.intersection(value)
                if overlap:
                    raise ValueError(
                        "Chart manifest must not contain config or data fields: "
                        + ", ".join(sorted(overlap))
                    )
                for nested in value.values():
                    reject_data_arrays(nested)
            elif isinstance(value, list):
                for nested in value:
                    reject_data_arrays(nested)

        try:
            manifest = self.model_dump(by_alias=True, mode="json", exclude_none=True)
            reject_data_arrays(manifest)
            encoded = json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Chart manifest must contain finite JSON values.") from exc
        if len(encoded) > 96_000:
            raise ValueError("Chart manifest is too large.")
        return self


class EditChartRequest(BaseModel):
    connection: str = Field(max_length=255)
    instruction: str = Field(max_length=500)
    contract_version: Literal[1, 2] = 1
    chart_kind: Optional[Literal["sql", "ml_band", "ml_basic"]] = None
    chart_manifest: Optional[ChartManifest] = None
    current_config: Optional[Dict[str, Any]] = None
    columns: Optional[List[ColumnInfo]] = Field(default=None, max_length=256)
    column_names: Optional[List[str]] = Field(default=None, max_length=256)
    sample_data: Optional[List[List[Any]]] = Field(default=None, max_length=20)
    recent_messages: Optional[List[ChatMessage]] = Field(default=None, max_length=30)
    # OSM edits use the compact spec and rebuild deterministically from the
    # cached full dataset instead of letting the model alter point payloads.
    chart_spec: Optional[Dict[str, Any]] = None
    query_id: Optional[str] = Field(default=None, max_length=64)
    user_id: Optional[str] = Field(default=None, max_length=255)
    all_data: Optional[List[List[Any]]] = Field(default=None, max_length=10_000)
    # Existing overlays are session state. New clients can send them so a
    # style-only edit returns the complete active set; older clients may omit.
    active_derived_series: Optional[List["DerivedSeriesSpec"]] = Field(
        default=None, max_length=4
    )

    @field_validator("current_config")
    @classmethod
    def _bounded_current_config(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if value is None:
            return value
        from src.api.chart_edit_validation import assert_bounded_chart_config

        assert_bounded_chart_config(value)
        return value

    @field_validator("chart_spec")
    @classmethod
    def _bounded_chart_spec(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if value is not None:
            from src.api.chart_edit_validation import assert_bounded_chart_config

            assert_bounded_chart_config(value)
        return value

    @field_validator("column_names")
    @classmethod
    def _bounded_column_names(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return value
        if any(len(str(name)) > 256 for name in value):
            raise ValueError("Chart column names must be at most 256 characters.")
        return value

    @field_validator("sample_data", "all_data")
    @classmethod
    def _bounded_chart_rows(
        cls, value: Optional[List[List[Any]]]
    ) -> Optional[List[List[Any]]]:
        if value is None:
            return value
        import json

        max_bytes = 256_000 if len(value) <= 20 else 5_000_000
        try:
            size = len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Chart rows must contain finite JSON values.") from exc
        if size > max_bytes:
            raise ValueError("Chart row payload is too large.")
        return value

    @model_validator(mode="after")
    def _contract_fields(self) -> "EditChartRequest":
        if self.contract_version == 2:
            if self.chart_kind is None or self.chart_manifest is None:
                raise ValueError("contract_version=2 requires chart_kind and chart_manifest.")
            if any(
                value is not None
                for value in (self.current_config, self.sample_data, self.all_data)
            ):
                raise ValueError(
                    "contract_version=2 must not include current_config, sample_data, or rows."
                )
        elif (
            self.current_config is None
            or self.columns is None
            or self.column_names is None
            or self.sample_data is None
        ):
            raise ValueError(
                "Legacy chart edits require current_config, columns, column_names, and sample_data."
            )
        return self


class DerivedSeriesSpec(BaseModel):
    operator: str = Field(max_length=32)
    source_column: Optional[str] = Field(default=None, max_length=256)
    params: Optional[Dict[str, Any]] = None
    label: Optional[str] = Field(default=None, max_length=80)

    @field_validator("params")
    @classmethod
    def _bounded_params(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if value is not None:
            import json

            try:
                size = len(
                    json.dumps(value, allow_nan=False).encode("utf-8")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Derived-series params must be finite JSON.") from exc
            if size > 16_384:
                raise ValueError("Derived-series params are too large.")
        return value


class ChartEditError(BaseModel):
    code: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class EditChartResponse(BaseModel):
    chart_config: Dict[str, Any]
    chart_type: str
    derived_series: List[DerivedSeriesSpec] = Field(default_factory=list)
    notes: Optional[str] = None
    out_of_scope: bool = False
    chart_spec: Optional[Dict[str, Any]] = None
    view_commands: List[Dict[str, Any]] = Field(default_factory=list)
    rebuild_required: bool = False
    error: Optional[ChartEditError] = None
    prompt: Optional[str] = None
    system_message: Optional[str] = None


class EditChartV2Response(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[2] = 2
    operations: List[ChartOperation] = Field(default_factory=list, max_length=12)
    notes: Optional[str] = Field(default=None, max_length=300)
    out_of_scope: bool = False
    reason_code: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$"
    )


class EditChartRebuildRequest(BaseModel):
    """Deterministic SQL-chart rebuild for validated binding/type operations."""

    model_config = ConfigDict(extra="forbid", strict=True)

    connection: str = Field(min_length=1, max_length=255)
    query_id: str = Field(min_length=1, max_length=64)
    chart_kind: Literal["sql"] = "sql"
    chart_spec: Dict[str, Any]
    operations: List[ChartRebuildOperation] = Field(min_length=1, max_length=4)
    column_names: Optional[List[str]] = Field(default=None, max_length=256)
    all_data: Optional[List[List[Any]]] = Field(default=None, max_length=10_000)

    @field_validator("chart_spec")
    @classmethod
    def _bounded_rebuild_spec(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        from src.api.chart_edit_validation import assert_bounded_chart_config

        assert_bounded_chart_config(value)
        return value

    @field_validator("column_names")
    @classmethod
    def _bounded_rebuild_columns(
        cls, value: Optional[List[str]]
    ) -> Optional[List[str]]:
        if value is not None and any(
            not isinstance(name, str) or not name or len(name) > 256 for name in value
        ):
            raise ValueError("Fallback column names must be non-empty strings.")
        return value

    @field_validator("all_data")
    @classmethod
    def _bounded_rebuild_rows(
        cls, value: Optional[List[List[Any]]]
    ) -> Optional[List[List[Any]]]:
        if value is None:
            return value
        import json

        try:
            size = len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Fallback chart rows must contain finite JSON values.") from exc
        if size > 5_000_000:
            raise ValueError("Fallback chart rows are too large.")
        return value

    @model_validator(mode="after")
    def _complete_fallback(self) -> "EditChartRebuildRequest":
        if (self.column_names is None) != (self.all_data is None):
            raise ValueError("column_names and all_data must be supplied together.")
        return self


class EditChartRebuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chart_config: Dict[str, Any]
    chart_spec: Dict[str, Any]


# ----------------------------------------------------------------------
# Insights / profiling
# ----------------------------------------------------------------------
class GenerateInsightsRequest(BaseModel):
    connection: str
    # dataset is optional: when the result is cached server-side (matched by
    # query_id) the rows are pulled from the cache instead of the request body.
    dataset: Optional[Dict[str, Any]] = None
    question: str
    query_id: Optional[UUID] = None
    user_id: Optional[str] = None
    # SQL that produced the dataset — when provided the LangGraph eval node is
    # used instead of the legacy insight_service path.
    sql: Optional[str] = None
    # Serialized ML ResultEnvelope (skill/facts/params/caveats/headline). When
    # present the request is an ML turn: findings + follow-ups are built from the
    # engine facts and the summary is grounded in them. Falls back to the
    # persisted turn analysis (by query_id) when omitted.
    analysis: Optional[Dict[str, Any]] = None


class GenerateInsightsResponse(BaseModel):
    # summary may be a plain string OR a fragment array
    # [{"t": "text", "hl": "accent|pos|neg|num"}, …] — rendered client-side by
    # InsightsManager.renderText().  Do NOT coerce to str here.
    summary: Any
    findings: List[str]
    suggestions: List[str]       # recommended actions (0-2)
    followups: Optional[List[str]] = None  # clickable follow-up questions
    prompt: Optional[str] = None
    system_message: Optional[str] = None


class GenerateProfileRequest(BaseModel):
    # dataset is optional: when the result is cached server-side (matched by
    # connection+query_id) the rows are pulled from the cache.
    dataset: Optional[Dict[str, Any]] = None
    report_type: str = "ydata"
    connection: Optional[str] = None
    query_id: Optional[str] = None
    user_id: Optional[str] = None


class EmptyResultHintRequest(BaseModel):
    """Ask for a one-line likely-cause hint for a 0-row result.

    No rows are needed: the hint is reasoned from the question, the SQL and the
    catalog's column statistics. ``query_id`` (when present) is used only to
    verify the caller owns the turn.
    """

    connection: str
    query_id: Optional[UUID] = None
    question: Optional[str] = None
    sql: Optional[str] = None


# ----------------------------------------------------------------------
# History / feedback
# ----------------------------------------------------------------------
# The values ``chk_insights_user_feedback`` accepts on the turn row.
# ``catalog_gap`` is the "Report catalog gap" action on a failed turn
# (migration 033 widened the constraint to admit it); ``edited`` marks a
# rerun question. Answer-quality signals (thumbs, rating, message) go to
# ``insights_answer_feedback`` via ``AnswerFeedbackRequest`` (migration 035);
# thumbs are still accepted here for older UI builds.
FeedbackValue = Literal["thumbs_up", "thumbs_down", "edited", "catalog_gap"]


class FeedbackRequest(BaseModel):
    query_id: UUID
    user_id: Optional[str] = None
    feedback: FeedbackValue
    corrected_sql: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    # Optional: lets the route enrich the structured ``result_feedback`` log
    # event with the turn's ML method/validation (owner + connection bound).
    connection: Optional[str] = None


AnswerThumb = Literal["thumbs_up", "thumbs_down", "cleared"]
AnswerFeedbackType = Literal["general", "report_bug", "ui_bug", "other"]


class AnswerFeedbackRequest(BaseModel):
    """One answer-feedback event (``insights_answer_feedback``, append-only).

    A thumbs click sends just ``thumb``; the Give feedback dialog sends
    ``rating`` / ``feedback_type`` / ``message``. At least one of thumb,
    rating or message must be present — mirrors the table's CHECK.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    thumb: Optional[AnswerThumb] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    feedback_type: Optional[AnswerFeedbackType] = None
    message: Optional[str] = Field(default=None, max_length=4000)
    # Accepted for compatibility but not trusted: the stored source_key is
    # always the turn's own (the server copies it from the turn row).
    connection: Optional[str] = Field(default=None, max_length=255)

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _require_content(self) -> "AnswerFeedbackRequest":
        if self.thumb is None and self.rating is None and not self.message:
            raise ValueError("feedback needs a thumb, a rating or a message")
        return self


class AnswerFeedbackResponse(BaseModel):
    status: Literal["success"] = "success"
    feedback_id: str
    # The turn's current thumb after this event (None when cleared / unset),
    # so the UI can settle its pressed state from the server's view.
    thumb: Optional[Literal["thumbs_up", "thumbs_down"]] = None


class PinQuestionRequest(BaseModel):
    connection: str
    user_id: str = "default"
    question: str


# ----------------------------------------------------------------------
# Admin analytics (usage ledger, migration 036)
# ----------------------------------------------------------------------
# Metric definitions (also in src/analytics/usage_ledger.py and the UI tooltips):
#   active user  — distinct user with a query or analysis event that UTC day;
#                  logins are a separate series and never count towards DAU.
#   question     — a query event on a SQL/ML route; greetings, capability
#                  answers, clarifications and memory answers are text-only turns.
#   success rate — success / (success + error) over questions; refused (an ML
#                  guard declined) is reported separately.
#   thumbs       — CURRENT thumb per turn (newest thumb event, 'cleared' = none);
#                  *_events are the raw click counts.

class AnalyticsPeriod(BaseModel):
    questions: int = 0
    text_only_turns: int = 0
    active_users: int = 0
    active_connections: int = 0
    successes: int = 0
    errors: int = 0
    refused: int = 0
    success_rate: Optional[float] = None
    avg_graph_time_ms: Optional[float] = None
    total_tokens: int = 0
    logins: int = 0
    comments: int = 0
    feedback_events: int = 0
    avg_rating: Optional[float] = None
    thumbs_up: int = 0
    thumbs_down: int = 0
    thumbs_up_events: int = 0
    thumbs_down_events: int = 0


class AnalyticsOverview(BaseModel):
    days: int
    start: str
    end: str
    dau: int = 0
    wau: int = 0
    mau: int = 0
    current: AnalyticsPeriod
    previous: AnalyticsPeriod


class AnalyticsDayPoint(BaseModel):
    day: str
    active_users: int = 0
    questions: int = 0
    errors: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0
    logins: int = 0


class AnalyticsTimeseries(BaseModel):
    days: int
    points: List[AnalyticsDayPoint]


class AnalyticsTopUser(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    questions: int = 0
    analyses: int = 0
    success_rate: Optional[float] = None
    last_active: Optional[str] = None
    thumbs_up: int = 0
    thumbs_down: int = 0
    avg_rating: Optional[float] = None


class AnalyticsTopUsers(BaseModel):
    days: int
    items: List[AnalyticsTopUser]


class AnalyticsTopConnection(BaseModel):
    source_key: str
    questions: int = 0
    distinct_users: int = 0
    success_rate: Optional[float] = None
    avg_graph_time_ms: Optional[float] = None
    last_used: Optional[str] = None
    thumbs_up: int = 0
    thumbs_down: int = 0
    thumbs_down_rate: Optional[float] = None


class AnalyticsTopConnections(BaseModel):
    days: int
    items: List[AnalyticsTopConnection]


class AnalyticsFeedbackItem(BaseModel):
    id: int
    occurred_at: Optional[str] = None
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    source_key: Optional[str] = None
    thumb: Optional[str] = None
    rating: Optional[int] = None
    feedback_type: Optional[str] = None
    message: Optional[str] = None
    question: Optional[str] = None
    query_id: Optional[str] = None


class AnalyticsFeedbackFeed(BaseModel):
    days: int
    items: List[AnalyticsFeedbackItem]
    # Pass back as ``before`` to fetch the next (older) page; None at the end.
    next_before: Optional[int] = None


class AnalyticsSkillUsage(BaseModel):
    skill: str
    runs: int = 0
    ok: int = 0
    guard_failed: int = 0
    errors: int = 0
    avg_execution_ms: Optional[float] = None
    distinct_users: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0


class AnalyticsSkills(BaseModel):
    days: int
    items: List[AnalyticsSkillUsage]


class AnalyticsErrorGroup(BaseModel):
    error_type: str
    source_key: Optional[str] = None
    failures: int = 0


class AnalyticsFailingQuestion(BaseModel):
    question: str
    failures: int = 0
    distinct_users: int = 0
    distinct_connections: int = 0
    last_seen: Optional[str] = None


class AnalyticsErrors(BaseModel):
    days: int
    by_type: List[AnalyticsErrorGroup]
    top_failing_questions: List[AnalyticsFailingQuestion]


class OnboardingPatch(BaseModel):
    """Partial update to a user's onboarding (FTUE) state.

    Boolean flags stamp NOW() into their column when true; `checklist` is
    shallow-merged into the stored jsonb map. `user_id` is stamped by the Flask
    proxy from the signed session and re-verified server-side.
    """

    user_id: str
    welcome_seen: Optional[bool] = None
    tour_completed: Optional[bool] = None
    checklist_dismissed: Optional[bool] = None
    nudge_dismissed: Optional[bool] = None
    # Permanent opt-out of every FTUE surface ("Don't show this again").
    ftue_opted_out: Optional[bool] = None
    checklist: Optional[Dict[str, Any]] = None


class SaveAnalysisRequest(BaseModel):
    connection: str
    user_id: Optional[str] = None
    name: Optional[str] = None
    question: str
    sql: Optional[str] = None
    query_id: Optional[UUID] = None
    results: Dict[str, Any]
    chart_spec: Optional[Dict[str, Any]] = None
    chart_config: Optional[Dict[str, Any]] = None
    chart_state: Optional[Dict[str, Any]] = None
    insights: Optional[Dict[str, Any]] = None


class UpdateSavedAnalysisRequest(BaseModel):
    user_id: Optional[str] = None
    name: Optional[str] = None
    chart_spec: Optional[Dict[str, Any]] = None
    chart_config: Optional[Dict[str, Any]] = None
    chart_state: Optional[Dict[str, Any]] = None


# ----------------------------------------------------------------------
# Conversations (restore last conversation / browse previous ones)
# ----------------------------------------------------------------------
class ConversationSummary(BaseModel):
    id: str
    title: str
    source_key: str
    source_label: str
    # False when the connection no longer exists / is inactive; such
    # conversations open read-only (no re-run, no new turns).
    connection_available: bool = True
    turn_count: int = 0
    saved_answer_count: int = 0
    last_question: Optional[str] = None
    last_activity_at: Optional[str] = None
    created_at: Optional[str] = None


class ConversationTurn(BaseModel):
    """One turn without its blobs. Shaped so the browser can build a
    ``QueryResponse``-compatible ``turn.result`` once the artifact is loaded."""

    turn_id: str
    sequence_number: int
    question: str
    sql: Optional[str] = None
    # Stored value: success | error | timeout | syntax_error | pending. The UI
    # treats everything but success as a failed turn.
    execution_status: str
    result_kind: str  # table | text | error
    answer: Any = None
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    findings: Optional[List[str]] = None
    suggestions: Optional[List[str]] = None
    followups: Optional[List[str]] = None
    snapshot_status: str  # stored | too_large | pruned | not_applicable
    row_count: Optional[int] = None
    has_chart: bool = False
    has_rerunnable_query: bool = False
    created_at: Optional[str] = None
    snapshot_at: Optional[str] = None
    # ML skills: method/validation/guard details and the low-confidence flag
    # travel with the turn so a restored ML answer is indistinguishable from
    # a live one.
    analysis: Optional[Dict[str, Any]] = None
    low_confidence: bool = False
    is_favorite: bool = False
    # Current thumb from insights_answer_feedback (newest thumb event that is
    # not 'cleared'): thumbs_up | thumbs_down | None. Lets the answer card
    # render its pressed state after a reload.
    user_feedback: Optional[str] = None


class ConversationDetail(BaseModel):
    conversation: ConversationSummary
    # Newest page first from the server; the client reverses for display.
    turns: List[ConversationTurn] = Field(default_factory=list)
    # Smallest sequence_number on this page; pass as ``before=`` for older turns.
    next_cursor: Optional[int] = None


class ConversationList(BaseModel):
    items: List[ConversationSummary] = Field(default_factory=list)
    # Opaque "<last_activity_at>|<id>" cursor for the next page.
    next_cursor: Optional[str] = None


class FavoriteAnswer(BaseModel):
    conversation_id: str
    turn_id: str
    sequence_number: int
    conversation_title: str
    question: str
    answer: Any = None
    result_kind: str
    snapshot_status: str
    source_key: str
    source_label: str
    connection_available: bool = True
    created_at: Optional[str] = None
    favorited_at: Optional[str] = None


class FavoriteAnswerList(BaseModel):
    items: List[FavoriteAnswer] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class TurnArtifact(BaseModel):
    turn_id: str
    # Full result envelope {columns, rows, row_count, truncated?, cap?} or null.
    results: Optional[Dict[str, Any]] = None
    # Only ever non-null together with ``results``.
    chart_spec: Optional[Dict[str, Any]] = None
    chart_config: Optional[Dict[str, Any]] = None
    snapshot_status: str
    snapshot_at: Optional[str] = None
    analysis: Optional[Dict[str, Any]] = None
    low_confidence: bool = False


class RerunTurnResponse(TurnArtifact):
    """Fresh results for a turn. Rows are always returned even when they exceed
    the persistence caps (then ``snapshot_status`` is ``too_large``)."""

    execution_time_ms: Optional[int] = None


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ----------------------------------------------------------------------
# Autocomplete
# ----------------------------------------------------------------------
class SuggestQuestionsRequest(BaseModel):
    connection: str
    partial: str
    recent_questions: Optional[List[str]] = None
    table_names: Optional[List[str]] = None
