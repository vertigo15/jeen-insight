"""Unit tests for individual LangGraph node functions.

Each test exercises one node in complete isolation.  LLM and DB calls are
replaced with ``AsyncMock`` / ``MagicMock`` so these tests run offline and
finish in milliseconds.

Coverage:
  - memory_shrink_check
  - sqlglot_validate
  - dlp_check
  - trivial_result_check
  - feedback_classifier
  - response_formatter
  - _extract_table_names  (catalog helper)
  - _extract_sql          (sql_gen helper)
  - PromptLoader          (load / render / hot-reload)
  - memory_summarizer     (async LLM call)
  - fused_router          (async LLM call)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.langgraph_agent.nodes.catalog import _extract_table_names, make_prompt_builder
from src.agent.langgraph_agent.nodes.execution import trivial_result_check
from src.agent.langgraph_agent.nodes.feedback import make_feedback_classifier
from src.agent.langgraph_agent.nodes.memory import make_memory_shrink_check, make_memory_summarizer
from src.agent.langgraph_agent.nodes.output import response_formatter
from src.agent.langgraph_agent.nodes.router import make_fused_router
from src.agent.langgraph_agent.nodes.sql_gen import _extract_sql
from src.agent.langgraph_agent.nodes.validation import make_dlp_check, make_sqlglot_validate
from src.agent.langgraph_agent.prompt_loader import PromptLoader


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def prompt_loader():
    """Real PromptLoader backed by the project's src/agent/prompts/ directory."""
    return PromptLoader()


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.generate = AsyncMock()
    return llm


# ── memory_shrink_check ────────────────────────────────────────────────────────

class TestMemoryShrinkCheck:
    def test_within_budget(self):
        check = make_memory_shrink_check(max_history_tokens=3000)
        state = {"conversation_history": [{"natural_language_query": "short q", "generated_sql": "SELECT 1"}]}
        result = check(state)
        assert result["is_over_budget"] is False

    def test_over_budget(self):
        check = make_memory_shrink_check(max_history_tokens=10)
        # 1000-char string → ~250 estimated tokens, way over budget=10
        big_entry = {"natural_language_query": "x" * 500, "generated_sql": "SELECT " + "a" * 500}
        state = {"conversation_history": [big_entry]}
        result = check(state)
        assert result["is_over_budget"] is True

    def test_empty_history_never_over_budget(self):
        check = make_memory_shrink_check(max_history_tokens=1)
        result = check({"conversation_history": []})
        assert result["is_over_budget"] is False


# ── memory_summarizer ──────────────────────────────────────────────────────────

class TestMemorySummarizer:
    @pytest.mark.asyncio
    async def test_returns_summary(self, mock_llm, prompt_loader):
        mock_llm.generate.return_value = {
            "content": "Sales data was queried. Total revenue was $1.2M.",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        summarizer = make_memory_summarizer(mock_llm, prompt_loader)
        state = {
            "conversation_history": [
                {"natural_language_query": "total sales?", "generated_sql": "SELECT sum(sales) FROM t"},
            ],
            "llm_call_count": 0,
            "llm_latency_ms": 0,
            "token_usage": {},
        }
        result = await summarizer(state)
        assert "Sales data was queried" in result["memory_summary"]
        assert result["llm_call_count"] == 1
        assert result["token_usage"]["total_tokens"] == 120

    @pytest.mark.asyncio
    async def test_increments_existing_counts(self, mock_llm, prompt_loader):
        mock_llm.generate.return_value = {"content": "summary", "finish_reason": "stop", "usage": {"total_tokens": 50}}
        summarizer = make_memory_summarizer(mock_llm, prompt_loader)
        state = {"conversation_history": [], "llm_call_count": 5, "llm_latency_ms": 100, "token_usage": {"total_tokens": 200}}
        result = await summarizer(state)
        assert result["llm_call_count"] == 6
        assert result["token_usage"]["total_tokens"] == 250


# ── fused_router ──────────────────────────────────────────────────────────────

class TestFusedRouter:
    @pytest.mark.asyncio
    async def test_routes_needs_query(self, mock_llm, prompt_loader):
        mock_llm.generate.return_value = {
            "content": '{"route": "needs_query", "reason": "Requires DB query"}',
            "finish_reason": "stop",
            "usage": {},
        }
        router = make_fused_router(mock_llm, prompt_loader)
        state = {
            "question": "What are total sales?",
            "connection_display_name": "AdventureWorks",
            "memory_summary": None,
            "llm_call_count": 0,
            "llm_latency_ms": 0,
            "token_usage": {},
        }
        result = await router(state)
        assert result["route"] == "needs_query"
        assert result["llm_call_count"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["from_memory", "out_of_scope", "unsafe"])
    async def test_routes_all_valid_categories(self, mock_llm, prompt_loader, route):
        mock_llm.generate.return_value = {
            "content": json.dumps({"route": route, "reason": "test"}),
            "finish_reason": "stop",
            "usage": {},
        }
        router = make_fused_router(mock_llm, prompt_loader)
        state = {"question": "q", "connection_display_name": "DB", "memory_summary": None,
                 "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {}}
        result = await router(state)
        assert result["route"] == route

    @pytest.mark.asyncio
    async def test_defaults_to_needs_query_on_bad_json(self, mock_llm, prompt_loader):
        mock_llm.generate.return_value = {
            "content": "I cannot classify this",
            "finish_reason": "stop",
            "usage": {},
        }
        router = make_fused_router(mock_llm, prompt_loader)
        state = {"question": "q", "connection_display_name": "DB", "memory_summary": None,
                 "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {}}
        result = await router(state)
        # Safe default: always proceed to query path rather than drop the request
        assert result["route"] == "needs_query"

    @pytest.mark.asyncio
    async def test_defaults_to_needs_query_on_unknown_route(self, mock_llm, prompt_loader):
        mock_llm.generate.return_value = {
            "content": '{"route": "warp_speed", "reason": "unknown"}',
            "finish_reason": "stop",
            "usage": {},
        }
        router = make_fused_router(mock_llm, prompt_loader)
        state = {"question": "q", "connection_display_name": "DB", "memory_summary": None,
                 "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {}}
        result = await router(state)
        assert result["route"] == "needs_query"


# ── sqlglot_validate ──────────────────────────────────────────────────────────

class TestSqlglotValidate:
    def test_valid_select_passes(self):
        validate = make_sqlglot_validate(enabled=True)
        result = validate({"generated_sql": "SELECT id, name FROM customers", "known_tables": ["customers"]})
        assert result["sqlglot_error"] is None

    def test_valid_cte_passes(self):
        validate = make_sqlglot_validate(enabled=True)
        state = {
            "generated_sql": "WITH cte AS (SELECT id FROM customers) SELECT * FROM cte",
            "known_tables": ["customers"],
        }
        result = validate(state)
        assert result["sqlglot_error"] is None

    def test_unknown_table_produces_error(self):
        validate = make_sqlglot_validate(enabled=True)
        result = validate({
            "generated_sql": "SELECT id FROM nonexistent_table",
            "known_tables": ["customers", "orders"],
        })
        assert result["sqlglot_error"] is not None
        assert "nonexistent_table" in result["sqlglot_error"].lower()

    def test_empty_catalog_skips_table_check(self):
        """When no known_tables are provided, table validation is skipped."""
        validate = make_sqlglot_validate(enabled=True)
        result = validate({"generated_sql": "SELECT id FROM mystery_table", "known_tables": []})
        assert result["sqlglot_error"] is None

    def test_disabled_skips_all_validation(self):
        validate = make_sqlglot_validate(enabled=False)
        result = validate({"generated_sql": "NOT EVEN SQL {{{{", "known_tables": ["t"]})
        assert result["sqlglot_error"] is None

    def test_empty_sql_passes_without_error(self):
        validate = make_sqlglot_validate(enabled=True)
        result = validate({"generated_sql": "", "known_tables": ["t"]})
        assert result["sqlglot_error"] is None

    def test_case_insensitive_table_match(self):
        """SQL uses mixed case; catalog is lowercased — should still pass."""
        validate = make_sqlglot_validate(enabled=True)
        state = {
            "generated_sql": "SELECT SalesAmount FROM FactSales LIMIT 10",
            "known_tables": ["factsales", "dimproduct"],
        }
        result = validate(state)
        assert result["sqlglot_error"] is None


# ── dlp_check ─────────────────────────────────────────────────────────────────

class TestDlpCheck:
    @pytest.mark.parametrize("sql", [
        "SELECT id, password FROM users",
        "SELECT ssn FROM customers WHERE id = 1",
        "SELECT credit_cards FROM accounts",
        "SELECT api_key FROM tokens",
        "SELECT access_token FROM sessions",
    ])
    def test_blocks_governed_keywords(self, sql):
        check = make_dlp_check(enabled=True)
        result = check({"generated_sql": sql})
        assert result["dlp_blocked"] is True
        assert result["governance_error"] is not None

    def test_passes_clean_sql(self):
        check = make_dlp_check(enabled=True)
        result = check({"generated_sql": "SELECT SalesAmount, OrderYear FROM FactSales"})
        assert result["dlp_blocked"] is False
        assert result["governance_error"] is None

    def test_disabled_never_blocks(self):
        check = make_dlp_check(enabled=False)
        result = check({"generated_sql": "SELECT password FROM users"})
        assert result["dlp_blocked"] is False

    def test_case_insensitive_blocking(self):
        check = make_dlp_check(enabled=True)
        result = check({"generated_sql": "SELECT PASSWORD FROM accounts"})
        assert result["dlp_blocked"] is True


# ── trivial_result_check ──────────────────────────────────────────────────────

class TestTrivialResultCheck:
    def test_single_value_is_trivial(self):
        state = {"query_result": {"rows": [{"total": 42}], "columns": ["total"]}}
        assert trivial_result_check(state)["is_trivial"] is True

    def test_single_row_many_cols_is_not_trivial(self):
        state = {"query_result": {
            "rows": [{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}],
            "columns": ["a", "b", "c", "d", "e", "f"],
        }}
        assert trivial_result_check(state)["is_trivial"] is False

    def test_multi_row_is_not_trivial(self):
        state = {"query_result": {
            "rows": [{"y": 2021, "sales": 100}, {"y": 2022, "sales": 200}],
            "columns": ["y", "sales"],
        }}
        assert trivial_result_check(state)["is_trivial"] is False

    def test_empty_result_is_trivial(self):
        state = {"query_result": {"rows": [], "columns": []}}
        assert trivial_result_check(state)["is_trivial"] is True

    def test_none_result_is_trivial(self):
        assert trivial_result_check({"query_result": None})["is_trivial"] is True


# ── feedback_classifier ───────────────────────────────────────────────────────

class TestFeedbackClassifier:
    def _state(self, **kwargs):
        base = {"retry_count": 0, "question": "test", "error_context": None,
                "sqlglot_error": None, "exec_error": None, "eval_result": None}
        base.update(kwargs)
        return base

    def test_syntax_error(self):
        fc = make_feedback_classifier(max_retries=3)
        result = fc(self._state(sqlglot_error="SQL syntax error near 'FROM'"))
        assert result["feedback_type"] == "syntax"
        assert result["retry_count"] == 1

    def test_missing_table(self):
        fc = make_feedback_classifier(max_retries=3)
        result = fc(self._state(sqlglot_error="Table 'foo' not found in catalog."))
        assert result["feedback_type"] == "missing_table"

    def test_exec_error(self):
        fc = make_feedback_classifier(max_retries=3)
        result = fc(self._state(exec_error="column does not exist"))
        assert result["feedback_type"] == "exec"
        assert result["retry_count"] == 1

    def test_semantic_wrong(self):
        fc = make_feedback_classifier(max_retries=3)
        result = fc(self._state(
            eval_result={"answers_intent": False},
        ))
        assert result["feedback_type"] == "semantic"

    def test_exhausted_at_max_retries(self):
        fc = make_feedback_classifier(max_retries=2)
        # At retry_count=2, new_count=3 > 2 → exhausted
        result = fc(self._state(retry_count=2, exec_error="still failing"))
        assert result["feedback_type"] == "exhausted"

    def test_increments_retry_count(self):
        fc = make_feedback_classifier(max_retries=5)
        r1 = fc(self._state(retry_count=0, exec_error="err"))
        r2 = fc(self._state(retry_count=r1["retry_count"], exec_error="err"))
        assert r1["retry_count"] == 1
        assert r2["retry_count"] == 2

    def test_exhausted_has_no_actionable_error(self):
        """feedback_classifier with no error and eval passing → still exhausted."""
        fc = make_feedback_classifier(max_retries=3)
        result = fc(self._state(eval_result={"answers_intent": True}))
        assert result["feedback_type"] == "exhausted"


# ── response_formatter ────────────────────────────────────────────────────────

class TestResponseFormatter:
    def _state(self, **kwargs):
        base = {
            "question": "What are total sales?",
            "query_id": None,
            "session_id": None,
            "generated_sql": "SELECT sum(sales) FROM t",
            "query_result": {"columns": ["sum"], "rows": [{"sum": 1000}], "row_count": 1},
            "clarification": None,
            "dlp_blocked": False,
            "governance_error": None,
            "route": "needs_query",
            "connection_display_name": "Test DB",
            "eval_result": {"summary": "Sales total is $1,000.", "insights": ["Revenue is stable"], "follow_up": "", "answers_intent": True},
            "structured_prompt": {},
            "error": None,
            "exec_error": None,
            "token_usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            "llm_latency_ms": 300,
            "execution_time_ms": 50,
            "retry_count": 0,
            "llm_call_count": 3,
            "answer": None,
        }
        base.update(kwargs)
        return base

    def test_all_required_keys_present(self):
        result = response_formatter(self._state())["formatted_response"]
        for key in ("question", "query_id", "session_id", "sql", "results", "answer", "prompt", "error", "metrics"):
            assert key in result, f"Missing key: {key}"

    def test_answer_from_eval_summary(self):
        result = response_formatter(self._state())["formatted_response"]
        assert result["answer"] == "Sales total is $1,000."

    def test_clarification_overrides_answer(self):
        state = self._state(clarification="Could you specify the year?", generated_sql=None)
        result = response_formatter(state)["formatted_response"]
        assert result["answer"] == "Could you specify the year?"

    def test_unsafe_route_gives_safety_message(self):
        state = self._state(route="unsafe", generated_sql=None, eval_result=None)
        result = response_formatter(state)["formatted_response"]
        assert "read-only" in result["answer"].lower() or "select" in result["answer"].lower()

    def test_out_of_scope_gives_scope_message(self):
        state = self._state(route="out_of_scope", generated_sql=None, eval_result=None)
        result = response_formatter(state)["formatted_response"]
        assert "Test DB" in result["answer"]

    def test_dlp_blocked_gives_governance_message(self):
        state = self._state(
            dlp_blocked=True,
            governance_error="Query blocked: references governed keyword.",
            generated_sql="SELECT password FROM users",
            eval_result=None,
        )
        result = response_formatter(state)["formatted_response"]
        assert "blocked" in result["answer"].lower() or "governed" in result["answer"].lower()

    def test_metrics_include_retry_count_and_route(self):
        state = self._state(retry_count=2, route="needs_query")
        result = response_formatter(state)["formatted_response"]
        assert result["metrics"]["retry_count"] == 2
        assert result["metrics"]["route"] == "needs_query"
        assert result["metrics"]["llm_call_count"] == 3

    def test_insights_attached_when_present(self):
        result = response_formatter(self._state())["formatted_response"]
        assert result.get("insights") == ["Revenue is stable"]

    def test_no_insights_key_when_absent(self):
        state = self._state(eval_result={"summary": "ok", "insights": [], "answers_intent": True, "follow_up": ""})
        result = response_formatter(state)["formatted_response"]
        assert "insights" not in result


# ── _extract_table_names ──────────────────────────────────────────────────────

class TestExtractTableNames:
    def test_parses_simple_names(self):
        tables = "- FactSales\n- DimProduct\n- DimCustomer"
        names = _extract_table_names(tables)
        assert names == ["factsales", "dimproduct", "dimcustomer"]

    def test_strips_descriptions(self):
        tables = "- FactSales - Internet sales fact table\n- DimProduct - Product dimension"
        names = _extract_table_names(tables)
        assert "factsales" in names
        assert "description" not in names[0]

    def test_empty_string_returns_empty(self):
        assert _extract_table_names("") == []

    def test_skips_blank_lines(self):
        tables = "- FactSales\n\n\n- DimDate"
        names = _extract_table_names(tables)
        assert len(names) == 2


# ── _extract_sql ──────────────────────────────────────────────────────────────

class TestExtractSql:
    def test_extracts_from_tool_call(self):
        response = {
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "run_sql",
                    "arguments": json.dumps({"sql": "SELECT id FROM users"}),
                },
            }],
        }
        assert _extract_sql(response) == "SELECT id FROM users"

    def test_extracts_from_code_fence(self):
        response = {"content": "Here is the SQL:\n```sql\nSELECT count(*) FROM orders\n```", "tool_calls": []}
        assert _extract_sql(response) == "SELECT count(*) FROM orders"

    def test_extracts_bare_select(self):
        response = {"content": "The query is: SELECT * FROM products WHERE active = TRUE;"}
        assert "SELECT" in _extract_sql(response).upper()

    def test_returns_none_for_no_sql(self):
        response = {"content": "I need more information about the time period."}
        assert _extract_sql(response) is None

    def test_returns_none_for_empty_content(self):
        response = {"content": "", "tool_calls": []}
        assert _extract_sql(response) is None

    def test_prefers_tool_call_over_text(self):
        """When both tool call and text SQL are present, tool call wins."""
        response = {
            "content": "SELECT wrong FROM table",
            "tool_calls": [{
                "id": "c",
                "type": "function",
                "function": {"name": "run_sql", "arguments": json.dumps({"sql": "SELECT correct FROM tbl"})},
            }],
        }
        assert _extract_sql(response) == "SELECT correct FROM tbl"


# ── PromptLoader ──────────────────────────────────────────────────────────────

class TestPromptLoader:
    def test_loads_all_required_prompts(self, prompt_loader):
        for name in [
            "jeen_insights_system",
            "fused_router",
            "memory_answer",
            "memory_summarizer",
            "sql_generator",
            "fused_eval_analytics",
        ]:
            template = prompt_loader.get(name)
            assert len(template) > 50, f"Prompt '{name}' seems too short"

    def test_render_substitutes_placeholders(self, prompt_loader):
        rendered = prompt_loader.render(
            "fused_router",
            question="What are total sales?",
            conversation_summary="No prior conversation.",
            source_description="AdventureWorks",
        )
        assert "What are total sales?" in rendered
        assert "AdventureWorks" in rendered

    def test_get_raises_on_missing_prompt(self, prompt_loader):
        with pytest.raises(KeyError, match="not found"):
            prompt_loader.get("does_not_exist")

    def test_render_raises_on_missing_placeholder(self, prompt_loader):
        with pytest.raises(KeyError):
            prompt_loader.render("fused_router", question="q")  # missing conversation_summary, source_description

    def test_reload_preserves_prompts(self, prompt_loader):
        before = prompt_loader.get("fused_router")
        prompt_loader.reload()
        after = prompt_loader.get("fused_router")
        assert before == after
