"""Regression tests for numeric answers that bypass AI analytics."""

from __future__ import annotations

from decimal import Decimal

from src.agent.langgraph_agent.nodes.output import response_formatter


def _format_trivial_answer(value):
    return response_formatter(
        {
            "route": "needs_query",
            "is_trivial": True,
            "query_result": {
                "columns": ["total_internet_sales"],
                "rows": [{"total_internet_sales": value}],
            },
        }
    )["formatted_response"]["answer"]


def test_large_trivial_result_matches_table_number_formatting():
    assert _format_trivial_answer(Decimal("29358677.89")) == (
        "Total Internet Sales: 29,358,678"
    )


def test_small_trivial_result_keeps_up_to_four_decimal_places():
    assert _format_trivial_answer(Decimal("12.34567")) == (
        "Total Internet Sales: 12.3457"
    )
