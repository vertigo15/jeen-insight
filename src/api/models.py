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
    answer: Optional[str] = None
    prompt: Optional[Dict[str, Any]] = None
    error: Optional[str]
    # Per-request metrics surfaced to the UI:
    #   - input_tokens / output_tokens / total_tokens: from Azure OpenAI usage
    #   - llm_latency_ms: total time spent inside llm.generate (not TTFT;
    #     real TTFT requires streaming, which we don't do today)
    metrics: Optional[Dict[str, Any]] = None
    # Per-node execution trace. Each entry: {node, elapsed_ms, icon, type, detail, ...}
    trace: Optional[List[Dict[str, Any]]] = None


class ColumnInfo(BaseModel):
    name: str
    type: str


# ----------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------
class GenerateChartRequest(BaseModel):
    connection: str
    columns: List[ColumnInfo]
    column_names: List[str]
    sample_data: List[List[Any]]
    all_data: Optional[List[List[Any]]] = None
    chart_type: Optional[str] = "auto"


class GenerateChartResponse(BaseModel):
    chart_config: Dict[str, Any]
    chart_type: str
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
    dataset: Dict[str, Any]
    question: str
    query_id: Optional[UUID] = None
    # SQL that produced the dataset — when provided the LangGraph eval node is
    # used instead of the legacy insight_service path.
    sql: Optional[str] = None


class GenerateInsightsResponse(BaseModel):
    summary: str
    findings: List[str]
    suggestions: List[str]
    prompt: Optional[str] = None
    system_message: Optional[str] = None


class GenerateProfileRequest(BaseModel):
    dataset: Dict[str, Any]
    report_type: str = "ydata"


# ----------------------------------------------------------------------
# History / feedback
# ----------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    query_id: UUID
    feedback: str
    corrected_sql: Optional[str] = None
    notes: Optional[str] = None


class PinQuestionRequest(BaseModel):
    connection: str
    user_id: str = "default"
    question: str


# ----------------------------------------------------------------------
# Autocomplete
# ----------------------------------------------------------------------
class SuggestQuestionsRequest(BaseModel):
    connection: str
    partial: str
    recent_questions: Optional[List[str]] = None
    table_names: Optional[List[str]] = None
