"""State schema for the Jeen Insights LangGraph subgraphs.

InsightsState
-------------
Carries all data that flows through the insights eval subgraph. Keys are
split into three groups:

  - Inputs  — set by the caller before invoking the graph.
  - Outputs — written by the eval node after the LLM call.
  - Error   — set if any node raises; downstream nodes should check this.

All keys use ``total=False`` so callers only need to supply the inputs;
LangGraph merges partial dicts returned by each node into the state.
"""
from __future__ import annotations

from typing import Any, List, Optional
from typing_extensions import TypedDict


class InsightsState(TypedDict, total=False):
    # ── Inputs ──────────────────────────────────────────────────────────
    #: Original user question forwarded to the eval node.
    question: str
    #: SQL that was executed (empty string if not available).
    sql: str
    #: Raw result rows as a list of dicts (keys = column names).
    results: List[Any]
    #: Total number of rows returned by the query.
    row_count: int

    # ── Eval outputs ────────────────────────────────────────────────────
    #: True when the result set genuinely answers the original question.
    answers_intent: bool
    #: One-to-two sentence business summary of what the data shows.
    summary: str
    #: 2–3 key insights backed by specific numbers from the data.
    insights: List[str]
    #: 3–5 short follow-up questions (≤ 15 words each, ending with "?").
    follow_up_questions: List[str]

    # ── Error tracking ───────────────────────────────────────────────────
    error: Optional[str]
