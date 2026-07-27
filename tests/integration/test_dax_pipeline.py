"""End-to-end wiring test for the text-to-DAX LangGraph.

Drives the REAL compiled ``build_dax_graph`` from START to END. Only the leaf
I/O is faked: the router + main LLM (deterministic content), the metadata
catalog loader, the delegated Power BI token provider, and ``PowerBiDaxClient``.
Everything in between — memory gate, router, DAX catalog, typed planner, prompt
builder, generator, static validator, executor, integrity check, and the shared
response formatter — runs for real, proving the DAX-specific nodes interoperate
with the shared, engine-agnostic tail.

Self-contained (fakes only, no live services), but marked ``integration`` so it
stays out of the default fast unit run.
"""

from __future__ import annotations

import json
import os
import time

# src.config needs these to import; real values are irrelevant (nothing connects).
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.invalid")
os.environ.setdefault("METADATA_DB_HOST", "test")
os.environ.setdefault("METADATA_DB_NAME", "test")
os.environ.setdefault("METADATA_DB_USER", "test")
os.environ.setdefault("METADATA_DB_PASSWORD", "test")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

import src.agent.langgraph_agent_dax.nodes.dax_execute as ex  # noqa: E402
from src.agent.langgraph_agent_dax.graph import build_dax_graph  # noqa: E402
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader  # noqa: E402

pytestmark = pytest.mark.integration

_DAX = "EVALUATE SUMMARIZECOLUMNS('Sales'[Region], \"Total\", SUM('Sales'[Amount]))"

_PLAN = {
    "grain": "aggregate",
    "dimensions": ["'Sales'[Region]"],
    "metrics": [{"kind": "column", "ref": "'Sales'[Amount]", "agg": "sum"}],
    "filters": [],
    "sort": [],
    "assumptions": ["Aggregating the raw Amount column by Region."],
    "clarification_required": False,
}


class _RouterLLM:
    async def generate(self, **kwargs):
        return {"content": json.dumps({"route": "needs_query", "reason": "data question"}), "usage": {}}


class _MainLLM:
    """Planner call (no tools) → plan JSON; generator call (tools) → run_dax."""

    def __init__(self):
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        if kwargs.get("tools"):
            return {
                "tool_calls": [
                    {"function": {"name": "run_dax", "arguments": json.dumps({"dax": _DAX})}}
                ],
                "content": "",
                "usage": {},
            }
        return {"content": json.dumps(_PLAN), "usage": {}}


class _MetadataLoader:
    async def load_all(self, source_key):
        return {
            "tables": "- Sales\n- Customer",
            "columns": (
                "- Sales.Amount - Type: decimal\n"
                "- Sales.Region - Type: text\n"
                "- Sales.OrderDate - Type: date"
            ),
            "relationships": "",
            "business_terms": "",
            "knowledge_pairs": "",
            "sources": "",
        }


class _TokenProvider:
    async def get_token_for_auth_user(self, auth_user_id, *, force_refresh=False):
        return SimpleNamespace(access_token="tok")


class _PbiClient:
    def __init__(self, **kw):
        pass

    async def execute_dax(self, dax, token, *, max_rows=None):
        return {
            "columns": ["Region", "Total"],
            "rows": [{"Region": "EU", "Total": 100}, {"Region": "US", "Total": 250}],
            "row_count": 2,
            "is_partial": False,
            "http_status": 200,
        }


def _initial_state():
    return {
        "question": "total amount by region",
        "source_key": "sales-model",
        "connection_display_name": "Sales Model",
        "database_type": "powerbi",
        "workspace_id": "ws-1",
        "dataset_id": "ds-1",
        "user_id": "42",
        "conversation_history": [],
        "retry_count": 0,
        "token_usage": {},
        "node_prompts": {},
        "start_time": time.monotonic(),
        "eval_analytics_override": None,
    }


async def test_dax_pipeline_happy_path(monkeypatch):
    monkeypatch.setattr(ex, "_token_provider", lambda: _TokenProvider())
    monkeypatch.setattr(ex, "PowerBiDaxClient", _PbiClient)

    main_llm = _MainLLM()
    graph = build_dax_graph(
        llm=main_llm,
        router_llm=_RouterLLM(),
        metadata_loader=_MetadataLoader(),
        history_service=MagicMock(),
        prompt_loader=DaxPromptLoader(),
        deployment_name="gpt-x",
        max_retries=4,
        dlp_enabled=False,           # keep the happy path free of governance blocks
        dax_validation_enabled=True,
        eval_analytics_enabled=False,  # skip the eval LLM leg
    )

    final = await graph.ainvoke(_initial_state())

    # The generator's DAX was mirrored into generated_sql and survived validation.
    assert final["generated_dax"] == _DAX
    assert final.get("dax_validation_error") is None
    assert final.get("dax_repairable_error") is None
    assert final.get("exec_error") is None

    # The shared response formatter produced the UI contract with real rows.
    formatted = final["formatted_response"]
    assert formatted["sql"] == _DAX
    assert formatted["results"]["row_count"] == 2
    assert formatted["error"] is None
    # Both LLM legs (planner + generator) were exercised.
    assert main_llm.calls == 2


async def test_dax_pipeline_needs_connect(monkeypatch):
    # No connector services configured → the executor short-circuits to a hard,
    # terminal connect/config message rather than looping the repair budget.
    monkeypatch.setattr(ex, "_token_provider", lambda: None)

    graph = build_dax_graph(
        llm=_MainLLM(),
        router_llm=_RouterLLM(),
        metadata_loader=_MetadataLoader(),
        history_service=MagicMock(),
        prompt_loader=DaxPromptLoader(),
        deployment_name="gpt-x",
        dlp_enabled=False,
        eval_analytics_enabled=False,
    )

    final = await graph.ainvoke(_initial_state())
    assert final["dax_terminal"] is True
    assert final["formatted_response"]["error"]
