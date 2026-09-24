"""ConversationHistoryService behaviour with and without migration 022.

The service must keep questions working on a database where the conversation
tables do not exist yet (pre-022 SQL), and must use the conversation-scoped SQL
once they do. A tiny fake pool records every statement so the tests can assert
which path ran without a real database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from src.agent.conversation_history import ConversationHistoryService


class _FakeConn:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[tuple] = []

    def _next(self):
        return self.responses.pop(0) if self.responses else None

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", " ".join(sql.split()), args))
        return self._next()

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", " ".join(sql.split()), args))
        return self._next()

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", " ".join(sql.split()), args))
        return self._next() or []

    async def execute(self, sql, *args):
        self.calls.append(("execute", " ".join(sql.split()), args))
        return self._next() or "UPDATE 1"

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                conn.calls.append(("tx", "BEGIN", ()))
                return None

            async def __aexit__(self_inner, *exc):
                conn.calls.append(("tx", "COMMIT" if exc[0] is None else "ROLLBACK", ()))
                return False

        return _Tx()


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn
        self.acquired = 0

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_inner):
                pool.acquired += 1
                return pool.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


def _sql_of(conn: _FakeConn) -> str:
    return "\n".join(call[1] for call in conn.calls)


SESSION = uuid.uuid4()
QID = uuid.uuid4()


def test_log_query_uses_legacy_path_when_schema_missing():
    conn = _FakeConn(responses=[3, None, QID])  # next seq, parent id, inserted id
    svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=False)
    out = asyncio.run(svc.log_query(
        user_id="u", source_key="s", session_id=SESSION, natural_language_query="q",
        source_label="Label",
    ))
    assert out == QID
    sql = _sql_of(conn)
    assert "insights_conversations" not in sql, "pre-022 DB: never touch the conversation table"
    assert "insights_get_next_sequence_number" in sql
    assert "INSERT INTO insights_conversation_sessions" in sql


def test_conversation_summary_saved_count_is_schema_gated():
    svc = ConversationHistoryService(_FakePool(_FakeConn([])), conversation_schema_ready=True)
    assert "insights_favorite_answers" not in svc._conversation_summary_sql()
    assert "0::bigint" in svc._conversation_summary_sql()
    svc.favorite_schema_ready = True
    assert "insights_favorite_answers" in svc._conversation_summary_sql()
    assert "saved_answer_count" in svc._conversation_summary_sql()


def test_log_query_is_one_transaction_with_lock_when_schema_ready():
    conn = _FakeConn(responses=["INSERT 0 1", 1, 4, None, QID])
    svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=True)
    out = asyncio.run(svc.log_query(
        user_id="u", source_key="s", session_id=SESSION, natural_language_query="q", source_label="L",
    ))
    assert out == QID
    kinds = [c[0] for c in conn.calls]
    assert kinds[0] == "tx" and conn.calls[0][1] == "BEGIN"
    assert kinds[-1] == "tx" and conn.calls[-1][1] == "COMMIT"
    sql = _sql_of(conn)
    assert "INSERT INTO insights_conversations" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "FOR UPDATE" in sql
    assert "COALESCE(MAX(sequence_number), 0) + 1" in sql
    # Upsert carries the label and a title derived from the first question.
    upsert_args = conn.calls[1][2]
    assert upsert_args[3] == "L" and upsert_args[4] == "q"


def test_log_query_refuses_foreign_session_when_schema_ready():
    # Upsert is a no-op for a foreign conversation, then FOR UPDATE finds nothing.
    conn = _FakeConn(responses=["INSERT 0 0", None])
    svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=True)
    out = asyncio.run(svc.log_query(
        user_id="intruder", source_key="s", session_id=SESSION, natural_language_query="q",
    ))
    assert isinstance(out, uuid.UUID) and out != QID
    assert "INSERT INTO insights_conversation_sessions" not in _sql_of(conn)
    assert conn.calls[-1][1] == "ROLLBACK"


def test_conversation_belongs_to_user_legacy_semantics():
    def run(responses, **kw):
        conn = _FakeConn(responses=responses)
        svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=False)
        result = asyncio.run(svc.conversation_belongs_to_user(session_id=SESSION, **kw))
        return result, conn

    # No rows yet: allowed (pre-022 behaviour), only the turn table is queried.
    ok, conn = run([None], user_id="u", source_key="s")
    assert ok is True and "insights_conversations " not in _sql_of(conn) + " "
    # Owned, same connection.
    ok, _ = run([{"user_id": "u", "source_key": "s"}], user_id="u", source_key="s")
    assert ok is True
    # Owned by someone else.
    ok, _ = run([{"user_id": "other", "source_key": "s"}], user_id="u", source_key="s")
    assert ok is False
    # Owned, but on another connection: refused.
    ok, _ = run([{"user_id": "u", "source_key": "other_db"}], user_id="u", source_key="s")
    assert ok is False


def test_conversation_belongs_to_user_uses_conversation_table_when_ready():
    conn = _FakeConn(responses=[1])
    svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=True)
    assert asyncio.run(svc.conversation_belongs_to_user(session_id=SESSION, user_id="u", source_key="s")) is True
    assert "FROM insights_conversations WHERE" in _sql_of(conn)
    # Unknown id -> refused (no empty-session allowance once conversations exist).
    conn2 = _FakeConn(responses=[None])
    svc2 = ConversationHistoryService(_FakePool(conn2), conversation_schema_ready=True)
    assert asyncio.run(svc2.conversation_belongs_to_user(session_id=SESSION, user_id="u", source_key="s")) is False
    # Garbage id is refused without touching the pool.
    assert asyncio.run(svc2.conversation_belongs_to_user(session_id="not-a-uuid", user_id="u")) is False


def test_get_conversation_context_scopes_by_source_in_both_modes():
    ready = _FakeConn(responses=[[]])
    asyncio.run(ConversationHistoryService(_FakePool(ready), conversation_schema_ready=True)
                .get_conversation_context(session_id=SESSION, user_id="u", source_key="s"))
    assert "JOIN insights_conversations c" in _sql_of(ready)

    legacy = _FakeConn(responses=[[]])
    asyncio.run(ConversationHistoryService(_FakePool(legacy), conversation_schema_ready=False)
                .get_conversation_context(session_id=SESSION, user_id="u", source_key="s"))
    sql = _sql_of(legacy)
    assert "insights_conversations" not in sql
    assert "source_key = $4" in sql


def test_get_conversation_context_excludes_in_flight_turns_in_both_modes():
    """The agents fetch context concurrently with inserting the current turn's
    'pending' row; that row must never come back as a prior turn. NULL statuses
    (legacy rows) must still be returned, hence IS DISTINCT FROM."""
    for ready in (True, False):
        conn = _FakeConn(responses=[[]])
        asyncio.run(ConversationHistoryService(_FakePool(conn), conversation_schema_ready=ready)
                    .get_conversation_context(session_id=SESSION, user_id="u", source_key="s"))
        assert "execution_status IS DISTINCT FROM 'pending'" in _sql_of(conn), f"schema_ready={ready}"


def test_get_conversation_context_joins_turn_answer_when_schema_ready():
    from datetime import datetime, timezone

    ready = _FakeConn(responses=[[{
        "id": QID, "natural_language_query": "q", "generated_sql": "SELECT 1", "execution_status": "success",
        "row_count": 1, "result_preview": None, "result_artifact": None, "created_at": datetime.now(timezone.utc),
        "turn_answer": '[{"t": "Sales ", "hl": null}, {"t": "grew", "hl": "pos"}]', "result_kind": "table",
        "snapshot_status": "stored",
    }]])
    rows = asyncio.run(ConversationHistoryService(_FakePool(ready), conversation_schema_ready=True)
                       .get_conversation_context(session_id=SESSION, user_id="u", source_key="s"))
    assert "LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id" in _sql_of(ready)
    assert rows[0]["answer"] == "Sales grew"          # fragment array flattened to text
    assert rows[0]["snapshot_status"] == "stored" and rows[0]["result_kind"] == "table"
    assert "turn_answer" not in rows[0]


def test_search_turns_reads_the_history_log_rows_with_keyword_and_window_predicates():
    """history_lookup must answer from what the History log drawer shows: the
    user's queries on this connection in insights_conversation_sessions (same
    scoping as get_history_log), minus the turn being answered right now."""
    from datetime import datetime, timezone

    since, until = datetime(2026, 9, 13, tzinfo=timezone.utc), datetime(2026, 9, 17, tzinfo=timezone.utc)
    ready = _FakeConn(responses=[[]])
    asyncio.run(ConversationHistoryService(_FakePool(ready), conversation_schema_ready=True)
                .search_turns(user_id="u", source_key="s", keywords=["revenue", "sales"], since=since, until=until,
                              limit=7, exclude_query_id=QID))
    sql = _sql_of(ready)
    assert "FROM insights_conversation_sessions cs" in sql
    assert "insights_conversations c" not in sql                     # same scope as the History log, not conversation-joined
    assert "WHERE cs.user_id = $1 AND ($2::text IS NULL OR cs.source_key = $2)" in sql
    assert "LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id" in sql   # only to attach the stored answer
    assert "cs.natural_language_query ILIKE $5 OR cs.natural_language_query ILIKE $6" in sql
    assert "cs.created_at >= $3 AND cs.created_at < $4" in sql
    assert "cs.id::text <> $7" in sql and "LIMIT $8" in sql
    assert "pending" not in sql                                       # the log shows every status
    args = ready.calls[0][2]
    assert args[4:6] == ("%revenue%", "%sales%") and args[6] == str(QID) and args[7] == 7

    legacy = _FakeConn(responses=[[]])
    asyncio.run(ConversationHistoryService(_FakePool(legacy), conversation_schema_ready=False)
                .search_turns(user_id="u", source_key="s", keywords=[], since=since, until=until))
    sql = _sql_of(legacy)
    assert "insights_turn_artifacts" not in sql and "ILIKE" not in sql and "LIMIT $5" in sql


def test_new_schema_methods_short_circuit_without_schema():
    """Every method that only makes sense once migration 022 exists must return
    its empty value without touching the pool (no logged DB errors)."""
    pool = _FakePool(_FakeConn(responses=[]))
    svc = ConversationHistoryService(pool, conversation_schema_ready=False)
    run = asyncio.run
    assert run(svc.get_last_conversation(user_id="u", source_key="s")) is None
    assert run(svc.get_conversation(conversation_id=SESSION, user_id="u")) is None
    assert run(svc.list_conversations(user_id="u")) == []
    assert run(svc.get_conversation_turns(conversation_id=SESSION, user_id="u")) == []
    assert run(svc.get_conversation_turn(conversation_id=SESSION, turn_id=QID, user_id="u")) is None
    assert run(svc.get_turn_artifact(conversation_id=SESSION, turn_id=QID, user_id="u")) is None
    assert run(svc.get_turn_for_rerun(conversation_id=SESSION, turn_id=QID, user_id="u")) is None
    assert run(svc.upsert_turn_artifact(turn_id=QID, result_kind="text")) is False
    assert run(svc.upsert_turn_chart(turn_id=QID, user_id="u", chart_spec={}, chart_config={}, chart_bytes=1)) is False
    assert run(svc.clear_turn_chart(turn_id=QID, user_id="u")) is False
    assert run(svc.store_rerun_snapshot(turn_id=QID, user_id="u", result_snapshot=None, snapshot_status="too_large", snapshot_bytes=None)) is False
    assert run(svc.rename_conversation(conversation_id=SESSION, user_id="u", title="t")) is False
    assert run(svc.delete_conversation(conversation_id=SESSION, user_id="u")) == {
        "status": "missing", "saved_answer_count": 0,
    }
    assert run(svc.prune_user_conversations(user_id="u", source_key="s", keep_last=1, keep_last_turns=1, min_interval_seconds=0)) == {"skipped": "schema_missing"}
    assert pool.acquired == 0, "no query is attempted against missing tables"


def test_chart_writes_use_request_start_as_ordering_watermark():
    started = datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)
    watermark_conn = _FakeConn(responses=[started])
    watermark_service = ConversationHistoryService(
        _FakePool(watermark_conn), conversation_schema_ready=True
    )
    assert asyncio.run(watermark_service.get_chart_request_watermark()) == started
    assert "SELECT clock_timestamp()" in _sql_of(watermark_conn)

    upsert_conn = _FakeConn(responses=["UPDATE 1"])
    service = ConversationHistoryService(
        _FakePool(upsert_conn), conversation_schema_ready=True
    )
    assert asyncio.run(service.upsert_turn_chart(
        turn_id=QID,
        user_id="u",
        chart_spec={"chart_type": "bar"},
        chart_config={"series": [{"type": "bar"}]},
        chart_bytes=12,
        request_started_at=started,
    ))
    upsert_sql = _sql_of(upsert_conn)
    assert "a.chart_updated_at <= $6::timestamptz" in upsert_sql
    assert upsert_conn.calls[0][2][5] == started

    clear_conn = _FakeConn(responses=["UPDATE 1"])
    clear_service = ConversationHistoryService(
        _FakePool(clear_conn), conversation_schema_ready=True
    )
    assert asyncio.run(clear_service.clear_turn_chart(
        turn_id=QID, user_id="u", request_started_at=started
    ))
    clear_sql = _sql_of(clear_conn)
    assert "a.chart_updated_at <= $3::timestamptz" in clear_sql
    assert "ELSE $3::timestamptz" in clear_sql
    assert clear_conn.calls[0][2][2] == started

    rerun_conn = _FakeConn(responses=["INSERT 0 1"])
    rerun_service = ConversationHistoryService(
        _FakePool(rerun_conn), conversation_schema_ready=True
    )
    assert asyncio.run(rerun_service.store_rerun_snapshot(
        turn_id=QID,
        user_id="u",
        result_snapshot={"columns": ["a"], "rows": [[1]], "row_count": 1},
        snapshot_status="stored",
        snapshot_bytes=12,
    ))
    rerun_sql = _sql_of(rerun_conn)
    assert "snapshot_at, chart_updated_at" in rerun_sql
    assert "chart_updated_at = NOW()" in rerun_sql


def test_saved_analysis_persists_versioned_chart_state():
    saved_id = uuid.uuid4()
    conn = _FakeConn(responses=[saved_id])
    service = ConversationHistoryService(
        _FakePool(conn), conversation_schema_ready=True
    )
    chart_state = {
        "chart_config": {"series": [{"type": "line", "data": [1]}]},
        "chart_toggles": {"dataLabels": True},
        "derived_specs": [{"operator": "moving_avg", "source_column": "value"}],
    }
    result = asyncio.run(service.save_analysis(
        user_id="u",
        source_key="sales",
        connection_id="sales",
        name="Saved",
        question="show value",
        generated_sql="select 1",
        query_id=None,
        columns=["value"],
        rows=[[1]],
        chart_spec={"chart_type": "line"},
        chart_config=chart_state["chart_config"],
        chart_state=chart_state,
    ))
    assert result == saved_id
    sql = _sql_of(conn)
    assert "chart_spec, chart_config, chart_state, insights_payload" in sql
    args = conn.calls[0][2]
    assert '"chart_toggles"' in args[12]


@pytest.mark.parametrize(
    "present,setting,expect_ready,expect_enabled",
    [(False, True, False, False), (True, False, True, False), (True, True, True, True)],
)
def test_lifespan_probe_sets_schema_and_kill_switch_independently(
    monkeypatch, present, setting, expect_ready, expect_enabled
):
    from src.api import lifespan

    monkeypatch.setattr(lifespan.settings, "CONVERSATION_PERSISTENCE_ENABLED", setting)
    conn = MagicMock()

    async def fetchval(_sql):
        return present

    conn.fetchval = fetchval
    history = SimpleNamespace(persistence_enabled=None, conversation_schema_ready=None)
    asyncio.run(lifespan._probe_conversation_persistence(conn, history))
    assert history.conversation_schema_ready is expect_ready
    assert history.persistence_enabled is expect_enabled


def test_answer_favorite_is_owned_idempotent_and_listed():
    conn = _FakeConn(responses=[
        "s", None, 1, "INSERT 0 1",
        [{
            "conversation_id": SESSION,
            "turn_id": QID,
            "sequence_number": 3,
            "conversation_title": "Revenue",
            "question": "Revenue by month",
            "answer": '"Revenue rose"',
            "result_kind": "table",
            "snapshot_status": "stored",
            "source_key": "s",
            "source_label": "Sales",
            "created_at": "2026-09-01T10:00:00+00:00",
            "favorited_at": "2026-09-01T10:01:00+00:00",
        }],
    ])
    svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=True)
    svc.favorite_schema_ready = True

    state = asyncio.run(svc.set_answer_favorite(
        conversation_id=SESSION, turn_id=QID, user_id="u", favorite=True,
    ))
    items = asyncio.run(svc.list_favorite_answers(user_id="u", source_key="s"))

    assert state is True
    assert items[0]["answer"] == "Revenue rose"
    sql = _sql_of(conn)
    assert "cs.execution_status = 'success'" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "ON CONFLICT (user_id, turn_id) DO NOTHING" in sql
    assert "c.user_id = $1" in sql


def test_answer_favorite_refuses_foreign_turn_and_schema_missing():
    conn = _FakeConn(responses=[None])
    svc = ConversationHistoryService(_FakePool(conn), conversation_schema_ready=True)
    svc.favorite_schema_ready = True
    assert asyncio.run(svc.set_answer_favorite(
        conversation_id=SESSION, turn_id=QID, user_id="intruder", favorite=True,
    )) is None
    assert "INSERT INTO insights_favorite_answers" not in _sql_of(conn)
    missing_remove = ConversationHistoryService(_FakePool(_FakeConn([None])), conversation_schema_ready=True)
    missing_remove.favorite_schema_ready = True
    assert asyncio.run(missing_remove.set_answer_favorite(
        conversation_id=SESSION, turn_id=QID, user_id="u", favorite=False,
    )) is False

    unavailable = ConversationHistoryService(_FakePool(_FakeConn([])), conversation_schema_ready=True)
    assert asyncio.run(unavailable.list_favorite_answers(user_id="u")) == []
    assert asyncio.run(unavailable.set_answer_favorite(
        conversation_id=SESSION, turn_id=QID, user_id="u", favorite=True,
    )) is None


def test_favorite_schema_probe_sets_independent_flag():
    from src.api import lifespan

    conn = MagicMock()

    async def fetchval(_sql):
        return True

    conn.fetchval = fetchval
    history = SimpleNamespace(favorite_schema_ready=None)
    assert asyncio.run(lifespan._probe_favorite_schema(conn, history)) is True
    assert history.favorite_schema_ready is True


def test_delete_conversation_blocks_saved_answers_and_forced_delete_is_locked():
    blocked_conn = _FakeConn(responses=["s", None, 1, 2])
    blocked = ConversationHistoryService(_FakePool(blocked_conn), conversation_schema_ready=True)
    blocked.favorite_schema_ready = True
    outcome = asyncio.run(blocked.delete_conversation(
        conversation_id=SESSION, user_id="u",
    ))
    assert outcome == {"status": "blocked", "saved_answer_count": 2}
    assert "pg_advisory_xact_lock" in _sql_of(blocked_conn)
    assert "DELETE FROM insights_conversations" not in _sql_of(blocked_conn)

    forced_conn = _FakeConn(responses=["s", None, 1, 2, "DELETE 1"])
    forced = ConversationHistoryService(_FakePool(forced_conn), conversation_schema_ready=True)
    forced.favorite_schema_ready = True
    outcome = asyncio.run(forced.delete_conversation(
        conversation_id=SESSION, user_id="u", delete_saved=True,
    ))
    assert outcome == {"status": "deleted", "saved_answer_count": 2}
    assert "DELETE FROM insights_conversations" in _sql_of(forced_conn)
