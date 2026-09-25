"""/api/feedback — the values the DB constraint admits, notes reach the store,
and a thumbs-down on an ML answer is logged with the model it judged."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

_QID = "11111111-1111-1111-1111-111111111111"


def test_feedback_rejects_values_the_constraint_would_refuse(client, fake_state):
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    r = client.post("/api/feedback", json={"query_id": _QID, "feedback": "meh"})
    assert r.status_code == 422
    fake_state.history_service.record_feedback.assert_not_awaited()


@pytest.mark.parametrize("value", ["thumbs_up", "thumbs_down", "edited", "catalog_gap"])
def test_feedback_accepts_every_constraint_value_and_forwards_notes(client, fake_state, value):
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    r = client.post("/api/feedback", json={"query_id": _QID, "feedback": value, "notes": "  the horizon looked off  "})
    assert r.status_code == 200, r.text
    kw = fake_state.history_service.record_feedback.await_args.kwargs
    assert kw["user_feedback"] == value
    # ``notes`` is the field the store writes; the old client sent ``comment`` and it was dropped.
    assert kw["feedback_notes"] == "  the horizon looked off  "
    assert kw["user_id"] == "user-a"


def test_feedback_notes_are_bounded(client, fake_state):
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    r = client.post("/api/feedback", json={"query_id": _QID, "feedback": "thumbs_down", "notes": "x" * 4001})
    assert r.status_code == 422


def test_feedback_on_an_ml_turn_logs_the_method_it_judged(client, fake_state, caplog):
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    fake_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "analysis": {"skill": "forecast", "method_used": "AutoETS",
                     "validation": {"metric": "WAPE", "value": 0.31, "band": "poor", "coverage": 0.6}},
        "low_confidence": False,
    })
    with caplog.at_level(logging.INFO, logger="src.api.routes.history"):
        r = client.post("/api/feedback", json={"query_id": _QID, "feedback": "thumbs_down", "connection": "sales_db"})
    assert r.status_code == 200
    fake_state.history_service.get_turn_analysis.assert_awaited_once()
    assert fake_state.history_service.get_turn_analysis.await_args.kwargs["source_key"] == "sales_db"
    line = next(rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("result_feedback "))
    event = json.loads(line[len("result_feedback "):])
    assert event["feedback"] == "thumbs_down" and event["skill"] == "forecast"
    assert event["method"] == "AutoETS" and event["band"] == "poor" and event["coverage"] == 0.6
    assert event["has_notes"] is False


def test_feedback_without_connection_logs_without_a_turn_lookup(client, fake_state, caplog):
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    fake_state.history_service.get_turn_analysis = AsyncMock()
    with caplog.at_level(logging.INFO, logger="src.api.routes.history"):
        r = client.post("/api/feedback", json={"query_id": _QID, "feedback": "thumbs_up"})
    assert r.status_code == 200
    fake_state.history_service.get_turn_analysis.assert_not_awaited()
    assert any(rec.getMessage().startswith("result_feedback ") for rec in caplog.records)
