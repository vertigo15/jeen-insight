"""Pydantic request/response schemas for the public API.

Kept as a single module because the schemas are small and frequently
referenced in pairs (request + response). Splitting per-feature would create
a lot of one-class files without buying much.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


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
    # Per-node execution trace. Each entry: {node, elapsed_ms, icon, type, detail, ...}
    trace: Optional[List[Dict[str, Any]]] = None
    # Result analysis from the inline eval node, present only when the caller
    # asked for it (eval_analytics=true). Named to match
    # GenerateInsightsResponse so both endpoints expose the same shape; the
    # browser uses the /generate-insights/stream SSE path instead.
    findings: Optional[List[str]] = None
    suggestions: Optional[List[str]] = None
    followups: Optional[List[str]] = None
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


class ColumnInfo(BaseModel):
    name: str
    type: str


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
    role: str  # "user" | "assistant"
    content: str


class EditChartRequest(BaseModel):
    connection: str
    instruction: str
    current_config: Dict[str, Any]
    columns: List[ColumnInfo]
    column_names: List[str]
    sample_data: List[List[Any]]
    recent_messages: Optional[List[ChatMessage]] = None


class DerivedSeriesSpec(BaseModel):
    operator: str
    source_column: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    label: Optional[str] = None


class EditChartResponse(BaseModel):
    chart_config: Dict[str, Any]
    chart_type: str
    derived_series: List[DerivedSeriesSpec] = []
    notes: Optional[str] = None
    out_of_scope: bool = False
    prompt: Optional[str] = None
    system_message: Optional[str] = None


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


# ----------------------------------------------------------------------
# History / feedback
# ----------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    query_id: UUID
    user_id: Optional[str] = None
    feedback: str
    corrected_sql: Optional[str] = None
    notes: Optional[str] = None


class PinQuestionRequest(BaseModel):
    connection: str
    user_id: str = "default"
    question: str


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
    insights: Optional[Dict[str, Any]] = None


class UpdateSavedAnalysisRequest(BaseModel):
    user_id: Optional[str] = None
    name: Optional[str] = None
    chart_spec: Optional[Dict[str, Any]] = None
    chart_config: Optional[Dict[str, Any]] = None


# ----------------------------------------------------------------------
# Autocomplete
# ----------------------------------------------------------------------
class SuggestQuestionsRequest(BaseModel):
    connection: str
    partial: str
    recent_questions: Optional[List[str]] = None
    table_names: Optional[List[str]] = None
