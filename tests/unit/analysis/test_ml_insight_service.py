"""Hybrid ML insight service: deterministic findings/follow-ups + grounded LLM summary."""

from __future__ import annotations

import pytest

from src.agent.ml_insight_service import generate_ml_insights
from src.analysis.narration import narrate

_ENV = {
    "skill": "anomaly_detection",
    "headline": "3 of 26 months fall outside the expected range.",
    "caveats": ["92% of history sits inside the band; sensitivity 0.95 expects about 95%."],
    "params": {"series": {"grain": "month"}},
    "validation": {"metric": "WAPE", "value": 0.064, "band": "good"},
    "facts": {
        "skill": "anomaly_detection", "measure": "SalesAmount", "grain": "month", "n_points": 26, "n_flagged": 1,
        "sensitivity": 0.95, "seasonal_periods": [12],
        "headline_detail": "Largest deviation: 2007-02 came in 31% above expectation.",
        "flagged": [{"period": "2007-02", "actual": 180348, "expected": 141000, "deviation_pct": 0.279, "direction": "above"}],
    },
}


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def generate(self, messages, **kw):
        self.calls.append(messages)
        return {"content": self.content}


async def test_hybrid_summary_from_llm_findings_from_narrator():
    llm = _FakeLLM('{"summary": "Sales in Feb 2007 ran well above trend.", "insights": ["LLM-i"], "follow_up_questions": ["LLM-q?"]}')
    res = await generate_ml_insights(analysis=_ENV, question="flag anomalies", row_count=26, llm_service=llm, prompt_cache=None)

    expected = narrate(_ENV)
    assert res["summary"] == "Sales in Feb 2007 ran well above trend."  # from the LLM
    assert res["findings"] == expected.findings  # deterministic, authoritative
    assert res["followups"] == expected.followups
    # Grounding: the engine facts reached the prompt (so the LLM restates, not invents).
    prompt = llm.calls[0][1]["content"]
    assert "SalesAmount" in prompt and "2007-02" in prompt


async def test_summary_falls_back_to_headline_on_unparseable_llm():
    llm = _FakeLLM("not json at all")
    res = await generate_ml_insights(analysis=_ENV, question="q", llm_service=llm, prompt_cache=None)
    assert res["summary"] == _ENV["headline"]
    assert res["findings"] == narrate(_ENV).findings  # deterministic path is unaffected


async def test_no_llm_service_still_returns_deterministic_findings():
    res = await generate_ml_insights(analysis=_ENV, question="q", llm_service=None, prompt_cache=None)
    assert res["summary"] == _ENV["headline"]
    assert res["findings"] == narrate(_ENV).findings
    assert res["followups"] == narrate(_ENV).followups


async def test_unregistered_skill_falls_back_to_llm_insights():
    env = {"skill": "made_up_skill", "facts": {"skill": "made_up_skill"}, "headline": "H", "params": {}, "caveats": []}
    llm = _FakeLLM('{"summary": "S", "insights": ["only-llm"], "follow_up_questions": ["only-llm-q?"]}')
    res = await generate_ml_insights(analysis=env, question="q", llm_service=llm, prompt_cache=None)
    # The generic narrator has nothing, so the LLM's own list fills the gap.
    assert res["findings"] == ["only-llm"]
    assert res["followups"] == ["only-llm-q?"]
