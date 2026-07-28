"""Unit tests for the dax_generator node + _extract_dax."""

from __future__ import annotations

import json

from src.agent.langgraph_agent_dax.nodes.dax_gen import _extract_dax, make_dax_generator
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader


class TestExtractDax:
    def test_from_tool_call(self):
        resp = {
            "tool_calls": [
                {"function": {"name": "run_dax", "arguments": json.dumps({"dax": "EVALUATE Sales"})}}
            ]
        }
        assert _extract_dax(resp) == "EVALUATE Sales"

    def test_from_dax_fence(self):
        resp = {"content": "Here you go:\n```dax\nEVALUATE Products\n```"}
        assert _extract_dax(resp) == "EVALUATE Products"

    def test_from_generic_fence_with_keyword(self):
        resp = {"content": "```\nEVALUATE Customer\n```"}
        assert _extract_dax(resp) == "EVALUATE Customer"

    def test_from_bare_statement(self):
        resp = {"content": "Sure. DEFINE MEASURE 'S'[m] = 1 EVALUATE {1}"}
        assert _extract_dax(resp).startswith("DEFINE")

    def test_none_when_no_dax(self):
        assert _extract_dax({"content": "I need more info."}) is None
        assert _extract_dax({}) is None


class _LLM:
    def __init__(self, response):
        self._response = response

    async def generate(self, **kwargs):
        return self._response


def _state(**overrides):
    st = {
        "question": "total sales",
        "system_prompt": "SYS",
        "connection_display_name": "PBI",
        "dataset_id": "ds",
        "workspace_id": "ws",
        "conversation_history": [],
    }
    st.update(overrides)
    return st


class TestGeneratorNode:
    async def test_tool_call_mirrors_generated_sql(self):
        resp = {
            "tool_calls": [
                {"function": {"name": "run_dax", "arguments": json.dumps({"dax": "EVALUATE Sales"})}}
            ],
            "content": "",
            "usage": {},
        }
        node = make_dax_generator(_LLM(resp), DaxPromptLoader())
        out = await node(_state())
        assert out["generated_dax"] == "EVALUATE Sales"
        assert out["generated_sql"] == "EVALUATE Sales"  # shared nodes read this
        assert out["previous_dax_hashes"]

    async def test_clarification_when_no_dax(self):
        node = make_dax_generator(_LLM({"content": "Which region?", "usage": {}}), DaxPromptLoader())
        out = await node(_state())
        assert out["generated_dax"] is None
        assert out["clarification"] == "Which region?"

    async def test_empty_response_yields_fallback_clarification(self):
        node = make_dax_generator(_LLM({"content": "", "usage": {}}), DaxPromptLoader())
        out = await node(_state())
        assert out["generated_dax"] is None
        assert "unable to generate" in out["clarification"].lower()
