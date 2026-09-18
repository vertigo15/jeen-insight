"""The `capability` route answers questions about the assistant itself.

Covers: the route is valid and passes the ML gate untouched, the router graph
sends it to the capability node, the skill catalog is built from the registry,
and the node renders the prompt (with the catalog + method guidance) and returns
a natural-language answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.agent.langgraph_agent.graph import _route_from_router
from src.agent.langgraph_agent.nodes.capability import build_skill_catalog, make_capability_answer
from src.agent.langgraph_agent.nodes.router import _VALID_ROUTES, resolve_ml_route
from src.agent.langgraph_agent.prompt_loader import PromptLoader


def test_capability_is_a_valid_route():
    assert "capability" in _VALID_ROUTES


def test_capability_route_goes_to_capability_answer_node():
    assert _route_from_router({"route": "capability"}) == "capability_answer"


def test_ml_gate_passes_capability_through_untouched():
    # A capability question is neither SQL nor ML; the gate must not rewrite it.
    for enabled in (True, False):
        out = resolve_ml_route("capability", "about the app", "what can you do?",
                               ml_skills_enabled=enabled, analysis_override=None)
        assert out["route"] == "capability"


def test_skill_catalog_lists_every_registered_skill():
    from src.analysis.contracts import SKILLS

    catalog = build_skill_catalog()
    for spec in SKILLS.values():
        assert spec.title in catalog
    # And it names the method-changing example the users asked about.
    assert "anomaly" in catalog.lower()


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


async def test_capability_node_falls_back_when_llm_fails():
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("boom"))
    node = make_capability_answer(llm, PromptLoader())
    out = await node({"question": "what do you do?", "connection_display_name": "DB",
                      "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {}, "node_prompts": {}})
    assert "Jeen Insights" in out["answer"] and "3-sigma" in out["answer"]
