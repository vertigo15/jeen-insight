"""Where usage events are written from: save_to_memory (one query event per
turn with an explicit outcome), the feedback routes (mirror of the 035 row),
the analysis audit callback (independent of AuditService) and the Flask login
path (same transaction, savepoint-guarded). Offline.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from src.agent.langgraph_agent.nodes.output import make_save_to_memory

TURN_ID = UUID("11111111-1111-1111-1111-111111111111")
SAVED = {"id": "22222222-2222-2222-2222-222222222222", "thumb": "thumbs_up", "source_key": "sales_db",
         "question": "revenue by month", "session_id": UUID("33333333-3333-3333-3333-333333333333")}


def _history():
    history = MagicMock()
    history.persistence_enabled = True
    history.update_llm_response = AsyncMock()
    history.update_execution = AsyncMock()
    history.upsert_turn_artifact = AsyncMock(return_value=True)
    return history


def _node(ledger):
    return make_save_to_memory(_history(), "gpt-5.1", usage_ledger=ledger)


def _base_state(**extra):
    state = {
        "query_id": TURN_ID, "user_id": "7", "source_key": "sales_db", "route": "needs_query",
        "question": "revenue by month", "token_usage": {"total_tokens": 100, "input_tokens": 80},
        "llm_latency_ms": 300, "execution_time_ms": 20, "start_time": None,
    }
    state.update(extra)
    return state


# ── save_to_memory ───────────────────────────────────────────────────────

def test_successful_sql_turn_records_a_success_query_event():
    ledger = MagicMock(); ledger.record_query = AsyncMock()
    asyncio.run(_node(ledger)(_base_state(
        generated_sql="select 1", query_result={"columns": ["a"], "rows": [{"a": 1}, {"a": 2}]},
        formatted_response={"answer": "two rows"},
    )))
    ledger.record_query.assert_awaited_once()
    k = ledger.record_query.await_args.kwargs
    assert k["outcome"] == "success" and k["error_type"] is None
    assert k["user_id"] == "7" and k["source_key"] == "sales_db" and k["query_id"] == TURN_ID
    assert k["route"] == "needs_query" and k["llm_model"] == "gpt-5.1" and k["row_count"] == 2
    assert k["token_usage"] == {"total_tokens": 100, "input_tokens": 80} and k["question"] == "revenue by month"


def test_execution_error_records_error_with_connector_error_type():
    ledger = MagicMock(); ledger.record_query = AsyncMock()
    asyncio.run(_node(ledger)(_base_state(
        generated_sql="select 1", exec_error="relation does not exist",
        query_result={"error_type": "sql_error"}, formatted_response={"error": "relation does not exist"},
    )))
    k = ledger.record_query.await_args.kwargs
    assert k["outcome"] == "error" and k["error_type"] == "sql_error"
    assert k["detail"] == {"connector_error_type": "sql_error"}


def test_blocked_sql_records_a_validation_error():
    ledger = MagicMock(); ledger.record_query = AsyncMock()
    asyncio.run(_node(ledger)(_base_state(
        generated_sql="select * from private.users", query_result={},
        formatted_response={"error": "Blocked by governance", "answer": None},
    )))
    k = ledger.record_query.await_args.kwargs
    assert k["outcome"] == "error" and k["error_type"] == "validation"


def test_text_only_turn_is_a_success_that_keeps_its_route():
    ledger = MagicMock(); ledger.record_query = AsyncMock()
    asyncio.run(_node(ledger)(_base_state(route="greeting", formatted_response={"answer": "Hello!"})))
    k = ledger.record_query.await_args.kwargs
    assert k["outcome"] == "success" and k["route"] == "greeting" and k["row_count"] == 0


def test_guard_refusal_records_refused_with_the_skill():
    ledger = MagicMock(); ledger.record_query = AsyncMock()
    asyncio.run(_node(ledger)(_base_state(
        route="needs_analysis", analysis_skill="forecast",
        analysis_guard_failure={"message": "too few points"},
        formatted_response={"answer": "too few points", "proposal": {"kind": "guard"}},
        query_result={"columns": [], "rows": []},
    )))
    k = ledger.record_query.await_args.kwargs
    assert k["outcome"] == "refused" and k["skill"] == "forecast" and k["error_type"] is None


def test_ledger_failure_never_breaks_the_turn_and_no_ledger_is_fine():
    ledger = MagicMock(); ledger.record_query = AsyncMock(side_effect=RuntimeError("db down"))
    asyncio.run(_node(ledger)(_base_state(generated_sql="select 1", query_result={"rows": [{"a": 1}]},
                                          formatted_response={"answer": "x"})))
    asyncio.run(make_save_to_memory(_history(), "gpt")(_base_state(formatted_response={"answer": "Hi"})))


def test_no_query_id_means_no_event():
    ledger = MagicMock(); ledger.record_query = AsyncMock()
    asyncio.run(_node(ledger)({"formatted_response": {"answer": "Hi"}}))
    ledger.record_query.assert_not_awaited()


# ── feedback routes mirror ───────────────────────────────────────────────

def test_answer_feedback_route_mirrors_the_event_into_the_ledger(client, fake_state, monkeypatch):
    from src.api import state as api_state

    ledger = MagicMock(); ledger.record_feedback = AsyncMock()
    monkeypatch.setattr(api_state, "usage_ledger", ledger)
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.get_turn_analysis = AsyncMock(return_value=None)

    resp = client.post("/api/answer-feedback", json={
        "query_id": str(TURN_ID), "thumb": "thumbs_down", "rating": 2, "feedback_type": "report_bug", "message": "wrong total",
    })
    assert resp.status_code == 200
    ledger.record_feedback.assert_awaited_once()
    k = ledger.record_feedback.await_args.kwargs
    assert k == {
        "user_id": "user-a", "source_key": "sales_db", "query_id": TURN_ID, "session_id": SAVED["session_id"],
        "feedback_id": SAVED["id"], "thumb": "thumbs_down", "rating": 2, "feedback_type": "report_bug",
        "message": "wrong total", "question": "revenue by month",
    }


def test_legacy_feedback_thumb_is_mirrored_too(client, fake_state, monkeypatch):
    from src.api import state as api_state

    ledger = MagicMock(); ledger.record_feedback = AsyncMock()
    monkeypatch.setattr(api_state, "usage_ledger", ledger)
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)
    fake_state.history_service.get_turn_analysis = AsyncMock(return_value=None)
    assert client.post("/api/feedback", json={"query_id": str(TURN_ID), "feedback": "thumbs_up", "notes": "nice"}).status_code == 200
    k = ledger.record_feedback.await_args.kwargs
    assert k["thumb"] == "thumbs_up" and k["message"] == "nice" and k["feedback_id"] == SAVED["id"]


def test_feedback_mirror_is_skipped_when_write_failed_or_not_owned(client, fake_state, monkeypatch):
    from src.api import state as api_state

    ledger = MagicMock(); ledger.record_feedback = AsyncMock()
    monkeypatch.setattr(api_state, "usage_ledger", ledger)
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=None)
    assert client.post("/api/answer-feedback", json={"query_id": str(TURN_ID), "thumb": "thumbs_up"}).status_code == 404
    ledger.record_feedback.assert_not_awaited()


def test_feedback_mirror_failure_does_not_fail_the_request(client, fake_state, monkeypatch):
    from src.api import state as api_state

    ledger = MagicMock(); ledger.record_feedback = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(api_state, "usage_ledger", ledger)
    fake_state.history_service.answer_feedback_schema_ready = True
    fake_state.history_service.record_answer_feedback = AsyncMock(return_value=SAVED)
    fake_state.history_service.get_turn_analysis = AsyncMock(return_value=None)
    assert client.post("/api/answer-feedback", json={"query_id": str(TURN_ID), "thumb": "thumbs_up"}).status_code == 200


# ── analysis audit callback ──────────────────────────────────────────────

def test_analysis_audit_writes_the_ledger_even_without_an_audit_service(monkeypatch):
    from src.api import lifespan
    from src.api import state as api_state

    ledger = MagicMock(); ledger.record_analysis = AsyncMock()
    monkeypatch.setattr(api_state, "usage_ledger", ledger)
    monkeypatch.setattr(api_state, "audit_service", None)
    asyncio.run(lifespan._analysis_audit({
        "user_id": "7", "source_key": "sales_db", "query_id": str(TURN_ID), "skill": "forecast",
        "outcome": "guard_failed", "elapsed_ms": 120, "runner": "sandbox",
        "refused_by": [{"guard": "min_points", "observed": 3, "required": 12}],
        "params": {"filters": {"region": "EMEA"}},
    }))
    ledger.record_analysis.assert_awaited_once()
    k = ledger.record_analysis.await_args.kwargs
    assert k["skill"] == "forecast" and k["outcome"] == "guard_failed" and k["execution_time_ms"] == 120
    assert k["detail"]["refused_by"] == [{"guard": "min_points", "observed": 3, "required": 12}]
    assert "params" not in k["detail"]                              # allowlist is applied by the ledger


def test_analysis_audit_still_logs_to_audit_service(monkeypatch):
    from src.api import lifespan
    from src.api import state as api_state

    ledger = MagicMock(); ledger.record_analysis = AsyncMock(side_effect=RuntimeError("x"))
    audit = MagicMock(); audit.log = AsyncMock()
    monkeypatch.setattr(api_state, "usage_ledger", ledger)
    monkeypatch.setattr(api_state, "audit_service", audit)
    asyncio.run(lifespan._analysis_audit({"user_id": "7", "outcome": "ok", "skill": "forecast", "query_id": str(TURN_ID)}))
    audit.log.assert_awaited_once()
    assert audit.log.await_args.kwargs["event_type"] == "analysis.run"


# ── Flask login path ─────────────────────────────────────────────────────

class _Tx:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): self.conn.savepoints += 1; return self
    def __exit__(self, exc_type, exc, tb): return exc_type is not None   # rollback-to-savepoint swallows


class _PgConn:
    def __init__(self, fail_insert=False):
        self.calls = []; self.commits = 0; self.savepoints = 0; self.fail_insert = fail_insert
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        if self.fail_insert and sql.lstrip().upper().startswith("INSERT"):
            raise RuntimeError('relation "insights_usage_events" does not exist')
    def transaction(self): return _Tx(self)
    def commit(self): self.commits += 1


def test_touch_last_active_records_a_login_event_in_the_same_transaction(monkeypatch):
    from src import auth_db

    conn = _PgConn()
    monkeypatch.setattr(auth_db, "_connect", lambda: conn)
    auth_db.touch_last_active(7)
    assert conn.calls[0][0].startswith("UPDATE auth_users SET last_active_at")
    assert conn.calls[1][0].startswith("INSERT INTO insights_usage_events (event_type, user_id) VALUES ('login', %s)")
    assert conn.calls[1][1] == ("7",)                              # principal id as text
    assert conn.savepoints == 1 and conn.commits == 1


def test_touch_last_active_survives_a_missing_ledger_table(monkeypatch):
    from src import auth_db

    conn = _PgConn(fail_insert=True)
    monkeypatch.setattr(auth_db, "_connect", lambda: conn)
    auth_db.touch_last_active(7)                                   # no exception
    assert conn.commits == 1                                       # last_active_at still committed
