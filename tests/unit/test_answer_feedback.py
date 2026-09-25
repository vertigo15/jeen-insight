"""Answer feedback: the append-only log behind thumbs and the Give feedback dialog.

Covers the request model, the /api/answer-feedback route (ownership, schema
gate, error mapping, untrusted connection), the legacy /api/feedback thumb
path, the conversation DTO field, the SQL that derives the current thumb, and
the shape of migration 035. Offline: the history service is a MagicMock.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.agent.conversation_history import ConversationHistoryService
from src.api.models import AnswerFeedbackRequest, AnswerFeedbackResponse, ConversationTurn

ROOT = Path(__file__).resolve().parents[2]
QUERY_ID = "11111111-1111-1111-1111-111111111111"
SAVED = {"id": "22222222-2222-2222-2222-222222222222", "thumb": "thumbs_up", "source_key": "sales_db"}


# ----------------------------------------------------------------------
# Request model
# ----------------------------------------------------------------------
def test_request_accepts_thumb_only_and_dialog_only_events():
    thumb = AnswerFeedbackRequest(query_id=QUERY_ID, thumb="cleared")
    assert thumb.rating is None and thumb.message is None

    dialog = AnswerFeedbackRequest(
        query_id=QUERY_ID, rating=5, feedback_type="ui_bug", message="  legend overlaps  "
    )
    assert dialog.message == "legend overlaps"  # trimmed


def test_request_rejects_empty_events_and_out_of_range_values():
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID)
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID, message="   ")  # blank counts as empty
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID, rating=0)
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID, rating=6)
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID, thumb="meh")
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID, feedback_type="praise")
    with pytest.raises(ValidationError):
        AnswerFeedbackRequest(query_id=QUERY_ID, thumb="thumbs_up", notes="extra")  # extra=forbid


def test_conversation_turn_dto_carries_the_current_thumb():
    # Without this field pydantic strips user_feedback from the response and
    # the card can never render its pressed state after a reload.
    assert "user_feedback" in ConversationTurn.model_fields
    assert AnswerFeedbackResponse(feedback_id="x").thumb is None


# ----------------------------------------------------------------------
# /api/answer-feedback route
# ----------------------------------------------------------------------
def test_answer_feedback_requires_authentication(anon_client, fake_state):
    resp = anon_client.post("/api/answer-feedback", json={"query_id": QUERY_ID, "thumb": "thumbs_up"})
    assert resp.status_code == 401


def test_answer_feedback_is_503_before_migration_035(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = False
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    resp = client.post("/api/answer-feedback", json={"query_id": QUERY_ID, "thumb": "thumbs_up"})
    assert resp.status_code == 503
    fake_state.history_service.record_answer_feedback.assert_not_awaited()


def test_answer_feedback_writes_for_the_principal_and_ignores_client_connection(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.get_turn_analysis = AsyncMock(return_value=None)
    resp = client.post(
        "/api/answer-feedback",
        json={
            "query_id": QUERY_ID,
            "rating": 4,
            "feedback_type": "report_bug",
            "message": "wrong total",
            "connection": "someone_elses_db",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "feedback_id": SAVED["id"], "thumb": "thumbs_up"}
    kwargs = fake_state.history_service.record_answer_feedback.await_args.kwargs
    assert kwargs["user_id"] == "user-a"  # from the token, never the body
    assert kwargs["query_id"] == UUID(QUERY_ID)
    assert kwargs == {
        "query_id": UUID(QUERY_ID), "user_id": "user-a", "thumb": None,
        "rating": 4, "feedback_type": "report_bug", "message": "wrong total",
    }
    # The log enrichment uses the turn's own source_key, not the client's.
    log_kwargs = fake_state.history_service.get_turn_analysis.await_args.kwargs
    assert log_kwargs["source_key"] == "sales_db"


def test_answer_feedback_404_when_turn_is_not_owned(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=None)
    resp = client.post("/api/answer-feedback", json={"query_id": QUERY_ID, "thumb": "thumbs_down"})
    assert resp.status_code == 404


def test_answer_feedback_db_fault_is_500_not_404(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(side_effect=RuntimeError("pool down"))
    resp = client.post("/api/answer-feedback", json={"query_id": QUERY_ID, "thumb": "thumbs_down"})
    assert resp.status_code == 500


def test_answer_feedback_rejects_invalid_payloads(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    assert client.post("/api/answer-feedback", json={"query_id": QUERY_ID}).status_code == 422
    assert client.post("/api/answer-feedback", json={"query_id": QUERY_ID, "rating": 9}).status_code == 422
    fake_state.history_service.record_answer_feedback.assert_not_awaited()


# ----------------------------------------------------------------------
# Legacy /api/feedback keeps working and routes thumbs to the log
# ----------------------------------------------------------------------
def test_legacy_feedback_thumb_goes_to_the_log_when_035_is_present(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    resp = client.post("/api/feedback", json={"query_id": QUERY_ID, "feedback": "thumbs_up", "notes": "nice"})
    assert resp.status_code == 200
    kwargs = fake_state.history_service.record_answer_feedback.await_args.kwargs
    assert kwargs["thumb"] == "thumbs_up" and kwargs["message"] == "nice" and kwargs["user_id"] == "user-a"
    # No corrected_sql, so the turn row is left alone.
    fake_state.history_service.record_feedback.assert_not_awaited()


def test_legacy_feedback_catalog_gap_stays_on_the_turn_row(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    resp = client.post("/api/feedback", json={"query_id": QUERY_ID, "feedback": "catalog_gap", "notes": "no table"})
    assert resp.status_code == 200
    fake_state.history_service.record_answer_feedback.assert_not_awaited()
    assert fake_state.history_service.record_feedback.await_args.kwargs["user_feedback"] == "catalog_gap"


def test_legacy_feedback_thumb_falls_back_to_turn_row_before_035(client, fake_state):
    fake_state.history_service.answer_feedback_schema_ready = False
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    resp = client.post("/api/feedback", json={"query_id": QUERY_ID, "feedback": "thumbs_down"})
    assert resp.status_code == 200
    fake_state.history_service.record_answer_feedback.assert_not_awaited()
    assert fake_state.history_service.record_feedback.await_args.kwargs["user_feedback"] == "thumbs_down"


# ----------------------------------------------------------------------
# Service SQL: how the current thumb is derived
# ----------------------------------------------------------------------
def _service(*, ready: bool) -> ConversationHistoryService:
    service = ConversationHistoryService.__new__(ConversationHistoryService)
    service.answer_feedback_schema_ready = ready
    service.analysis_schema_ready = False
    service.favorite_schema_ready = False
    return service


def test_thumb_select_orders_by_event_seq_and_reads_cleared_as_none():
    sql = _service(ready=True)._thumb_select("cs")
    assert "insights_answer_feedback" in sql
    assert "ORDER BY f.event_seq DESC LIMIT 1" in sql
    assert "created_at" not in sql  # timestamps can tie; the identity column cannot
    # A withdrawn thumb must not fall back to the legacy column.
    assert re.search(r"NULLIF\(COALESCE\(.*cs\.user_feedback\), 'cleared'\)", sql, re.S)
    assert sql.strip().endswith("AS user_feedback")


def test_thumb_select_uses_legacy_column_before_035():
    assert _service(ready=False)._thumb_select("cs") == ", cs.user_feedback"


def test_turn_row_only_exposes_real_thumbs():
    def row(value):
        base = {
            "id": UUID(QUERY_ID), "sequence_number": 1, "natural_language_query": "q",
            "generated_sql": "select 1", "execution_status": "success", "row_count": 1,
            "error_message": None, "created_at": None, "result_kind": "table", "answer": None,
            "artifact_error": None, "metrics": None, "findings": None, "suggestions": None,
            "followups": None, "snapshot_status": "pruned", "snapshot_at": None, "has_chart": False,
            "user_feedback": value,
        }
        return base

    service = _service(ready=True)
    assert service._turn_row(row("thumbs_up"))["user_feedback"] == "thumbs_up"
    assert service._turn_row(row("thumbs_down"))["user_feedback"] == "thumbs_down"
    # Legacy non-quality values are not pressed states.
    assert service._turn_row(row("catalog_gap"))["user_feedback"] is None
    assert service._turn_row(row("edited"))["user_feedback"] is None
    assert service._turn_row(row(None))["user_feedback"] is None


@pytest.mark.asyncio
async def test_record_answer_feedback_is_a_noop_without_035():
    service = _service(ready=False)
    assert await service.record_answer_feedback(query_id=UUID(QUERY_ID), user_id="u", thumb="thumbs_up") is None


# ----------------------------------------------------------------------
# Migration 035 shape
# ----------------------------------------------------------------------
def test_migration_035_defines_a_documented_append_only_log():
    sql = (ROOT / "db/migrations/insights/035_answer_feedback.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS insights_answer_feedback" in sql
    assert "event_seq" in sql and "GENERATED ALWAYS AS IDENTITY" in sql
    assert "REFERENCES insights_conversation_sessions(id) ON DELETE CASCADE" in sql
    # Values the API accepts must be exactly what the CHECKs admit.
    assert "thumb IN ('thumbs_up', 'thumbs_down', 'cleared')" in sql
    assert "rating BETWEEN 1 AND 5" in sql
    assert "feedback_type IN ('general', 'report_bug', 'ui_bug', 'other')" in sql
    assert "char_length(message) <= 4000" in sql
    assert "chk_insights_answer_feedback_not_empty" in sql
    # Index that serves "latest thumb per turn".
    assert "ON insights_answer_feedback (query_id, event_seq DESC)" in sql
    # Historical thumbs are carried over, idempotently.
    assert "INSERT INTO insights_answer_feedback (query_id, user_id, source_key, thumb, created_at)" in sql
    assert "NOT EXISTS" in sql
    # Documented in the DB: the table and every column carry a comment.
    assert "COMMENT ON TABLE insights_answer_feedback" in sql
    for column in ("event_seq", "query_id", "user_id", "source_key", "thumb", "rating", "feedback_type", "message", "created_at"):
        assert f"COMMENT ON COLUMN insights_answer_feedback.{column} IS" in sql, column
    # Shared metadata DB: only Insights-owned objects.
    assert not re.search(r"\b(metadata_|settings_services|admin_)\w*", sql)
