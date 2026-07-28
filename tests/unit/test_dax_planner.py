"""Unit tests for the dax_query_planner node (typed plan before generation)."""

from __future__ import annotations

import json

from src.agent.langgraph_agent_dax.nodes.planner import make_dax_query_planner
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader


class _LLM:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return {"content": self._content, "usage": {"total_tokens": 5}}


def _state(**overrides):
    st = {
        "question": "total sales by region",
        "metadata_bundle": {
            "columns": "- Sales.Amount - Type: measure\n- Sales.Region - Type: text",
            "tables": "- Sales",
            "relationships": "",
            "business_terms": "",
            "knowledge_pairs": "",
        },
        "date_table": "Calendar",
        "conversation_history": [],
        "llm_timeout_seconds": 30,
        "llm_call_count": 0,
        "llm_latency_ms": 0,
        "token_usage": {},
        "node_prompts": {},
    }
    st.update(overrides)
    return st


def _node(content):
    return make_dax_query_planner(_LLM(content), DaxPromptLoader())


class TestPlannerOutput:
    async def test_valid_plan_parsed(self):
        plan = {
            "grain": "aggregate",
            "dimensions": ["'Sales'[Region]"],
            "metrics": [{"kind": "measure", "ref": "[Total Sales]"}],
            "filters": [],
            "sort": [],
            "assumptions": ["Used the curated measure."],
            "clarification_required": False,
        }
        out = await _node(json.dumps(plan))({**_state()})
        assert out["query_plan"]["grain"] == "aggregate"
        assert out["plan_grain"] == "aggregate"
        assert out["clarification_required"] is False
        assert out["plan_assumptions"] == ["Used the curated measure."]

    async def test_fenced_json_is_extracted(self):
        plan = {"grain": "detail", "clarification_required": False}
        content = f"```json\n{json.dumps(plan)}\n```"
        out = await _node(content)({**_state()})
        assert out["query_plan"]["grain"] == "detail"
        assert out["plan_grain"] == "detail"

    async def test_clarification_required_sets_answer(self):
        plan = {
            "grain": "aggregate",
            "clarification_required": True,
            "clarification": "Which region hierarchy did you mean?",
        }
        out = await _node(json.dumps(plan))({**_state()})
        assert out["clarification_required"] is True
        assert "region" in (out["answer"] or "").lower()

    async def test_malformed_json_falls_back_open(self):
        out = await _node("this is not json at all")({**_state()})
        assert out["query_plan"]["grain"] == "aggregate"
        assert out["clarification_required"] is False
        assert out["query_plan"]["assumptions"]
