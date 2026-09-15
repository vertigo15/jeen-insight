"""ML-vs-SQL routing is observable and deterministic.

Two layers are covered:

* :func:`resolve_ml_route` / :func:`explain_routing` — the pure rule shared by
  the router node and the dry-run endpoint, so the two can never drift.
* ``GET /api/analysis/routing`` — the dry-run endpoint the e2e suite asserts
  against to know, without an LLM call, when a question triggers ML vs SQL.
"""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent.nodes.router import (
    ROUTE_SOURCE_CUE,
    ROUTE_SOURCE_DISABLED,
    ROUTE_SOURCE_GREETING,
    ROUTE_SOURCE_LLM,
    ROUTE_SOURCE_OVERRIDE,
    explain_routing,
    resolve_ml_route,
)


# ── resolve_ml_route: the run-time rule ────────────────────────────────────────

def test_keyword_cue_upgrades_sql_to_ml():
    """A strong cue ("forecast") makes ML the outcome even when the LLM says SQL."""
    d = resolve_ml_route(
        "needs_query", "looks like a lookup", "Forecast profit for the next 8 weeks",
        ml_skills_enabled=True, analysis_override=None,
    )
    assert d["route"] == "needs_analysis"
    assert d["source"] == ROUTE_SOURCE_CUE
    assert d["skill_hint"] == "forecast"


def test_llm_analysis_kept_without_cue():
    """When the LLM picks ML and nothing blocks it, the source is the LLM."""
    d = resolve_ml_route(
        "needs_analysis", "wants a model", "Break down what moved margin last quarter",
        ml_skills_enabled=True, analysis_override=None,
    )
    assert d["route"] == "needs_analysis"
    assert d["source"] == ROUTE_SOURCE_LLM


def test_ml_disabled_collapses_to_sql():
    d = resolve_ml_route(
        "needs_analysis", "wants a forecast", "Forecast revenue",
        ml_skills_enabled=False, analysis_override=None,
    )
    assert d["route"] == "needs_query"
    assert d["source"] == ROUTE_SOURCE_DISABLED


def test_request_override_forces_sql():
    """`analysis=false` ("Answer with SQL instead") wins over an ML classification."""
    d = resolve_ml_route(
        "needs_analysis", "wants a forecast", "Forecast revenue",
        ml_skills_enabled=True, analysis_override=False,
    )
    assert d["route"] == "needs_query"
    assert d["source"] == ROUTE_SOURCE_OVERRIDE


def test_plain_lookup_stays_sql():
    d = resolve_ml_route(
        "needs_query", "simple aggregate", "Total sales by region last month",
        ml_skills_enabled=True, analysis_override=None,
    )
    assert d["route"] == "needs_query"
    assert d["source"] == ROUTE_SOURCE_LLM
    assert d["skill_hint"] is None


# ── explain_routing: the dry-run prediction ────────────────────────────────────

@pytest.mark.parametrize("q", ["hi", "hello", "good morning"])
def test_greeting_predicted(q):
    p = explain_routing(q, ml_skills_enabled=True)
    assert p["would_route"] == "greeting"
    assert p["source"] == ROUTE_SOURCE_GREETING


def test_cue_predicted_as_ml():
    p = explain_routing("Is anything unusual in weekly profit?", ml_skills_enabled=True)
    assert p["would_route"] == "needs_analysis"
    assert p["skill_hint"] == "anomaly_detection"


def test_no_cue_defers_to_router_llm():
    p = explain_routing("Show me total sales by region", ml_skills_enabled=True)
    assert p["would_route"] == "router_decides"
    assert p["source"] == ROUTE_SOURCE_LLM


def test_disabled_predicts_sql_even_with_cue():
    p = explain_routing("Forecast revenue next quarter", ml_skills_enabled=False)
    assert p["would_route"] == "needs_query"
    assert p["source"] == ROUTE_SOURCE_DISABLED


def test_override_predicts_sql():
    p = explain_routing("Forecast revenue", ml_skills_enabled=True, analysis_override=False)
    assert p["would_route"] == "needs_query"
    assert p["source"] == ROUTE_SOURCE_OVERRIDE


# ── The dry-run endpoint ───────────────────────────────────────────────────────

def test_routing_endpoint_requires_auth(anon_client):
    r = anon_client.get("/api/analysis/routing", params={"q": "forecast sales"})
    assert r.status_code == 401


def test_routing_endpoint_reports_ml_for_cue(client, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "ML_SKILLS_ENABLED", True)
    r = client.get("/api/analysis/routing", params={"q": "Forecast profit for the next 8 weeks"})
    assert r.status_code == 200
    body = r.json()
    assert body["would_route"] == "needs_analysis"
    assert body["skill_hint"] == "forecast"
    assert "contract_version" in body


def test_routing_endpoint_reports_sql_for_lookup(client, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "ML_SKILLS_ENABLED", True)
    r = client.get("/api/analysis/routing", params={"q": "Total sales by region"})
    assert r.status_code == 200
    assert r.json()["would_route"] == "router_decides"


def test_routing_endpoint_honours_disabled(client, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "ML_SKILLS_ENABLED", False)
    r = client.get("/api/analysis/routing", params={"q": "Forecast revenue next quarter"})
    assert r.status_code == 200
    body = r.json()
    assert body["would_route"] == "needs_query"
    assert body["source"] == ROUTE_SOURCE_DISABLED


def test_routing_endpoint_honours_override(client, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "ML_SKILLS_ENABLED", True)
    r = client.get("/api/analysis/routing", params={"q": "Forecast revenue", "analysis": "false"})
    assert r.status_code == 200
    assert r.json()["source"] == ROUTE_SOURCE_OVERRIDE
