"""Each migrated production prompt is loaded on its LLM call path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.insight_service import generate_insights
from src.agent.langgraph_agent.nodes.eval import (
    make_fused_eval_analytics,
    make_fused_eval_analytics_subgraph,
)
from src.agent.langgraph_agent.nodes.memory import make_memory_summarizer
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.read_continuation import build_continuation_messages, continue_read
from src.api import state as api_state
from src.api.models import ColumnInfo, EnhanceChartRequest, GenerateChartRequest
from src.api.routes import charts
from src.security.internal_auth import Principal


class _TemplateCache:
    def __init__(self, content):
        self.content = content
        self.requested = []

    async def get_content(self, name):
        self.requested.append(name)
        return self.content[name]

    async def get_model_override(self, name):
        self.requested.append(f"{name}:model")
        return None


@pytest.mark.asyncio
async def test_memory_and_eval_nodes_load_registered_user_prompts():
    loader = PromptLoader()
    llm = MagicMock()
    llm.generate = AsyncMock(
        return_value={"content": '{"answers_intent": true}', "usage": {}}
    )

    summarizer = make_memory_summarizer(llm, loader)
    await summarizer({
        "conversation_history": [],
        "llm_call_count": 0,
        "llm_latency_ms": 0,
        "token_usage": {},
    })
    assert (
        llm.generate.await_args.kwargs["messages"][1]["content"]
        == loader.get("memory_summarizer_user")
    )

    evaluator = make_fused_eval_analytics(llm, loader)
    await evaluator({
        "question": "revenue by region",
        "generated_sql": "SELECT region, SUM(revenue) FROM sales",
        "query_result": {"columns": ["region", "revenue"], "rows": [{"region": "North", "revenue": 3}]},
        "llm_call_count": 0,
        "llm_latency_ms": 0,
        "token_usage": {},
    })
    assert (
        llm.generate.await_args.kwargs["messages"][1]["content"]
        == loader.get("fused_eval_analytics_user")
    )


@pytest.mark.asyncio
async def test_eval_subgraph_loads_registered_system_prompt():
    cache = _TemplateCache({
        "fused_eval_analytics": "Question: {question}; rows: {row_count}; {sql}; {results_sample}",
        "fused_eval_analytics_system": "registered eval system",
    })
    llm = MagicMock()
    llm.generate = AsyncMock(return_value={"content": '{"answers_intent": true}'})

    node = make_fused_eval_analytics_subgraph(llm, cache)
    await node({"question": "q", "sql": "SELECT 1", "results": [], "row_count": 0})

    assert llm.generate.await_args.kwargs["messages"][0]["content"].startswith(
        "registered eval system"
    )
    assert "fused_eval_analytics_system" in cache.requested


@pytest.mark.asyncio
async def test_insight_and_read_continuation_load_registered_templates():
    llm = MagicMock()
    llm.generate = AsyncMock(
        return_value={"content": '{"summary":"ok","findings":[],"suggestions":[]}'}
    )
    result = await generate_insights(
        dataset={"columns": ["region", "revenue"], "rows": [["North", 10], ["South", 20]]},
        context={},
        original_question="Which region has more revenue?",
        llm_service=llm,
        prompt_template=(
            "Question: {original_question}; {business_rules}; {row_count}; "
            "{column_names}; {data_sample}; {column_stats}"
        ),
        system_template="registered insights system",
    )
    assert result["system_message"] == "registered insights system"
    assert llm.generate.await_args.kwargs["messages"][0]["content"] == "registered insights system"

    cache = _TemplateCache({
        "read_continuation_system": "registered read system; no tools",
        "read_continuation_user": "Q={question}\n{fenced_data}",
    })
    tool_results = MagicMock()
    tool_results.consume = AsyncMock(return_value={"payload": {"results": ["answer"]}})
    llm.generate = AsyncMock(return_value={"content": "final answer"})
    continuation = await continue_read(
        proposal_id="p",
        artifact_id="a",
        question="What happened?",
        owner_user_id="u",
        session_id=None,
        tool_results=tool_results,
        llm=llm,
        prompt_cache=cache,
    )
    assert continuation["answer"] == "final answer"
    messages = llm.generate.await_args.args[0]
    assert messages[0]["content"] == "registered read system; no tools"
    assert "What happened?" in messages[1]["content"]


def test_read_continuation_falls_back_from_legacy_malformed_template():
    messages = build_continuation_messages(
        "what happened?",
        "<tool-data>result</tool-data>",
        user_template='{"example": "unescaped JSON braces"}',
    )
    assert "what happened?" in messages[1]["content"]
    assert "<tool-data>" in messages[1]["content"]


@pytest.mark.asyncio
async def test_chart_generation_and_enhancement_load_registered_templates(monkeypatch):
    cache = _TemplateCache({
        "generate_chart_system": "registered chart generation system",
        "generate_chart_user": "rows={row_count}; {profile_blob}; {instruction_blob}; {sample_count}; {sample_rows}",
        "enhance_chart_system": "registered chart enhancement system",
        "enhance_chart_user": "type={chart_type}; cols={columns}; data={sample_data}; cfg={current_config}",
    })
    monkeypatch.setattr(api_state, "prompt_cache", cache)
    llm = MagicMock()
    llm.generate = AsyncMock(
        return_value={"content": '{"chart_type":"bar","x":"category","y":["value"]}'}
    )
    agent = MagicMock(llm=llm)
    monkeypatch.setattr(charts, "resolve_agent", AsyncMock(return_value=agent))

    principal = Principal(user_id="u", role="admin")
    generated = await charts.generate_chart(GenerateChartRequest(
        connection="sales",
        user_id="u",
        question="revenue by category",
        column_names=["category", "value"],
        all_data=[["North", 10], ["South", 20]],
    ), principal)
    assert generated.system_message == "registered chart generation system"
    assert llm.generate.await_args.kwargs["messages"][0]["content"] == "registered chart generation system"

    llm.generate = AsyncMock(return_value={"content": '{"title":{"text":"Revenue"}}'})
    enhanced = await charts.enhance_chart_endpoint(EnhanceChartRequest(
        connection="sales",
        chart_type="bar",
        columns=[ColumnInfo(name="category", type="text"), ColumnInfo(name="value", type="number")],
        sample_data=[["North", 10]],
        current_config={"series": []},
    ), principal)
    assert enhanced["enhanced_config"]["title"]["text"] == "Revenue"
    assert llm.generate.await_args.kwargs["messages"][0]["content"] == "registered chart enhancement system"


@pytest.mark.asyncio
async def test_chart_system_prompt_unescapes_validated_json_braces(monkeypatch):
    monkeypatch.setattr(
        api_state,
        "prompt_cache",
        _TemplateCache({"generate_chart_system": ""}),
    )

    prompt, _ = await charts._get_runtime_prompt(
        "generate_chart_system",
        render_static=True,
    )

    assert '"chart_type"' in prompt
    assert "{{" not in prompt
