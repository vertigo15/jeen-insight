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
def _router_resp(route: str, reason: str = "test", **extra: Any) -> Dict[str, Any]:
    return {
        "content": json.dumps({"route": route, "reason": reason, **extra}),
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


def _empty_diag_resp(plausible: bool, reason: str = "reason", hint: str = "hint") -> Dict[str, Any]:
    """empty_result_check gate reply: is a 0-row result plausible, plus a hint."""
    return {
        "content": json.dumps({"plausible": plausible, "reason": reason, "hint": hint}),
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


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


def _build(svc, prompt_loader, *, max_retries=3, dlp_enabled=True, sqlglot_enabled=True, snapshot_engine=None):
    return build_graph(
        llm=svc.llm,
        router_llm=svc.llm,
        sql_runner=svc.sql_runner,
        metadata_loader=svc.metadata_loader,
        history_service=svc.history_service,
        prompt_loader=prompt_loader,
        deployment_name="test-deployment",
        max_retries=max_retries,
        dlp_enabled=dlp_enabled,
        sqlglot_validation_enabled=sqlglot_enabled,
        snapshot_engine=snapshot_engine,
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
        "memory_window": 5,
        "prior_refs": [],
        "history_query": None,
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
            _router_resp("from_memory", "User is asking about a previous result", prior_refs=["T1"]),
            _text_resp('{"action": "answer", "answer": "Based on our earlier query, total sales in 2007 were $25M."}'),
        ]

        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(conversation_history=[_PRIOR_TURN]))
        resp = result["formatted_response"]

        assert "25M" in resp["answer"]
        assert resp["sql"] is None        # no SQL was generated
        mock_services.sql_runner.run_sql.assert_not_called()
        assert resp["metrics"]["route"] == "from_memory"
        assert resp["routing"]["memory_action"] == "answer"

    @pytest.mark.asyncio
    async def test_from_memory_with_no_prior_turns_falls_through_to_query(self, mock_services, prompt_loader):
        """Nothing to remember → the memory node steps aside without an LLM call."""
        sql = "SELECT SalesAmount FROM FactSales LIMIT 5"
        mock_services.llm.generate.side_effect = [
            _router_resp("from_memory"),
            _sql_tool_resp(sql),
            _eval_resp(),
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"], "rows": [{"SalesAmount": 1}, {"SalesAmount": 2}], "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state())
        resp = result["formatted_response"]
        assert resp["sql"] == sql and resp["metrics"]["llm_call_count"] == 3

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
        result = await graph.ainvoke(_initial_state(conversation_history=[_PRIOR_TURN]))
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


class TestEmptyResultRecheck:
    _EMPTY = {"columns": ["OrderYear", "total"], "rows": [], "row_count": 0}
    _ROWS = {
        "columns": ["OrderYear", "total"],
        "rows": [{"OrderYear": 2007, "total": 25000000}, {"OrderYear": 2008, "total": 29000000}],
        "row_count": 2,
    }
    _SQL = "SELECT OrderYear, SUM(SalesAmount) AS total FROM FactSales JOIN DimProduct ON true GROUP BY OrderYear"

    @pytest.mark.asyncio
    async def test_suspicious_empty_regenerates_sql_then_returns_rows(self, mock_services, prompt_loader):
        """A suspicious 0-row aggregate is rechecked once; the fixed SQL returns rows."""
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(self._SQL),          # 1st SQL → empty
            _empty_diag_resp(False),            # gate: implausible → recheck
            _sql_tool_resp(self._SQL + " -- fixed"),  # regenerated SQL → rows
            _eval_resp("Sales by year."),       # eval on the non-empty result
        ]
        mock_services.sql_runner.run_sql.side_effect = [self._EMPTY, self._ROWS]

        graph = _build(mock_services, prompt_loader)
        resp = (await graph.ainvoke(_initial_state()))["formatted_response"]

        assert resp["results"]["row_count"] == 2
        assert resp.get("empty_result") is None  # the retry produced rows
        assert resp["metrics"]["llm_call_count"] == 5
        assert mock_services.sql_runner.run_sql.call_count == 2

    @pytest.mark.asyncio
    async def test_plausible_empty_gets_deterministic_answer_and_hint(self, mock_services, prompt_loader):
        """When the gate deems the empty result plausible, no SQL retry — the user
        gets an explicit 'no records' answer plus the likely-cause hint."""
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(self._SQL),
            _empty_diag_resp(True, hint="No sales fall in the requested range."),
        ]
        mock_services.sql_runner.run_sql.return_value = self._EMPTY

        graph = _build(mock_services, prompt_loader)
        resp = (await graph.ainvoke(_initial_state()))["formatted_response"]

        assert resp["empty_result"] is True
        assert resp["empty_hint"] == "No sales fall in the requested range."
        assert resp["answer"] == "No records were returned from the database for this query."
        assert resp.get("findings") is None and resp.get("followups") is None
        assert resp["results"]["row_count"] == 0
        assert resp["metrics"]["llm_call_count"] == 3
        assert mock_services.sql_runner.run_sql.call_count == 1

    @pytest.mark.asyncio
    async def test_recheck_is_bounded_to_one_pass(self, mock_services, prompt_loader):
        """If the regenerated SQL is still empty, the graph accepts it and stops —
        the diagnostics counter prevents a second gate call or any loop."""
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(self._SQL),
            _empty_diag_resp(False),            # first (and only) gate call
            _sql_tool_resp(self._SQL + " -- v2"),
            # No further LLM calls: the second empty result is accepted directly.
        ]
        mock_services.sql_runner.run_sql.side_effect = [self._EMPTY, self._EMPTY]

        graph = _build(mock_services, prompt_loader)
        resp = (await graph.ainvoke(_initial_state()))["formatted_response"]

        assert resp["empty_result"] is True
        assert resp["metrics"]["llm_call_count"] == 4  # router + 2 sql + 1 gate
        assert mock_services.sql_runner.run_sql.call_count == 2

    @pytest.mark.asyncio
    async def test_disabled_flag_accepts_empty_without_gate(self, mock_services, prompt_loader):
        """With the recheck disabled, a 0-row result is accepted with no LLM gate."""
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(self._SQL),
        ]
        mock_services.sql_runner.run_sql.return_value = self._EMPTY

        graph = build_graph(
            llm=mock_services.llm,
            router_llm=mock_services.llm,
            sql_runner=mock_services.sql_runner,
            metadata_loader=mock_services.metadata_loader,
            history_service=mock_services.history_service,
            prompt_loader=prompt_loader,
            deployment_name="test-deployment",
            empty_recheck_enabled=False,
        )
        resp = (await graph.ainvoke(_initial_state()))["formatted_response"]

        assert resp["empty_result"] is True
        assert resp.get("empty_hint") is None
        assert resp["metrics"]["llm_call_count"] == 2  # router + sql only


class TestRecursionLimit:
    def test_limit_covers_the_longest_budget_legal_path(self):
        """A request that exhausts every retry budget must end with an 'exhausted'
        answer, never a GraphRecursionError. Recount when a node is added to the
        prefix or to the repair loop (see the comment next to the constant)."""
        from src.agent.langgraph_agent.graph import _GRAPH_LONGEST_LEGAL_PATH, _GRAPH_RECURSION_LIMIT

        prefix = ["context_composer", "fused_router", "memory_answer_generator", "catalog_lookup",
                  "filter_planner", "filter_grounder", "prior_data_binder", "prompt_builder"]
        reground = ["sql_generator", "sqlglot_validate", "dlp_check", "execute_query", "empty_filter_result_check",
                    "feedback_classifier", "filter_grounder", "prior_data_binder", "prompt_builder"]
        recheck = ["sql_generator", "sqlglot_validate", "dlp_check", "execute_query", "empty_filter_result_check",
                   "empty_result_check", "feedback_classifier"]
        attempt = ["sql_generator", "sqlglot_validate", "dlp_check", "execute_query", "empty_filter_result_check",
                   "trivial_result_check", "fused_eval_analytics", "feedback_classifier"]
        tail = ["response_formatter", "save_to_memory", "observability_log"]
        max_retries = 3
        # reground and recheck are mutually exclusive on a given empty result, so
        # counting both is a conservative over-estimate of the true longest path.
        longest = len(prefix) + len(reground) + len(recheck) + (1 + max_retries) * len(attempt) + len(tail)
        assert longest == _GRAPH_LONGEST_LEGAL_PATH == 59
        assert _GRAPH_RECURSION_LIMIT > longest


_PRIOR_TURN = {
    "id": "qid-prev",
    "natural_language_query": "products and prices",
    "generated_sql": "SELECT product, price FROM p",
    "result_artifact": {"columns": ["product", "price"], "column_types": {"price": "int"}, "row_count": 3},
    "snapshot_status": "stored",
    "answer": "The Bike is the most expensive product.",
}


class TestConversationMemory:
    @pytest.mark.asyncio
    async def test_ledger_reaches_router_and_sql_generator_without_extra_llm_calls(self, mock_services, prompt_loader):
        """No summarizer any more: history costs zero LLM calls; the router and the
        SQL generator both receive the turn ledger."""
        sql = "SELECT SalesAmount FROM FactSales LIMIT 5"
        mock_services.llm.generate.side_effect = [
            _router_resp("needs_query"),
            _sql_tool_resp(sql),
            _eval_resp(),
        ]
        mock_services.sql_runner.run_sql.return_value = {
            "columns": ["SalesAmount"], "rows": [{"SalesAmount": 100}, {"SalesAmount": 200}], "row_count": 2,
        }
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(conversation_history=[_PRIOR_TURN] * 5))
        resp = result["formatted_response"]

        assert resp["metrics"]["llm_call_count"] == 3          # router + sql_gen + eval
        assert resp["sql"] == sql
        mem = resp["metrics"]["memory"]
        assert mem["turns_loaded"] == 5 and mem["window"] == 5 and mem["window_saturated"] is True
        assert mem["turns_with_data"] == 5 and mem["ledger_tokens_est"] > 0
        router_prompt = mock_services.llm.generate.await_args_list[0].kwargs["messages"][0]["content"]
        assert 'T5 (most recent) · Q: "products and prices"' in router_prompt
        sql_messages = mock_services.llm.generate.await_args_list[1].kwargs["messages"]
        tool_results = [m for m in sql_messages if m["role"] == "tool"]
        assert len(tool_results) == 5
        assert tool_results[0]["content"] == "3 rows; columns: product, price(int); answer: The Bike is the most expensive product."
        assert [e["node"] for e in result["trace"]][:2] == ["context_composer", "fused_router"]

    @pytest.mark.asyncio
    async def test_from_memory_compute_flows_through_eval_and_never_hits_the_source(self, mock_services, prompt_loader, snapshot_engine):
        from src.api.result_cache import result_cache

        state = _initial_state(question="which product was the most expensive?", conversation_history=[_PRIOR_TURN])
        result_cache.put(
            user_id=state["user_id"], connection="test_db", query_id="qid-prev",
            dataset={"columns": ["product", "price"],
                     "rows": [{"product": "Bike", "price": 1200}, {"product": "Helmet", "price": 80}]},
        )
        sql = 'SELECT "product", "price" FROM insights_mem_t1 ORDER BY "price" DESC'
        snapshot_engine.script(sql, rows=[{"product": "Bike", "price": 1200}, {"product": "Helmet", "price": 80}])
        mock_services.llm.generate.side_effect = [
            _router_resp("from_memory", prior_refs=["T1"]),
            _text_resp(json.dumps({"action": "compute", "ref": "T1", "sql": sql})),
            _eval_resp(),
        ]
        graph = _build(mock_services, prompt_loader, snapshot_engine=snapshot_engine)
        result = await graph.ainvoke(state)
        resp = result["formatted_response"]

        mock_services.sql_runner.run_sql.assert_not_awaited()          # the customer's source is never touched
        assert snapshot_engine.calls == [sql]
        assert resp["results"]["rows"] == [{"product": "Bike", "price": 1200}, {"product": "Helmet", "price": 80}]
        assert resp["sql"].startswith("-- memory:")
        assert resp["routing"]["route"] == "from_memory" and resp["routing"]["memory_action"] == "compute"
        assert resp["metrics"]["memory"]["data_sources"] == {"T1": "cache"}
        nodes = [e["node"] for e in result["trace"]]
        assert "memory_answer_generator" in nodes and "fused_eval_analytics" in nodes
        assert "sql_generator" not in nodes and "feedback_classifier" not in nodes

    @pytest.mark.asyncio
    async def test_history_lookup_answers_from_the_history_service(self, mock_services, prompt_loader):
        from datetime import datetime, timezone

        mock_services.history_service.search_turns = AsyncMock(return_value=[{
            "id": "q-old", "session_id": "s-old", "natural_language_query": "revenue by region",
            "created_at": datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc), "answer": "North led.",
        }])
        mock_services.llm.generate.side_effect = [
            _router_resp("history_lookup", history_query={"keywords": ["revenue"], "since": "2026-09-13"}),
        ]
        graph = _build(mock_services, prompt_loader)
        result = await graph.ainvoke(_initial_state(question="did I ask about revenue in the last 4 days?"))
        resp = result["formatted_response"]

        assert resp["metrics"]["llm_call_count"] == 1
        assert "revenue by region" in resp["answer"] and "North led." in resp["answer"]
        assert resp["history_matches"][0]["query_id"] == "q-old"
        assert resp["results"] is None and resp["sql"] is None
        mock_services.sql_runner.run_sql.assert_not_awaited()


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
