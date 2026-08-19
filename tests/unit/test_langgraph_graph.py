"""End-to-end LangGraph graph flow tests.

These tests run the compiled graph from START to END using mocked services —
no real Azure OpenAI, no real PostgreSQL connection is required.

Each test scenario covers a distinct execution path through the graph:
  - Happy path (SQL → rows → eval → response)
  - From-memory route (answered without querying the DB)
  - Out-of-scope and unsafe routes (immediate decline)
  - DLP governance block
  - SQL retry on execution error (succeeds on second attempt)
  - Retry exhaustion (all attempts fail → graceful error response)
  - Clarification response (LLM asks a question instead of generating SQL)
  - Trivial result (single value → eval node is skipped)
  - Memory summarizer triggered (large history → summary before routing)
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.agent.langgraph_agent.graph import build_graph
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState


# ── Shared test data ──────────────────────────────────────────────────────────

_METADATA = {
    "tables": "- FactSales - Internet sales fact table\n- DimProduct - Product dimension",
    "columns": "- FactSales.SalesAmount - Type: decimal\n- FactSales.OrderYear - Type: integer\n- DimProduct.ProductKey - Type: integer, PK: true",
    "relationships": "[('FactSales.ProductKey', 'DimProduct.ProductKey')]",
    "sources": "- AdventureWorks | postgresql | dbo | (Active: True)",
    "knowledge_pairs": "No knowledge pairs registered.",
    "business_terms": "No business terms registered.",
}

# LLM response templates
def _router_resp(route: str, reason: str = "test") -> Dict[str, Any]:
    return {
        "content": json.dumps({"route": route, "reason": reason}),
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }

def _sql_tool_resp(sql: str) -> Dict[str, Any]:
    return {
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_sql", "arguments": json.dumps({"sql": sql})},
        }],
        "usage": {"prompt_tokens": 300, "completion_tokens": 60, "total_tokens": 360},
    }

def _eval_resp(summary: str = "Data retrieved successfully.") -> Dict[str, Any]:
    return {
        "content": json.dumps({
            "answers_intent": True,
            "summary": summary,
            "insights": ["Revenue grew year-over-year", "Q4 was the strongest quarter"],
            "follow_up_questions": ["Would you like a breakdown by product?"],
        }),
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 400, "completion_tokens": 80, "total_tokens": 480},
    }

def _text_resp(content: str) -> Dict[str, Any]:
    return {"content": content, "finish_reason": "stop", "usage": {}}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def prompt_loader():
    return PromptLoader()


@pytest.fixture
def mock_services():
    """Return a namespace of mock services for each test."""
    class Services:
        llm = MagicMock()
        sql_runner = MagicMock()
        metadata_loader = MagicMock()
        history_service = MagicMock()

        def __init__(self):
            self.llm.generate = AsyncMock()
            self.sql_runner.run_sql = AsyncMock()
            self.metadata_loader.load_all = AsyncMock(return_value=_METADATA)
            self.history_service.log_query = AsyncMock(return_value=uuid4())
            self.history_service.get_conversation_context = AsyncMock(return_value=[])
            self.history_service.update_llm_response = AsyncMock()
            self.history_service.update_execution = AsyncMock()

    return Services()


def _build(svc, prompt_loader, *, max_retries=3, dlp_enabled=True, sqlglot_enabled=True):
    return build_graph(
        llm=svc.llm,
        router_llm=svc.llm,
        sql_runner=svc.sql_runner,
        metadata_loader=svc.metadata_loader,
        history_service=svc.history_service,
        prompt_loader=prompt_loader,
        deployment_name="test-deployment",
        max_retries=max_retries,
        max_history_tokens=3000,
        dlp_enabled=dlp_enabled,
        sqlglot_validation_enabled=sqlglot_enabled,
    )


def _initial_state(**overrides) -> AgentState:
    base: AgentState = {
        "question": "What are total sales by year?",
        "session_id": uuid4(),
        "source_key": "test_db",
        "user_context": {},
        "limit": 100,
        "temperature": None,
        "connection_display_name": "AdventureWorks",
        "database_type": "postgresql",
        "query_id": uuid4(),
        "user_id": "test_user",
        "start_time": 0.0,
        "llm_call_count": 0,
        "llm_latency_ms": 0,
        "token_usage": {},
        "conversation_history": [],
        "memory_summary": None,
        "is_over_budget": False,
        "route": "needs_query",
        "route_reason": "",
        "metadata_bundle": _METADATA,
        "known_tables": [],
        "retry_count": 0,
        "generated_sql": None,
        "clarification": None,
        "error_context": None,
        "sqlglot_error": None,
        "dlp_blocked": False,
        "governance_error": None,
        "query_result": None,
        "exec_error": None,
        "execution_time_ms": None,
        "is_trivial": False,
        "eval_result": None,
        "feedback_type": None,
        "answer": None,
        "error": None,
    }
    base.update(overrides)
    return base


# ── Graph flow tests ──────────────────────────────────────────────────────────

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_rows_with_summary_and_insights(self, mock_services, prompt_loader):
        """Full happy path: router → SQL gen → validate → execute → eval → format."""
        sql = "SELECT OrderYear, SUM(SalesAmount) AS total FROM FactSales GROUP BY OrderYear"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql),
            _eval_resp("Sales peaked in 2008 at $29M."),
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["OrderYear", "total"],
            "rows": [{"OrderYear": 2007, "total": 25000000}, {"OrderYear": 2008, "total": 29000000}],
            "row_count": 2,
        }

        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] == sql
        assert resp["results"]["row_count"] == 2
        assert "Sales peaked" in resp["answer"]
        assert "Revenue grew" in resp.get("findings", [""])[0]
        assert resp.get("followups") == ["Would you like a breakdown by product?"]
        assert resp["error"] is None
        assert resp["metrics"]["route"] == "needs_query"
        assert resp["metrics"]["retry_count"] == 0
        assert resp["metrics"]["llm_call_count"] == 3

    @pytest.mark.asyncio
    async def test_all_required_response_keys_present(self, mock_services, prompt_loader):
        sql = "SELECT SalesAmount FROM FactSales LIMIT 5"
        mock_services.llm.generate.side_effect = [_router_resp("needs_query"), _sql_tool_resp(sql), _eval_resp()]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"],
            "rows": [{"SalesAmount": 100}, {"SalesAmount": 200}],
            "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        for key in ("question", "query_id", "session_id", "sql", "results", "answer", "prompt", "error", "metrics"):
            assert key in resp, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_history_service_called(self, mock_services, prompt_loader):
        sql = "SELECT SalesAmount FROM FactSales LIMIT 2"
        mock_services.llm.generate.side_effect = [_router_resp("needs_query"), _sql_tool_resp(sql), _eval_resp()]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"], "rows": [{"SalesAmount": 50}, {"SalesAmount": 60}], "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader)
        await graph.ainvoke(_initial_state())

        mock_services.history_service.update_llm_response.assert_called_once()
        mock_services.history_service.update_execution.assert_called_once()


class TestFromMemoryRoute:
    @pytest.mark.asyncio
    async def test_answers_from_memory_without_db_call(self, mock_services, prompt_loader):
        mock_services.llm.generate.side_effect = [
            _router_resp("from_memory", "User is asking about a previous result"),
            _text_resp("Based on our earlier query, total sales in 2007 were $25M."),
        ]

        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert "25M" in resp["answer"]
        assert resp["sql"] is None        # no SQL was generated
        mock_services.sql_runner.run_sql.assert_not_called()
        assert resp["metrics"]["route"] == "from_memory"

    @pytest.mark.asyncio
    async def test_memory_escape_hatch_falls_through_to_query(self, mock_services, prompt_loader):
        """If memory_answer returns needs_query, the graph falls through to catalog_lookup."""
        sql = "SELECT SalesAmount FROM FactSales LIMIT 5"
        mock_services.llm.generate.side_effect = [
            _router_resp("from_memory"),
            _text_resp('{"needs_query": true}'),     # escape hatch
            _sql_tool_resp(sql),                     # sql_generator
            _eval_resp(),                            # eval
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"], "rows": [{"SalesAmount": 100}, {"SalesAmount": 200}], "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] == sql
        mock_services.sql_runner.run_sql.assert_called_once()


class TestGovernanceRoutes:
    @pytest.mark.asyncio
    async def test_out_of_scope_returns_polite_decline(self, mock_services, prompt_loader):
        mock_services.llm.generate.return_value = _router_resp("out_of_scope", "Unrelated to data")
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(question="What is the weather today?"))
        resp = result["formatted_response"]

        assert resp["sql"] is None
        assert resp["answer"] is not None
        assert "AdventureWorks" in resp["answer"] or "scope" in resp["answer"].lower()
        mock_services.sql_runner.run_sql.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsafe_returns_safety_message(self, mock_services, prompt_loader):
        mock_services.llm.generate.return_value = _router_resp("unsafe", "Requests data deletion")
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(question="DELETE all records from FactSales"))
        resp = result["formatted_response"]

        assert resp["sql"] is None
        assert "select" in resp["answer"].lower() or "read-only" in resp["answer"].lower()
        mock_services.sql_runner.run_sql.assert_not_called()

    @pytest.mark.asyncio
    async def test_dlp_blocked_never_reaches_db(self, mock_services, prompt_loader):
        # Use FactSales (known catalog table) with a DLP-triggering column name.
        # sqlglot passes (table in catalog); DLP catches the 'password' keyword.
        sql = "SELECT password FROM FactSales WHERE SalesAmount > 0"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql),
        ]
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(question="Show all passwords"))
        resp = result["formatted_response"]

        assert resp["sql"] == sql                 # SQL was generated
        assert "blocked" in resp["answer"].lower() or "governed" in resp["answer"].lower()
        mock_services.sql_runner.run_sql.assert_not_called()   # never reached DB

    @pytest.mark.asyncio
    async def test_dlp_disabled_allows_query(self, mock_services, prompt_loader):
        """When DLP is disabled, governed keywords do not block the query."""
        # sqlglot disabled too so the 'password' column name doesn't trigger a catalog error.
        sql = "SELECT password FROM FactSales"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql),
            _eval_resp(),
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["password"], "rows": [{"password": "***"}], "row_count": 1,
        }
        # Disable both DLP and sqlglot to test the DLP=off path in isolation.
        graph = _build(mock_services, prompt_loader, dlp_enabled=False, sqlglot_enabled=False)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        mock_services.sql_runner.run_sql.assert_called_once()
        assert resp["error"] is None


class TestCatalogDenyByDefault:
    @pytest.mark.asyncio
    async def test_empty_catalog_blocks_query(self, mock_services, prompt_loader):
        """No catalog metadata → deny-by-default: no SQL generated, no DB call."""
        mock_services.metadata_loader.load_all = AsyncMock(
            return_value={"tables": "", "columns": ""}
        )
        mock_services.llm.generate.side_effect = [_router_resp("needs_query")]
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] is None
        assert resp["error"] is not None
        assert "catalog" in resp["error"].lower() or "schema" in resp["error"].lower()
        mock_services.sql_runner.run_sql.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_catalog_load_blocks_query(self, mock_services, prompt_loader):
        """Metadata load raising → fail closed rather than querying blindly."""
        mock_services.metadata_loader.load_all = AsyncMock(
            side_effect=RuntimeError("metadata DB unreachable")
        )
        mock_services.llm.generate.side_effect = [_router_resp("needs_query")]
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] is None
        assert resp["error"] is not None
        mock_services.sql_runner.run_sql.assert_not_called()


class TestClarificationPath:
    @pytest.mark.asyncio
    async def test_clarification_returned_as_answer(self, mock_services, prompt_loader):
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _text_resp("Could you specify which year or region you are interested in?"),
        ]
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(question="Show me sales"))
        resp = result["formatted_response"]

        assert resp["sql"] is None
        assert "year" in resp["answer"].lower() or "region" in resp["answer"].lower()
        mock_services.sql_runner.run_sql.assert_not_called()


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_retries_once_and_succeeds(self, mock_services, prompt_loader):
        """First SQL attempt fails with execution error; second attempt succeeds."""
        sql_bad = "SELECT SalesAmount FROM FactSale"   # typo — wrong table
        sql_good = "SELECT SalesAmount FROM FactSales LIMIT 10"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql_bad),    # first attempt (sqlglot may pass since disabled=False would catch)
            _sql_tool_resp(sql_good),   # retry after exec error
            _eval_resp(),
        ]
        # First run_sql fails, second succeeds
        mock_services.sql_runner.run_sql.side_effect = [
            {"error": "relation \"factsale\" does not exist"},
            {"columns": ["SalesAmount"], "rows": [{"SalesAmount": 100}, {"SalesAmount": 200}], "row_count": 2},
        ]
        # Disable sqlglot so both SQLs reach execution (first is wrong at DB level only)
        graph = _build(mock_services, prompt_loader, max_retries=3, sqlglot_enabled=False)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] == sql_good
        assert resp["results"]["row_count"] == 2
        assert resp["error"] is None
        assert resp["metrics"]["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_exhausted_after_max_retries_returns_graceful_error(self, mock_services, prompt_loader):
        """All SQL attempts fail → graceful error response (no exception raised)."""
        sql = "SELECT bad_col FROM FactSales"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql),   # attempt 1
            _sql_tool_resp(sql),   # attempt 2
        ]
        mock_services.sql_runner.run_sql.side_effect = [
            {"error": "column does not exist"},
            {"error": "column does not exist"},
        ]
        # max_retries=1: attempt 1 fails → retry → attempt 2 fails → exhausted
        graph = _build(mock_services, prompt_loader, max_retries=1, sqlglot_enabled=False)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        # Graceful: returns a dict, not an exception
        assert isinstance(resp, dict)
        assert resp["metrics"]["retry_count"] >= 1
        assert resp["metrics"]["route"] == "needs_query"

    @pytest.mark.asyncio
    async def test_sqlglot_error_triggers_retry(self, mock_services, prompt_loader):
        """sqlglot finds an unknown table → feedback_classifier marks missing_table → retry."""
        sql_bad = "SELECT id FROM NonExistentTable"    # not in catalog
        sql_good = "SELECT SalesAmount FROM FactSales LIMIT 5"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql_bad),    # triggers sqlglot error
            _sql_tool_resp(sql_good),   # retry
            _eval_resp(),
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"],
            "rows": [{"SalesAmount": 100}, {"SalesAmount": 150}],
            "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader, max_retries=3, sqlglot_enabled=True)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] == sql_good
        assert resp["metrics"]["retry_count"] == 1
        assert resp["error"] is None


class TestTrivialResult:
    @pytest.mark.asyncio
    async def test_single_value_skips_eval_node(self, mock_services, prompt_loader):
        """COUNT(*) → 1 row × 1 col → trivial → fused_eval_analytics is NOT called."""
        sql = "SELECT COUNT(*) AS total FROM FactSales"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql),
            # No third call — eval is skipped for trivial results
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["total"],
            "rows": [{"total": 42}],
            "row_count": 1,
        }
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        assert resp["sql"] == sql
        assert resp["results"]["rows"][0]["total"] == 42
        # fused_eval_analytics was NOT called (only 2 LLM calls total)
        assert resp["metrics"]["llm_call_count"] == 2


class TestMemorySummarizer:
    @pytest.mark.asyncio
    async def test_summarizer_triggered_on_large_history(self, mock_services, prompt_loader):
        """When conversation history is over budget, memory_summarizer runs before fused_router."""
        large_history = [
            {
                "natural_language_query": "A" * 300,
                "generated_sql": "SELECT " + "col" * 200 + " FROM FactSales",
            }
            for _ in range(5)
        ]

        sql = "SELECT SalesAmount FROM FactSales LIMIT 5"
        mock_services.llm.generate.side_effect = [
            _text_resp("History summary: sales queries for 2007-2009."),   # memory_summarizer
            _router_resp("needs_query"),                                    # fused_router
            _sql_tool_resp(sql),                                           # sql_generator
            _eval_resp(),                                                  # eval
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"],
            "rows": [{"SalesAmount": 100}, {"SalesAmount": 200}],
            "row_count": 2,
        }
        # Set a very small token budget so the 5 large entries trigger summarization
        graph = _build(mock_services, prompt_loader, max_retries=3)
        graph_small_budget = build_graph(
            llm=mock_services.llm,
            router_llm=mock_services.llm,
            sql_runner=mock_services.sql_runner,
            metadata_loader=mock_services.metadata_loader,
            history_service=mock_services.history_service,
            prompt_loader=prompt_loader,
            deployment_name="test",
            max_retries=3,
            max_history_tokens=50,   # very small: triggers summarizer
            dlp_enabled=True,
            sqlglot_validation_enabled=True,
        )
        result = await graph_small_budget.ainvoke(_initial_state(conversation_history=large_history))
        resp = result["formatted_response"]

        # 4 LLM calls: summarizer + router + sql_gen + eval
        assert resp["metrics"]["llm_call_count"] == 4
        assert resp["sql"] == sql


class TestTokenAndMetricsAccumulation:
    @pytest.mark.asyncio
    async def test_token_usage_accumulated_across_llm_calls(self, mock_services, prompt_loader):
        sql = "SELECT SalesAmount FROM FactSales LIMIT 3"
        mock_services.llm.generate.side_effect = [
            {**_router_resp("needs_query"), "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
            {**_sql_tool_resp(sql), "usage": {"prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 350}},
            {**_eval_resp(), "usage": {"prompt_tokens": 400, "completion_tokens": 70, "total_tokens": 470}},
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"], "rows": [{"SalesAmount": 1}, {"SalesAmount": 2}], "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]

        # 60 + 350 + 470 = 880 total tokens
        assert resp["metrics"]["total_tokens"] == 880
        assert resp["metrics"]["input_tokens"] == 750    # 50 + 300 + 400
        assert resp["metrics"]["output_tokens"] == 130   # 10 + 50 + 70
