"""/api/generate-insights routes an ML turn through the algorithm-aware narrator.

When the request carries an ``analysis`` envelope (or a query_id whose turn has a
persisted analysis), findings + follow-ups come from the engine facts and the
summary is grounded in them — not the row-based eval path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.analysis.narration import narrate

_ENV = {
    "skill": "anomaly_detection",
    "headline": "3 of 26 months fall outside the expected range.",
    "caveats": [],
    "params": {"series": {"grain": "month"}},
    "facts": {
        "skill": "anomaly_detection", "measure": "SalesAmount", "grain": "month", "n_points": 26, "n_flagged": 1,
        "headline_detail": "Largest deviation: 2007-02 came in 31% above expectation.",
        "flagged": [{"period": "2007-02", "actual": 180348, "expected": 141000, "deviation_pct": 0.279, "direction": "above"}],
    },
}


class _FakeLLM:
    async def generate(self, messages, **kw):
        return {"content": '{"summary": "Sales spiked in Feb 2007.", "insights": ["ignored"], "follow_up_questions": ["ignored?"]}'}


def test_generate_insights_takes_ml_branch(client, fake_state, monkeypatch):
    from src.api.routes import insights as insights_mod

    monkeypatch.setattr(
        insights_mod, "resolve_agent",
        AsyncMock(return_value=SimpleNamespace(llm=_FakeLLM())),
    )
    # No query_id: ownership check + history logging are skipped, so the test
    # exercises the narrator branch in isolation.
    resp = client.post("/api/generate-insights", json={
        "connection": "aw",
        "question": "flag anomalies in SalesAmount",
        "analysis": _ENV,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected = narrate(_ENV)
    assert body["summary"] == "Sales spiked in Feb 2007."
    assert body["findings"] == expected.findings  # deterministic narrator, not the LLM list
    assert body["followups"] == expected.followups
    assert any("2007-02" in f for f in body["findings"])


def test_generate_insights_ignores_analysis_without_a_skill(client, fake_state, monkeypatch):
    """A body ``analysis`` with no skill must NOT hijack the ordinary SQL path."""
    from src.api.routes import insights as insights_mod

    called = {"eval": False}

    async def _fake_resolve(_conn):
        return SimpleNamespace(llm=_FakeLLM())

    monkeypatch.setattr(insights_mod, "resolve_agent", AsyncMock(side_effect=_fake_resolve))
    # No skill in analysis and no rows/sql → dataset resolution raises 409 cache_miss,
    # proving the ML branch was skipped (it would have returned 200 instead).
    resp = client.post("/api/generate-insights", json={
        "connection": "aw",
        "question": "top products",
        "analysis": {"facts": {}},  # no skill
    })
    assert resp.status_code == 409  # cache_miss from the non-ML path
