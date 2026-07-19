import pytest

from src.agent.langgraph_agent.nodes.router import make_fused_router


class _Prompts:
    async def arender(self, *_args, **_kwargs):
        return "router"

    async def model_override_for(self, *_args):
        return None


class _Llm:
    def __init__(self, content):
        self.content = content

    async def generate(self, **_kwargs):
        return {"content": self.content, "usage": {}}


@pytest.mark.asyncio
async def test_ambiguous_intent_creates_persistable_clarification():
    router = make_fused_router(
        _Llm(
            """{"route":"needs_query","reason":"ambiguous",
            "intent":{"metric":null,"dimensions":[],"filters":[],"time_range":null,
            "comparison":null,"referenced_result":null,"confidence":0.2,"assumptions":[]}}"""
        ),
        _Prompts(),
    )

    outcome = await router({"question": "show performance"})

    assert outcome["pending_clarification"]["intent"]["confidence"] == 0.2
    assert "which metric" in outcome["clarification"].lower()


@pytest.mark.asyncio
async def test_safe_default_is_disclosed_without_blocking_query():
    router = make_fused_router(
        _Llm(
            """{"route":"needs_query","reason":"uses default",
            "intent":{"metric":"revenue","dimensions":[],"filters":[],"time_range":"this year",
            "comparison":null,"referenced_result":null,"confidence":0.55,
            "assumptions":["Using calendar year to date."]}}"""
        ),
        _Prompts(),
    )

    outcome = await router({"question": "show revenue"})

    assert outcome["pending_clarification"] is None
    assert outcome["disclosed_assumptions"] == ["Using calendar year to date."]
