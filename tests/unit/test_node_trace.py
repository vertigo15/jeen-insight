"""The projection of the execution trace that gets stored.

Every node is wrapped by a timing helper, but the resulting trace is built for
the developer panel: it carries the fully rendered LLM prompt for each model
call and prose detail lines that can quote generated SQL. ``slim_trace`` is the
gate between that and the ``node_trace`` column — anything it lets through is
written to the database and kept for the life of the row.
"""

from __future__ import annotations

from src.agent.langgraph_agent.nodes.output import slim_trace, stamp_customer_table


class TestSlimTrace:
    def test_drops_prompts_and_details(self):
        events = [{
            "node": "sql_generator",
            "elapsed_ms": 812,
            "type": "llm",
            "icon": "🧠",
            "detail": "SELECT * FROM salaries",
            "prompt": "You are a SQL expert. Schema: employees.ssn …",
        }]
        assert slim_trace(events) == [
            {"node": "sql_generator", "elapsed_ms": 812, "type": "llm"}
        ]

    def test_keeps_repeats_in_order_so_retries_stay_visible(self):
        """A second sql_generator entry is a repair attempt.

        Collapsing duplicates would hide exactly the pathology this data exists
        to surface, so order and repetition are both part of the contract.
        """
        events = [
            {"node": "sql_generator", "elapsed_ms": 800, "type": "llm"},
            {"node": "execute_query", "elapsed_ms": 40, "type": "db"},
            {"node": "sql_generator", "elapsed_ms": 900, "type": "llm"},
        ]
        assert [e["node"] for e in slim_trace(events)] == [
            "sql_generator", "execute_query", "sql_generator",
        ]

    def test_tolerates_malformed_entries(self):
        events = [
            {"elapsed_ms": 5, "type": "llm"},            # no node — unusable
            {"node": "router", "elapsed_ms": None},      # missing timing
            {"node": "output", "elapsed_ms": "abc"},     # non-numeric timing
        ]
        assert slim_trace(events) == [
            {"node": "router", "elapsed_ms": 0, "type": "logic"},
            {"node": "output", "elapsed_ms": 0, "type": "logic"},
        ]

    def test_empty_input(self):
        assert slim_trace([]) == []
        assert slim_trace(None) == []

    def test_keeps_a_delivered_table_flag_only(self):
        events = [{
            "node": "trivial_result_check",
            "elapsed_ms": 4,
            "type": "logic",
            "shown": True,
            "prompt": "do not store",
            "detail": "SELECT secret",
        }]
        assert slim_trace(events) == [{
            "node": "trivial_result_check", "elapsed_ms": 4, "type": "logic", "shown": True,
        }]
        assert slim_trace([{"node": "execute_query", "elapsed_ms": 1, "shown": False}]) == [
            {"node": "execute_query", "elapsed_ms": 1, "type": "logic"},
        ]


class TestStampCustomerTable:
    def test_records_shown_and_drops_the_wrapper_signal(self):
        out = stamp_customer_table(
            {"is_trivial": False, "customer_table_shown": True, "partial_revision": 1},
            {"node": "trivial_result_check", "elapsed_ms": 3, "type": "logic"},
        )
        assert "customer_table_shown" not in out
        assert out["trace"] == [{
            "node": "trivial_result_check", "elapsed_ms": 3, "type": "logic", "shown": True,
        }]

    def test_omits_shown_when_the_table_was_not_delivered(self):
        out = stamp_customer_table(
            {"is_trivial": True},
            {"node": "trivial_result_check", "elapsed_ms": 3, "type": "logic"},
        )
        assert out["trace"] == [{
            "node": "trivial_result_check", "elapsed_ms": 3, "type": "logic",
        }]
