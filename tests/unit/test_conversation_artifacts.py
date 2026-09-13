"""Pure-helper tests for per-turn conversation artifacts (no DB)."""

from __future__ import annotations

import decimal
import uuid
from datetime import datetime

from src.agent.conversation_artifacts import (
    ANALYTICS_ITEM_MAX_BYTES,
    ANALYTICS_MAX_ITEMS,
    ANSWER_MAX_BYTES,
    RESULT_KIND_ERROR,
    RESULT_KIND_TABLE,
    RESULT_KIND_TEXT,
    SNAPSHOT_STORED,
    SNAPSHOT_TOO_LARGE,
    build_result_snapshot,
    coerce_json_safe_rows,
    conversation_title_from_question,
    extract_artifact_fields,
    measure_chart_payload,
    measure_json_bytes,
)


def test_coerce_json_safe_rows_handles_dict_and_positional_rows():
    ts = datetime(2026, 1, 2, 3, 4, 5)
    uid = uuid.uuid4()
    rows = [
        {"amount": decimal.Decimal("1.50"), "when": ts, "id": uid, "blob": b"hi"},
        [decimal.Decimal("2"), ts, uid, b"\xff\xfe"],
    ]
    out = coerce_json_safe_rows(rows)
    assert out[0] == {"amount": 1.5, "when": str(ts), "id": str(uid), "blob": "hi"}
    assert out[1][0] == 2.0
    assert out[1][1] == str(ts)
    assert out[1][2] == str(uid)
    assert out[1][3] == "fffe"  # undecodable bytes fall back to hex


def test_build_result_snapshot_stores_full_envelope_under_caps():
    result = {
        "columns": ["a", "b"],
        "rows": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}],
        "row_count": 2,
        "truncated": True,
        "cap": 2,
    }
    snapshot, status, size, row_count = build_result_snapshot(result, max_rows=10, max_bytes=10_000)
    assert status == SNAPSHOT_STORED
    assert row_count == 2
    assert snapshot["columns"] == ["a", "b"]
    assert snapshot["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert snapshot["truncated"] is True and snapshot["cap"] == 2
    assert size == measure_json_bytes(snapshot)


def test_build_result_snapshot_is_all_or_nothing_on_row_cap():
    result = {"columns": ["a"], "rows": [{"a": i} for i in range(11)]}
    snapshot, status, size, row_count = build_result_snapshot(result, max_rows=10, max_bytes=10_000)
    assert snapshot is None
    assert status == SNAPSHOT_TOO_LARGE
    assert size is None
    assert row_count == 11


def test_build_result_snapshot_is_all_or_nothing_on_byte_cap():
    result = {"columns": ["a"], "rows": [{"a": "x" * 500}]}
    snapshot, status, size, _ = build_result_snapshot(result, max_rows=10, max_bytes=100)
    assert snapshot is None
    assert status == SNAPSHOT_TOO_LARGE
    assert size is not None and size > 100


def test_build_result_snapshot_keeps_empty_success_as_stored():
    snapshot, status, _, row_count = build_result_snapshot(
        {"columns": ["a"], "rows": []}, max_rows=10, max_bytes=1000
    )
    assert status == SNAPSHOT_STORED
    assert snapshot == {"columns": ["a"], "rows": [], "row_count": 0}
    assert row_count == 0


def test_extract_artifact_fields_only_keeps_allowlist():
    formatted = {
        "question": "q",
        "answer": "Revenue rose",
        "error": None,
        "metrics": {"input_tokens": 10, "output_tokens": 5, "route": "sql", "secret": "x"},
        "findings": ["f1"],
        "suggestions": ["s1"],
        "followups": ["u1"],
        "prompt": {"system": "SCHEMA DUMP"},
        "node_prompts": {"sql_generator": "PROMPT"},
        "results": {"rows": [[1]]},
        "trace": [{"node": "x"}],
    }
    fields = extract_artifact_fields(formatted, has_sql=True, exec_error=None)
    assert set(fields) == {
        "result_kind", "answer", "error", "metrics", "findings", "suggestions", "followups"
    }
    assert fields["result_kind"] == RESULT_KIND_TABLE
    assert fields["answer"] == "Revenue rose"
    assert fields["metrics"] == {"input_tokens": 10, "output_tokens": 5, "route": "sql"}
    assert "prompt" not in str(fields)
    assert "SCHEMA DUMP" not in str(fields)
    assert "PROMPT" not in str(fields)


def test_extract_artifact_fields_result_kinds():
    assert extract_artifact_fields({"answer": "hi"}, has_sql=False, exec_error=None)["result_kind"] == RESULT_KIND_TEXT
    assert extract_artifact_fields({}, has_sql=True, exec_error="boom")["result_kind"] == RESULT_KIND_ERROR
    # A formatter-level error (unsafe / out of scope) also counts as an error turn.
    assert extract_artifact_fields({"error": "blocked"}, has_sql=False, exec_error=None)["result_kind"] == RESULT_KIND_ERROR


def test_extract_artifact_fields_applies_caps():
    big = "x" * (ANSWER_MAX_BYTES + 100)
    many = [f"item {i}" for i in range(ANALYTICS_MAX_ITEMS + 5)]
    long_item = ["y" * (ANALYTICS_ITEM_MAX_BYTES + 50)]
    fields = extract_artifact_fields(
        {"answer": big, "findings": many, "suggestions": long_item},
        has_sql=False,
        exec_error=None,
    )
    assert len(fields["answer"].encode("utf-8")) <= ANSWER_MAX_BYTES
    assert len(fields["findings"]) == ANALYTICS_MAX_ITEMS
    assert len(fields["suggestions"][0].encode("utf-8")) <= ANALYTICS_ITEM_MAX_BYTES


def test_extract_artifact_fields_keeps_rich_answer_fragments():
    answer = [{"t": "Revenue ", "hl": None}, {"t": "rose", "hl": "pos"}]
    fields = extract_artifact_fields({"answer": answer}, has_sql=False, exec_error=None)
    assert fields["answer"] == answer


def test_measure_chart_payload_counts_spec_and_config():
    small = measure_chart_payload({"chart_type": "bar"}, {"series": []})
    bigger = measure_chart_payload({"chart_type": "bar"}, {"series": [{"data": list(range(100))}]})
    assert bigger > small > 0


def test_conversation_title_from_question_caps_length():
    assert conversation_title_from_question("  hello   world ") == "hello world"
    assert conversation_title_from_question("") == "Conversation"
    long = conversation_title_from_question("w" * 500)
    assert len(long) <= 200
