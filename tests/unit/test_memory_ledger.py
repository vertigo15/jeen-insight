"""Turn ledger (nodes/context.py): the compact memory every prompt reads."""

from __future__ import annotations

import json

from src.agent.langgraph_agent.nodes.context import (
    DATA_NONE,
    DATA_RERUN,
    DATA_STORED,
    build_ledger,
    context_composer,
    estimate_tokens,
    find_turn,
    latest_turn_with_data,
    ledger_for,
    render_ledger,
    render_turn_tool_result,
)


def _artifact(**kw):
    base = {
        "columns": ["orderyear", "total"],
        "column_types": {"orderyear": "int", "total": "float"},
        "row_count": 12,
        "stats": {"total": {"non_null": 12, "min": 25000000, "max": 29000000}},
    }
    base.update(kw)
    return base


HISTORY = [
    {"id": "q1", "natural_language_query": "hello", "generated_sql": None, "result_kind": "text",
     "answer": "Hello! I'm Jeen Insights."},
    {"id": "q2", "natural_language_query": "total sales by year",
     "generated_sql": "SELECT orderyear, SUM(total) AS total FROM s GROUP BY 1",
     "result_artifact": _artifact(), "snapshot_status": "stored", "result_kind": "table",
     "answer": "Sales grew every year from 25M to 29M."},
    {"id": "q3", "natural_language_query": "top products", "generated_sql": "SELECT p FROM t",
     "result_artifact": json.dumps(_artifact(row_count=5000)), "snapshot_status": "too_large",
     "result_kind": "table", "answer": "x" * 500},
    {"id": None, "natural_language_query": "", "generated_sql": None},  # skipped: no question
]


class TestBuildLedger:
    def test_handles_are_oldest_first_and_skip_blank_questions(self):
        ledger = build_ledger(HISTORY)
        assert [t["handle"] for t in ledger] == ["T1", "T2", "T3"]
        assert ledger[0]["query_id"] == "q1"
        assert ledger[-1]["question"] == "top products"

    def test_data_status_reflects_recoverability(self):
        ledger = build_ledger(HISTORY)
        assert ledger[0]["data_status"] == DATA_NONE       # text turn
        assert ledger[1]["data_status"] == DATA_STORED     # snapshot stored
        assert ledger[2]["data_status"] == DATA_RERUN      # too large, but SQL exists

    def test_artifact_fields_and_answer_truncation(self):
        ledger = build_ledger(HISTORY)
        t2 = ledger[1]
        assert t2["columns"] == ["orderyear", "total"]
        assert t2["row_count"] == 12
        assert t2["stats"]["total"]["min"] == 25000000
        # JSON-string artifacts are parsed; long answers are capped to one line.
        assert ledger[2]["row_count"] == 5000
        assert len(ledger[2]["answer"]) <= 200 and ledger[2]["answer"].endswith("…")

    def test_empty_history(self):
        assert build_ledger([]) == []
        assert build_ledger(None) == []


class TestLookups:
    def test_find_turn_accepts_handle_variants_and_query_id(self):
        ledger = build_ledger(HISTORY)
        assert find_turn(ledger, "T2")["query_id"] == "q2"
        assert find_turn(ledger, "t2")["query_id"] == "q2"
        assert find_turn(ledger, "2")["query_id"] == "q2"
        assert find_turn(ledger, "q3")["handle"] == "T3"
        assert find_turn(ledger, "T9") is None
        assert find_turn(ledger, "") is None

    def test_latest_turn_with_data_skips_text_turns(self):
        ledger = build_ledger(HISTORY)
        assert latest_turn_with_data(ledger)["handle"] == "T3"
        assert latest_turn_with_data(build_ledger(HISTORY[:1])) is None

    def test_ledger_for_prefers_state_ledger(self):
        state = {"memory_ledger": [{"handle": "T1"}], "conversation_history": HISTORY}
        assert ledger_for(state) == [{"handle": "T1"}]
        assert [t["handle"] for t in ledger_for({"conversation_history": HISTORY})] == ["T1", "T2", "T3"]


class TestRendering:
    def test_render_ledger_lists_every_turn_with_shape_answer_and_data(self):
        text = render_ledger(build_ledger(HISTORY))
        assert text.startswith("Prior turns in this conversation")
        assert 'T2 · Q: "total sales by year"' in text
        assert "SQL: SELECT orderyear" in text
        assert "result: 12 rows; cols: orderyear(int), total(float); total: 25000000..29000000" in text
        assert 'A: "Sales grew every year from 25M to 29M."' in text
        assert "data: stored" in text and "data: rerun" in text and "data: none" in text
        assert "T3 (most recent)" in text

    def test_render_ledger_can_omit_sql(self):
        assert "SQL:" not in render_ledger(build_ledger(HISTORY), include_sql=False)
        assert render_ledger([]) == ""

    def test_tool_result_line(self):
        ledger = build_ledger(HISTORY)
        assert render_turn_tool_result(ledger[1]) == (
            "12 rows; columns: orderyear(int), total(float); answer: Sales grew every year from 25M to 29M."
        )
        assert render_turn_tool_result({"handle": "T9"}) == "Query executed successfully."


class TestComposerNode:
    def test_sets_ledger_and_telemetry(self):
        out = context_composer({"conversation_history": HISTORY, "memory_window": 5})
        assert [t["handle"] for t in out["memory_ledger"]] == ["T1", "T2", "T3"]
        tel = out["memory_telemetry"]
        assert tel["turns_loaded"] == 4 and tel["window"] == 5 and tel["ledger_turns"] == 3
        assert tel["turns_with_data"] == 2
        assert tel["window_saturated"] is False
        assert tel["ledger_tokens_est"] == estimate_tokens(render_ledger(out["memory_ledger"]))

    def test_window_saturation_flag(self):
        out = context_composer({"conversation_history": HISTORY, "memory_window": 4})
        assert out["memory_telemetry"]["window_saturated"] is True

    def test_no_history(self):
        out = context_composer({"conversation_history": [], "memory_window": 5})
        assert out["memory_ledger"] == []
        assert out["memory_telemetry"]["ledger_tokens_est"] == 0
