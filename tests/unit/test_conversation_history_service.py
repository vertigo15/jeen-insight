"""ConversationHistoryService behaviour with and without migration 022.

The service must keep questions working on a database where the conversation
tables do not exist yet (pre-022 SQL), and must use the conversation-scoped SQL
once they do. A tiny fake pool records every statement so the tests can assert
which path ran without a real database.
"""

from __future__ import annotations

import asyncio
import uuid
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
    assert run(svc.get_turn_artifact(conversation_id=SESSION, turn_id=QID, user_id="u")) is None
    assert run(svc.get_turn_for_rerun(conversation_id=SESSION, turn_id=QID, user_id="u")) is None
    assert run(svc.upsert_turn_artifact(turn_id=QID, result_kind="text")) is False
    assert run(svc.upsert_turn_chart(turn_id=QID, user_id="u", chart_spec={}, chart_config={}, chart_bytes=1)) is False
    assert run(svc.clear_turn_chart(turn_id=QID, user_id="u")) is False
    assert run(svc.store_rerun_snapshot(turn_id=QID, user_id="u", result_snapshot=None, snapshot_status="too_large", snapshot_bytes=None)) is False
    assert run(svc.rename_conversation(conversation_id=SESSION, user_id="u", title="t")) is False
    assert run(svc.delete_conversation(conversation_id=SESSION, user_id="u")) is False
    assert run(svc.prune_user_conversations(user_id="u", source_key="s", keep_last=1, keep_last_turns=1, min_interval_seconds=0)) == {"skipped": "schema_missing"}
    assert pool.acquired == 0, "no query is attempted against missing tables"


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
