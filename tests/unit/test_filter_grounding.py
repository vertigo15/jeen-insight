"""Focused regression tests for SQL filter planning and grounding."""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent.nodes.filtering import (
    empty_filter_result_check,
    make_filter_grounder,
    normalize_typed_filter,
)


class _Runner:
    database_type = "postgres"

    def __init__(self, values):
        self.values = values
        self.sql = ""

    async def run_sql(self, sql, **_kwargs):
        self.sql = sql
        return {"rows": [{"value": value} for value in self.values]}


def test_normalizes_numeric_and_iso_date_ranges():
    number, error = normalize_typed_filter(
        {"op": "between", "value": ["1,000.50", "2000"]}, "decimal"
    )
    assert error is None
    assert number["value"] == ["1000.5", "2000"]
    assert number["resolved"] is True

    dates, error = normalize_typed_filter(
        {"op": "between", "value": ["2026-01-01", "2026-01-31"]}, "date"
    )
    assert error is None
    assert dates["value"] == ["2026-01-01", "2026-01-31"]


def test_rejects_ambiguous_date_format():
    _, error = normalize_typed_filter(
        {"op": "equals", "value": "03/04/2026"}, "date"
    )
    assert "couldn't read" in error.lower()


@pytest.mark.asyncio
async def test_grounder_rewrites_a_typo_to_one_canonical_value():
    runner = _Runner(["Mountain-300"])
    grounder = make_filter_grounder(runner)
    state = {
        "catalog_source_used": "db",
        "source_key": "sales",
        "user_id": "user-1",
        "filter_plan": {
            "filters": [{
                "table": "product",
                "column": "name",
                "op": "equals",
                "value": "mountain 300",
                "data_type": "varchar",
                "resolved": False,
            }]
        },
    }

    result = await grounder(state)

    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["value"] == "Mountain-300"
    assert result["filter_plan"]["filters"][0]["op"] == "equals"
    assert "SELECT DISTINCT" in runner.sql


@pytest.mark.asyncio
async def test_grounder_requests_clarification_for_ambiguous_value():
    runner = _Runner(["East", "West"])
    grounder = make_filter_grounder(runner, match_threshold=1)
    state = {
        "catalog_source_used": "db",
        "source_key": "sales",
        "user_id": "user-1",
        "filter_plan": {
            "filters": [{
                "table": "orders",
                "column": "region",
                "op": "equals",
                "value": "region",
                "data_type": "text",
                "resolved": False,
            }]
        },
    }

    result = await grounder(state)

    assert result["filter_clarification_required"] is True
    assert result["filter_ambiguities"][0]["candidates"] == [
        "East", "West"
    ]


def test_empty_result_diagnostic_is_bounded():
    state = {
        "query_result": {"rows": []},
        "unresolved_filters": [{"target": "orders.status"}],
        "empty_filter_diagnostics": 0,
    }
    first = empty_filter_result_check(state)
    assert first["needs_filter_reground"] is True
    second = empty_filter_result_check({**state, **first})
    assert second["needs_filter_reground"] is False
