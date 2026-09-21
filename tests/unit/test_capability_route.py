"""The `capability` route answers questions about the assistant itself.

Covers: the route is valid and passes the ML gate untouched, the router graph
sends it to the capability node, the skill catalog is built from the registry,
and the node renders the prompt (with the catalog + method guidance) and returns
a natural-language answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.langgraph_agent.graph import _route_from_router
from src.agent.langgraph_agent.nodes.capability import (
    _SKILL_EXAMPLES,
    _SKILL_WHEN,
    _skill_algorithm,
    build_skill_catalog,
    make_capability_answer,
)
from src.agent.langgraph_agent.nodes.router import (
    ROUTE_SOURCE_CAPABILITY_CUE,
    _VALID_ROUTES,
    detect_capability_intent,
    explain_routing,
    resolve_ml_route,
)
from src.agent.langgraph_agent.prompt_loader import PromptLoader


def test_capability_is_a_valid_route():
    assert "capability" in _VALID_ROUTES


def test_capability_route_goes_to_capability_answer_node():
    assert _route_from_router({"route": "capability"}) == "capability_answer"


# ── detect_capability_intent: strict, deterministic ────────────────────────────

@pytest.mark.parametrize("q", [
    "which ML models can I use?",
    "what ml models can i use",
    "what ml model can we use",                  # first-person plural subject
    "which ML models can we use?",
    "what can you do?",
    "what analyses can you run?",
    "which machine learning algorithms are available?",
    "can I choose the ML skill?",
    "what algorithms do you support?",
])
def test_detect_capability_intent_positive(q):
    assert detect_capability_intent(q) is True


@pytest.mark.parametrize("q", [
    "which product models sold best in 2008?",  # "model" is a data word
    "sales by product model",
    "what models can we use for the campaign?",  # "model" alone, no ML qualifier
    "what's the weather in Paris tomorrow?",
    "forecast revenue next quarter",             # a skill name is not a qualifier
    "can we forecast revenue next quarter?",     # a data/analysis request, not capability
    "what did you say about those models?",      # a from_memory follow-up
    "total sales by region last month",
    "",
])
def test_detect_capability_intent_negative(q):
    assert detect_capability_intent(q) is False


@pytest.mark.parametrize("enabled", [True, False])
def test_explain_routing_predicts_capability(enabled):
    # The dry-run prediction must agree with the node's deterministic cue (no
    # drift), regardless of the ML gate.
    p = explain_routing("which ML models can I use?", ml_skills_enabled=enabled)
    assert p["would_route"] == "capability"
    assert p["source"] == ROUTE_SOURCE_CAPABILITY_CUE


def test_ml_gate_passes_capability_through_untouched():
    # A capability question is neither SQL nor ML; the gate must not rewrite it.
    for enabled in (True, False):
        out = resolve_ml_route("capability", "about the app", "what can you do?",
                               ml_skills_enabled=enabled, analysis_override=None)
        assert out["route"] == "capability"


def test_presentation_dicts_cover_every_skill():
    """The presentation dicts must stay in lockstep with the registry so a new
    skill can never fall back to incomplete output."""
    from src.analysis.contracts import SKILLS

    assert set(_SKILL_WHEN) == set(SKILLS)
    assert set(_SKILL_EXAMPLES) == set(SKILLS)
    # Every skill resolves to a non-empty algorithm (method-derived or mapped).
    for name in SKILLS:
        assert _skill_algorithm(name), name


def test_skill_catalog_lists_every_registered_skill():
    from src.analysis.contracts import SKILLS

    catalog = build_skill_catalog()
    for spec in SKILLS.values():
        assert spec.title in catalog
    # And it names the method-changing example the users asked about.
    assert "anomaly" in catalog.lower()
    # Grouped by category, with the algorithm and a "when to use" per skill.
    assert "**Time series**" in catalog
    assert "**Entity / row-level**" in catalog
    assert "MSTL" in catalog and "Auto ARIMA" in catalog and "K-means" in catalog
    assert "Use when" in catalog
    assert "aggregates only" in catalog and "row-level, capped" in catalog


async def test_capability_node_answers_from_the_prompt():
    llm = MagicMock()
    captured = {}

    async def generate(messages, **kw):
        captured["system"] = messages[0]["content"]
        return {"content": "Jeen writes SQL for you and can run forecasts and anomaly checks.",
                "usage": {"total_tokens": 20}}

    llm.generate = AsyncMock(side_effect=generate)
    node = make_capability_answer(llm, PromptLoader())

    out = await node({
        "question": "can I change the anomaly model to 3-sigma?",
        "connection_display_name": "AdventureWorksDW",
        "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {}, "node_prompts": {},
    })
    assert out["answer"].startswith("Jeen writes SQL")
    # The rendered prompt carried the app description, the skill catalog and the
    # method-change guidance (so the model can answer the 3-sigma question).
    sys = captured["system"]
    assert "text-to-SQL" in sys and "Anomaly detection" in sys and "3-sigma" in sys
    assert "AdventureWorksDW" in sys
    # The enriched catalog (category headers + algorithm + when-to-use) reaches
    # the model, so it can list algorithm / category / when to use.
    assert "**Time series**" in sys and "Use when" in sys and "MSTL" in sys


async def test_capability_node_falls_back_when_llm_fails():
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("boom"))
    node = make_capability_answer(llm, PromptLoader())
    out = await node({"question": "what do you do?", "connection_display_name": "DB",
                      "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {}, "node_prompts": {}})
    assert "Jeen Insights" in out["answer"] and "3-sigma" in out["answer"]
