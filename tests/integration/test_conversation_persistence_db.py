"""Real-PostgreSQL tests for conversation persistence guarantees.

The mocked-pool unit tests prove service logic and route behaviour; the
database semantics this feature relies on can only be proven here:

* migration 022 applies on a DB seeded with a legacy cross-source ``session_id``
  (the losing thread is re-keyed, the parent FK is added);
* deleting a conversation cascades to turns, insights and artifacts and nulls
  saved-analysis references;
* ``log_query`` under concurrent writers never violates ``(session_id,
  sequence_number)`` and refuses a foreign session id;
* the retention prune deletes beyond ``keep_last`` (never the protected
  conversation or a favorite), bounds blob-holding turns exactly to ``keep_last_turns``
  across a long active conversation, leaves ``not_applicable`` turns alone,
  is idempotent, and its DB claim lets only one run through per interval;
* the guarded artifact upsert is a no-op after a prune, while the rerun write
  promotes the turn back to ``stored``.

Run against an ephemeral Postgres:

    docker run -d --name jeen-e2e-pg -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e \
        -e POSTGRES_DB=e2e -p 55440:5432 postgres:16-alpine
    JEEN_E2E_DB=1 METADATA_DB_HOST=localhost METADATA_DB_PORT=55440 METADATA_DB_NAME=e2e \
      METADATA_DB_USER=e2e METADATA_DB_PASSWORD=e2e METADATA_DB_SSL=false \
      python3 -m pytest tests/integration/test_conversation_persistence_db.py -q

The module SKIPS unless ``JEEN_E2E_DB`` is truthy so the DB-less unit run is
unaffected. It applies the migrations itself (idempotent runner).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ENABLED = (os.getenv("JEEN_E2E_DB") or "").strip().lower() in ("1", "true", "yes", "on")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ENABLED, reason="Set JEEN_E2E_DB=1 (+ test METADATA_DB_*) to run"),
]

USER_A = "e2e-conv-user-a"
USER_B = "e2e-conv-user-b"
SRC_A = "e2e_src_a"
SRC_B = "e2e_src_b"

MIGRATIONS = sorted(
    (Path(__file__).resolve().parents[2] / "db" / "migrations" / "insights").glob("*.sql")
)


async def _apply_migrations_upto(conn, last: str) -> None:
    """Apply migration files <= *last* (by filename) with the runner's bookkeeping."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS insights_schema_migrations (
            revision TEXT PRIMARY KEY, checksum TEXT,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    done = {r["revision"] for r in await conn.fetch("SELECT revision FROM insights_schema_migrations")}
    for path in MIGRATIONS:
        if path.name > last or path.name in done:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO insights_schema_migrations (revision, checksum) VALUES ($1, NULL) "
                "ON CONFLICT (revision) DO NOTHING",
                path.name,
            )


async def _bootstrap_lifespan_columns(conn) -> None:
    """Columns the API adds at startup via ``_ensure_schema`` (no migration)."""
    for ddl in (
        "ALTER TABLE insights_conversation_sessions ADD COLUMN IF NOT EXISTS graph_time_ms INT",
        "ALTER TABLE insights_conversation_sessions ADD COLUMN IF NOT EXISTS result_artifact JSONB",
        "ALTER TABLE insights_conversation_sessions ADD COLUMN IF NOT EXISTS node_trace JSONB",
    ):
        await conn.execute(ddl)


async def _cleanup(conn) -> None:
    await conn.execute(
        "DELETE FROM insights_conversation_sessions WHERE user_id = ANY($1::text[])",
        [USER_A, USER_B],
    )
    if await conn.fetchval("SELECT to_regclass('insights_conversations') IS NOT NULL"):
        await conn.execute(
            "DELETE FROM insights_conversations WHERE user_id = ANY($1::text[])", [USER_A, USER_B]
        )
        await conn.execute(
            "DELETE FROM insights_conversation_prune_state WHERE user_id = ANY($1::text[])",
            [USER_A, USER_B],
        )


@pytest.fixture(scope="module")
def pool():
    from src.metadata import close_metadata_pool, get_metadata_pool

    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_metadata_pool())
    yield p, loop
    loop.run_until_complete(close_metadata_pool())
    loop.close()


def _run(pool_loop, coro):
    _pool, loop = pool_loop
    return loop.run_until_complete(coro)


# ── migration 022 on a legacy DB ─────────────────────────────────────────────

def test_migration_022_rekeys_cross_source_threads_and_adds_fk(pool):
    p, _ = pool

    async def scenario():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, "021_user_onboarding.sql")
            # Fresh DB? Then 022 may already be recorded by an earlier module run;
            # roll it back to simulate a legacy DB for this test.
            if await conn.fetchval("SELECT to_regclass('insights_conversations') IS NOT NULL"):
                await conn.execute("ALTER TABLE insights_conversation_sessions DROP CONSTRAINT IF EXISTS fk_insights_turn_conversation")
                await conn.execute("DROP TABLE IF EXISTS insights_turn_artifacts")
                await conn.execute("DROP TABLE IF EXISTS insights_conversation_prune_state")
                await conn.execute("DROP TABLE IF EXISTS insights_conversations")
                await conn.execute(
                    "DELETE FROM insights_schema_migrations WHERE revision LIKE '022_%'"
                )
            await _cleanup(conn)

            # One legacy session_id shared by two connections: 3 turns on A, 1 on B.
            legacy = uuid.uuid4()
            for seq in (1, 2, 3):
                await conn.execute(
                    """
                    INSERT INTO insights_conversation_sessions
                        (user_id, source_key, session_id, sequence_number, natural_language_query, execution_status)
                    VALUES ($1, $2, $3, $4, $5, 'success')
                    """,
                    USER_A, SRC_A, legacy, seq, f"A question {seq}",
                )
            await conn.execute(
                """
                INSERT INTO insights_conversation_sessions
                    (user_id, source_key, session_id, sequence_number, natural_language_query, execution_status)
                VALUES ($1, $2, $3, 4, 'B question', 'success')
                """,
                USER_A, SRC_B, legacy,
            )

            await _apply_migrations_upto(conn, "022_conversations_and_turn_artifacts.sql")

            rows = await conn.fetch(
                "SELECT source_key, session_id, natural_language_query FROM insights_conversation_sessions "
                "WHERE user_id = $1 ORDER BY sequence_number",
                USER_A,
            )
            a_ids = {r["session_id"] for r in rows if r["source_key"] == SRC_A}
            b_ids = {r["session_id"] for r in rows if r["source_key"] == SRC_B}
            assert a_ids == {legacy}, "largest thread keeps the legacy id"
            assert len(b_ids) == 1 and legacy not in b_ids, "losing thread re-keyed"

            convs = await conn.fetch(
                "SELECT id, source_key, title FROM insights_conversations WHERE user_id = $1", USER_A
            )
            by_src = {c["source_key"]: c for c in convs}
            assert by_src[SRC_A]["id"] == legacy and by_src[SRC_A]["title"] == "A question 1"
            assert by_src[SRC_B]["id"] in b_ids and by_src[SRC_B]["title"] == "B question"

            fk = await conn.fetchval(
                "SELECT 1 FROM pg_constraint WHERE conname = 'fk_insights_turn_conversation'"
            )
            assert fk == 1
            await _cleanup(conn)

    _run(pool, scenario())


def test_migration_031_round_trips_account_date_format(pool):
    p, _ = pool

    async def scenario():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, "031_user_date_format.sql")
            email = f"date-format-{uuid.uuid4()}@example.test"
            user_id = await conn.fetchval(
                """
                INSERT INTO auth_users (name, email, password_hash, role, status, avatar_hue)
                VALUES ('Date Format Test', $1, 'unused', 'viewer', 'active', 0)
                RETURNING id
                """,
                email,
            )
            try:
                await conn.execute(
                    "UPDATE auth_users SET date_format = 'dmy' WHERE id = $1",
                    user_id,
                )
                assert await conn.fetchval(
                    "SELECT date_format FROM auth_users WHERE id = $1",
                    user_id,
                ) == "dmy"
            finally:
                await conn.execute("DELETE FROM auth_users WHERE id = $1", user_id)

    _run(pool, scenario())


# ── service-level guarantees ─────────────────────────────────────────────────

@pytest.fixture()
def history(pool):
    from src.agent.conversation_history import ConversationHistoryService

    p, loop = pool

    async def prep():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, "022_conversations_and_turn_artifacts.sql")
            await _bootstrap_lifespan_columns(conn)
            await _cleanup(conn)

    loop.run_until_complete(prep())
    yield ConversationHistoryService(p, persistence_enabled=True)
    loop.run_until_complete(prep())


async def _turn(history, *, user=USER_A, src=SRC_A, session, question="q", sql="select 1", rows=1):
    qid = await history.log_query(
        user_id=user, source_key=src, session_id=session,
        natural_language_query=question, source_label="Label",
    )
    await history.update_llm_response(query_id=qid, generated_sql=sql, llm_model="m", llm_latency_ms=1, tokens_used=1)
    await history.update_execution(query_id=qid, execution_status="success", row_count=rows)
    await history.upsert_turn_artifact(
        turn_id=qid, result_kind="table",
        result_snapshot={"columns": ["a"], "rows": [[i] for i in range(rows)], "row_count": rows},
        snapshot_status="stored", snapshot_bytes=10,
    )
    await history.upsert_turn_chart(
        turn_id=qid, user_id=user, chart_spec={"chart_type": "bar"},
        chart_config={"series": [{"type": "bar"}]}, chart_bytes=20,
    )
    return qid


def test_chart_request_start_watermark_rejects_stale_write_and_clear(history, pool):
    async def scenario():
        session = uuid.uuid4()
        qid = await _turn(history, session=session)
        newer = datetime.now(timezone.utc) + timedelta(seconds=2)
        older = newer - timedelta(seconds=1)

        assert await history.upsert_turn_chart(
            turn_id=qid,
            user_id=USER_A,
            chart_spec={"chart_type": "line"},
            chart_config={"series": [{"type": "line"}]},
            chart_bytes=30,
            request_started_at=newer,
        )
        assert not await history.upsert_turn_chart(
            turn_id=qid,
            user_id=USER_A,
            chart_spec={"chart_type": "pie"},
            chart_config={"series": [{"type": "pie"}]},
            chart_bytes=31,
            request_started_at=older,
        )
        assert not await history.clear_turn_chart(
            turn_id=qid, user_id=USER_A, request_started_at=older
        )
        artifact = await history.get_turn_artifact(
            conversation_id=session, turn_id=qid, user_id=USER_A
        )
        assert artifact["chart_config"]["series"][0]["type"] == "line"

        newest = newer + timedelta(seconds=1)
        assert await history.clear_turn_chart(
            turn_id=qid, user_id=USER_A, request_started_at=newest
        )
        assert not await history.upsert_turn_chart(
            turn_id=qid,
            user_id=USER_A,
            chart_spec={"chart_type": "bar"},
            chart_config={"series": [{"type": "bar"}]},
            chart_bytes=20,
            request_started_at=newer,
        )
        async with history.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT chart_config, chart_updated_at FROM insights_turn_artifacts WHERE turn_id = $1",
                qid,
            )
        assert row["chart_config"] is None
        assert row["chart_updated_at"] == newest

        # A rerun replaces the result rows and establishes its own watermark.
        # A chart request that started before the rerun must not attach a chart
        # built from the obsolete rows after the rerun finishes.
        before_rerun = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await history.store_rerun_snapshot(
            turn_id=qid,
            user_id=USER_A,
            result_snapshot={"columns": ["a"], "rows": [[99]], "row_count": 1},
            snapshot_status="stored",
            snapshot_bytes=16,
        )
        assert not await history.upsert_turn_chart(
            turn_id=qid,
            user_id=USER_A,
            chart_spec={"chart_type": "bar"},
            chart_config={"series": [{"type": "bar", "data": [1]}]},
            chart_bytes=20,
            request_started_at=before_rerun,
        )

    _run(pool, scenario())


def test_log_query_is_atomic_and_refuses_foreign_session(history, pool):
    async def scenario():
        session = uuid.uuid4()
        # 12 concurrent writers into one brand-new conversation.
        ids = await asyncio.gather(*[
            history.log_query(user_id=USER_A, source_key=SRC_A, session_id=session,
                              natural_language_query=f"q{i}", source_label="L")
            for i in range(12)
        ])
        assert len(set(ids)) == 12
        async with history.pool.acquire() as conn:
            seqs = [r["sequence_number"] for r in await conn.fetch(
                "SELECT sequence_number FROM insights_conversation_sessions WHERE session_id = $1 ORDER BY 1", session)]
            assert seqs == list(range(1, 13)), "no gaps, no duplicates under contention"
            conv = await conn.fetchrow("SELECT * FROM insights_conversations WHERE id = $1", session)
            assert conv["user_id"] == USER_A and conv["source_key"] == SRC_A
            assert conv["source_label"] == "L" and conv["title"].startswith("q")

        # Same session id from another user / another connection: refused, and no
        # row is written (log_query swallows the error and hands back a random id).
        before = await history.get_conversation_turns(conversation_id=session, user_id=USER_A, limit=100)
        await history.log_query(user_id=USER_B, source_key=SRC_A, session_id=session, natural_language_query="hijack")
        await history.log_query(user_id=USER_A, source_key=SRC_B, session_id=session, natural_language_query="wrong source")
        after = await history.get_conversation_turns(conversation_id=session, user_id=USER_A, limit=100)
        assert len(after) == len(before) == 12
        assert await history.conversation_belongs_to_user(session_id=session, user_id=USER_A, source_key=SRC_A)
        assert not await history.conversation_belongs_to_user(session_id=session, user_id=USER_A, source_key=SRC_B)
        assert not await history.conversation_belongs_to_user(session_id=session, user_id=USER_B)
        assert not await history.conversation_belongs_to_user(session_id=uuid.uuid4(), user_id=USER_A)

    _run(pool, scenario())


def test_delete_conversation_cascades(history, pool):
    async def scenario():
        session = uuid.uuid4()
        qid = await _turn(history, session=session)
        await history.add_insight(query_id=qid, insight_type="summary", content="s")
        async with history.pool.acquire() as conn:
            saved = await conn.fetchval(
                """
                INSERT INTO insights_saved_analyses
                    (user_id, source_key, query_id, name, question, columns, row_count, result_snapshot)
                VALUES ($1, $2, $3, 'n', 'q', '[]', 0, '{}') RETURNING id
                """,
                USER_A, SRC_A, qid,
            )
        foreign = await history.delete_conversation(conversation_id=session, user_id=USER_B)
        assert foreign["status"] == "missing", "foreign delete refused"
        deleted = await history.delete_conversation(conversation_id=session, user_id=USER_A)
        assert deleted["status"] == "deleted"
        async with history.pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM insights_conversation_sessions WHERE session_id = $1", session) == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM insights_turn_artifacts WHERE turn_id = $1", qid) == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM insights_query_insights WHERE query_id = $1", qid) == 0
            assert await conn.fetchval("SELECT query_id FROM insights_saved_analyses WHERE id = $1", saved) is None
            await conn.execute("DELETE FROM insights_saved_analyses WHERE id = $1", saved)

    _run(pool, scenario())


def test_saved_answers_require_explicit_conversation_delete(history, pool):
    async def scenario():
        migration = Path(__file__).resolve().parents[2] / "db" / "migrations" / "insights" / "030_favorite_answers.sql"
        async with history.pool.acquire() as conn:
            await conn.execute(migration.read_text(encoding="utf-8"))
        history.favorite_schema_ready = True

        session = uuid.uuid4()
        qid = await _turn(history, session=session)
        assert await history.set_answer_favorite(
            conversation_id=session, turn_id=qid, user_id=USER_A, favorite=True,
        )
        blocked = await history.delete_conversation(
            conversation_id=session, user_id=USER_A,
        )
        assert blocked == {"status": "blocked", "saved_answer_count": 1}
        assert await history.get_conversation(conversation_id=session, user_id=USER_A)

        deleted = await history.delete_conversation(
            conversation_id=session, user_id=USER_A, delete_saved=True,
        )
        assert deleted == {"status": "deleted", "saved_answer_count": 1}
        assert await history.get_conversation(conversation_id=session, user_id=USER_A) is None
        assert await history.list_favorite_answers(user_id=USER_A) == []

    _run(pool, scenario())


def test_favorite_and_manual_delete_are_serialized(history, pool):
    async def scenario():
        migration = Path(__file__).resolve().parents[2] / "db" / "migrations" / "insights" / "030_favorite_answers.sql"
        async with history.pool.acquire() as conn:
            await conn.execute(migration.read_text(encoding="utf-8"))
        history.favorite_schema_ready = True

        session = uuid.uuid4()
        qid = await _turn(history, session=session)
        favorite_result, delete_result = await asyncio.gather(
            history.set_answer_favorite(
                conversation_id=session, turn_id=qid, user_id=USER_A, favorite=True,
            ),
            history.delete_conversation(conversation_id=session, user_id=USER_A),
        )
        conversation = await history.get_conversation(conversation_id=session, user_id=USER_A)
        if favorite_result is True:
            assert delete_result["status"] == "blocked"
            assert conversation is not None
        else:
            assert favorite_result is None
            assert delete_result["status"] == "deleted"
            assert conversation is None

    _run(pool, scenario())


def test_favorite_protects_conversation_and_artifact_until_removed(history, pool):
    async def scenario():
        migration = Path(__file__).resolve().parents[2] / "db" / "migrations" / "insights" / "030_favorite_answers.sql"
        async with history.pool.acquire() as conn:
            await conn.execute(migration.read_text(encoding="utf-8"))
        history.favorite_schema_ready = True

        favorite_session, middle_session, newest_session = (uuid.uuid4() for _ in range(3))
        favorite_turn = await _turn(history, session=favorite_session, question="keep me")
        await _turn(history, session=middle_session, question="middle")
        await _turn(history, session=newest_session, question="newest")
        async with history.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE insights_conversations
                SET last_activity_at = CASE id
                    WHEN $1 THEN NOW() - INTERVAL '3 days'
                    WHEN $2 THEN NOW() - INTERVAL '2 days'
                    WHEN $3 THEN NOW() - INTERVAL '1 day'
                END
                WHERE id = ANY($4::uuid[])
                """,
                favorite_session, middle_session, newest_session,
                [favorite_session, middle_session, newest_session],
            )

        assert await history.set_answer_favorite(
            conversation_id=favorite_session,
            turn_id=favorite_turn,
            user_id=USER_A,
            favorite=True,
        )
        stats = await history.prune_user_conversations(
            user_id=USER_A, source_key=SRC_A, keep_last=1, keep_last_turns=1,
            min_interval_seconds=0,
        )
        assert stats["deleted_conversations"] == 1
        assert await history.get_conversation(conversation_id=favorite_session, user_id=USER_A)
        artifact = await history.get_turn_artifact(
            conversation_id=favorite_session, turn_id=favorite_turn, user_id=USER_A,
        )
        assert artifact["snapshot_status"] == "stored" and artifact["results"] is not None
        favorites = await history.list_favorite_answers(user_id=USER_A)
        assert [item["turn_id"] for item in favorites] == [str(favorite_turn)]

        assert await history.set_answer_favorite(
            conversation_id=favorite_session,
            turn_id=favorite_turn,
            user_id=USER_A,
            favorite=False,
        ) is False
        await history.prune_user_conversations(
            user_id=USER_A, source_key=SRC_A, keep_last=1, keep_last_turns=1,
            min_interval_seconds=0,
        )
        assert await history.get_conversation(conversation_id=favorite_session, user_id=USER_A) is None

    _run(pool, scenario())


def test_favorite_and_retention_are_serialized(history, pool):
    async def scenario():
        migration = Path(__file__).resolve().parents[2] / "db" / "migrations" / "insights" / "030_favorite_answers.sql"
        async with history.pool.acquire() as conn:
            await conn.execute(migration.read_text(encoding="utf-8"))
        history.favorite_schema_ready = True

        target, newest = uuid.uuid4(), uuid.uuid4()
        target_turn = await _turn(history, session=target, question="race target")
        await _turn(history, session=newest, question="newest")
        async with history.pool.acquire() as conn:
            await conn.execute(
                "UPDATE insights_conversations SET last_activity_at = NOW() - INTERVAL '1 day' WHERE id = $1",
                target,
            )

        favorite_result, _stats = await asyncio.gather(
            history.set_answer_favorite(
                conversation_id=target, turn_id=target_turn, user_id=USER_A, favorite=True,
            ),
            history.prune_user_conversations(
                user_id=USER_A, source_key=SRC_A, keep_last=1, keep_last_turns=1,
                min_interval_seconds=0,
            ),
        )
        conversation = await history.get_conversation(conversation_id=target, user_id=USER_A)
        if favorite_result is True:
            assert conversation is not None, "a successful favorite must protect its conversation"
            artifact = await history.get_turn_artifact(
                conversation_id=target, turn_id=target_turn, user_id=USER_A,
            )
            assert artifact["snapshot_status"] == "stored" and artifact["results"] is not None
        else:
            assert favorite_result is None and conversation is None

    _run(pool, scenario())


def test_prune_bounds_conversations_and_snapshots(history, pool):
    async def scenario():
        # 4 conversations; the newest ("active") one has 5 turns.
        sessions = [uuid.uuid4() for _ in range(4)]
        older_qids = [await _turn(history, session=s) for s in sessions[:3]]
        active = sessions[3]
        active_turns = [await _turn(history, session=active, question=f"a{i}") for i in range(5)]
        # One text-only turn must never be touched by the snapshot step.
        text_qid = await history.log_query(user_id=USER_A, source_key=SRC_A, session_id=active, natural_language_query="hi")
        await history.upsert_turn_artifact(turn_id=text_qid, result_kind="text", answer="Hello", snapshot_status="not_applicable")

        stats = await history.prune_user_conversations(
            user_id=USER_A, source_key=SRC_A, keep_last=2, keep_last_turns=3,
            min_interval_seconds=600, protect_conversation_id=active,
        )
        # 8 blob-holding turns before; 2 leave with the deleted conversations,
        # keep_last_turns=3 survive, so exactly 3 are pruned.
        assert stats["deleted_conversations"] == 2
        assert stats["pruned_turns"] == 3
        async with history.pool.acquire() as conn:
            remaining = {r["id"] for r in await conn.fetch(
                "SELECT id FROM insights_conversations WHERE user_id = $1 AND source_key = $2", USER_A, SRC_A)}
            assert active in remaining and len(remaining) == 2, "keep_last honoured, protected kept"
            with_blobs = await conn.fetchval(
                """
                SELECT COUNT(*) FROM insights_turn_artifacts a
                JOIN insights_conversation_sessions cs ON cs.id = a.turn_id
                WHERE cs.user_id = $1 AND cs.source_key = $2
                  AND (a.result_snapshot IS NOT NULL OR a.chart_config IS NOT NULL)
                """, USER_A, SRC_A)
            assert with_blobs == 3, "exactly keep_last_turns snapshots survive, even inside the active conversation"
            newest = await conn.fetchrow("SELECT snapshot_status, chart_config FROM insights_turn_artifacts WHERE turn_id = $1", active_turns[-1])
            assert newest["snapshot_status"] == "stored" and newest["chart_config"] is not None
            oldest = await conn.fetchrow("SELECT snapshot_status, result_snapshot, chart_spec FROM insights_turn_artifacts WHERE turn_id = $1", active_turns[0])
            assert oldest["snapshot_status"] == "pruned" and oldest["result_snapshot"] is None and oldest["chart_spec"] is None
            text = await conn.fetchrow("SELECT snapshot_status, answer FROM insights_turn_artifacts WHERE turn_id = $1", text_qid)
            assert text["snapshot_status"] == "not_applicable" and text["answer"] is not None

        # Claim: a second run inside the interval is skipped; after the interval it runs and is idempotent.
        assert (await history.prune_user_conversations(
            user_id=USER_A, source_key=SRC_A, keep_last=2, keep_last_turns=3, min_interval_seconds=600))["skipped"] == "recently_pruned"
        again = await history.prune_user_conversations(
            user_id=USER_A, source_key=SRC_A, keep_last=2, keep_last_turns=3, min_interval_seconds=0)
        assert again["deleted_conversations"] == 0 and again["pruned_turns"] == 0

        # Pruned is terminal for normal writes; the rerun write promotes it back.
        assert not await history.upsert_turn_artifact(
            turn_id=active_turns[0], result_kind="table",
            result_snapshot={"columns": ["a"], "rows": [[1]], "row_count": 1}, snapshot_status="stored", snapshot_bytes=5)
        assert not await history.upsert_turn_chart(
            turn_id=active_turns[0], user_id=USER_A, chart_spec={}, chart_config={"series": []}, chart_bytes=5)
        assert await history.store_rerun_snapshot(
            turn_id=active_turns[0], user_id=USER_A,
            result_snapshot={"columns": ["a"], "rows": [[9]], "row_count": 1}, snapshot_status="stored", snapshot_bytes=5)
        art = await history.get_turn_artifact(conversation_id=active, turn_id=active_turns[0], user_id=USER_A)
        assert art["snapshot_status"] == "stored" and art["results"]["rows"] == [[9]]
        assert art["chart_config"] is None, "rerun invalidates the old chart"

        # Deleted conversation: a late artifact/chart write for one of its turns is a no-op.
        gone_qid = older_qids[0]
        assert await history.get_conversation(conversation_id=sessions[0], user_id=USER_A) is None
        assert not await history.upsert_turn_artifact(
            turn_id=gone_qid, result_kind="table",
            result_snapshot={"columns": ["a"], "rows": [[1]], "row_count": 1}, snapshot_status="stored", snapshot_bytes=5)
        assert not await history.upsert_turn_chart(
            turn_id=gone_qid, user_id=USER_A, chart_spec={}, chart_config={"series": []}, chart_bytes=1)

    _run(pool, scenario())


def test_routes_serialize_real_rows(history, monkeypatch):
    """Drive the FastAPI conversation routes against the real service so the
    DTOs are proven against asyncpg row types (JSONB text, TIMESTAMPTZ).

    Everything runs on one fresh event loop with its own pool: asyncpg
    connections are bound to the loop that created them.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import asyncpg
    import httpx

    from src.agent.conversation_history import ConversationHistoryService
    from src.api import app
    from src.api import conversation_retention
    from src.api import state as api_state
    from src.config import settings
    from src.security.internal_auth import issue_internal_token

    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", False)

    def token(user):
        return f"Bearer {issue_internal_token({'user_id': user, 'role': 'viewer', 'name': user, 'email': f'{user}@x'})}"

    async def scenario():
        local_pool = await asyncpg.create_pool(
            host=settings.METADATA_DB_HOST, port=settings.METADATA_DB_PORT,
            database=settings.METADATA_DB_NAME, user=settings.METADATA_DB_USER,
            password=settings.METADATA_DB_PASSWORD, min_size=1, max_size=3,
        )
        svc = ConversationHistoryService(local_pool, persistence_enabled=True)
        connections = MagicMock()
        connections.list_connections = AsyncMock(return_value=[SimpleNamespace(source_key=SRC_A)])
        monkeypatch.setattr(api_state, "history_service", svc)
        monkeypatch.setattr(api_state, "connection_service", connections)
        try:
            session = uuid.uuid4()
            qid = await _turn(svc, session=session, rows=2)
            text_qid = await svc.log_query(
                user_id=USER_A, source_key=SRC_A, session_id=session, natural_language_query="thanks")
            await svc.update_execution(query_id=text_qid, execution_status="success", row_count=0)
            await svc.upsert_turn_artifact(
                turn_id=text_qid, result_kind="text", answer=[{"t": "You're welcome", "hl": None}],
                metrics={"input_tokens": 3}, snapshot_status="not_applicable")

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                a = {"Authorization": token(USER_A)}
                b = {"Authorization": token(USER_B)}

                last = await client.get(f"/api/conversations/last?connection={SRC_A}", headers=a)
                assert last.status_code == 200, last.text
                body = last.json()
                assert body["conversation"]["id"] == str(session)
                assert body["conversation"]["connection_available"] is True
                assert body["conversation"]["turn_count"] == 2
                assert [t["sequence_number"] for t in body["turns"]] == [2, 1]
                text_turn, table_turn = body["turns"]
                assert text_turn["result_kind"] == "text" and text_turn["snapshot_status"] == "not_applicable"
                assert text_turn["answer"] == [{"t": "You're welcome", "hl": None}], "JSONB decoded, not a string"
                assert text_turn["metrics"] == {"input_tokens": 3}
                assert table_turn["has_chart"] is True and table_turn["snapshot_status"] == "stored"
                assert table_turn["created_at"] and "T" in table_turn["created_at"]

                art = await client.get(f"/api/conversations/{session}/turns/{qid}/artifact", headers=a)
                assert art.status_code == 200, art.text
                assert art.json()["results"]["rows"] == [[0], [1]]
                assert art.json()["chart_config"]["series"][0]["type"] == "bar"

                listed = await client.get("/api/conversations?all=true&limit=1", headers=a)
                assert listed.status_code == 200
                assert listed.json()["items"][0]["id"] == str(session)
                cursor = listed.json()["next_cursor"]
                assert cursor, "full page -> cursor"
                more = await client.get(f"/api/conversations?all=true&limit=1&before={cursor}", headers=a)
                assert more.status_code == 200 and more.json()["items"] == []

                # Another principal sees nothing.
                assert (await client.get(f"/api/conversations/last?connection={SRC_A}", headers=b)).json() is None
                assert (await client.get(f"/api/conversations/{session}", headers=b)).status_code == 404
                assert (await client.get(f"/api/conversations/{session}/turns/{qid}/artifact", headers=b)).status_code == 404
                assert (await client.delete(f"/api/conversations/{session}", headers=b)).status_code == 404

                renamed = await client.patch(f"/api/conversations/{session}", json={"title": "Renamed via API"}, headers=a)
                assert renamed.status_code == 200
                detail = await client.get(f"/api/conversations/{session}", headers=a)
                assert detail.json()["conversation"]["title"] == "Renamed via API"
                assert (await client.delete(f"/api/conversations/{session}", headers=a)).status_code == 200
                assert (await client.get(f"/api/conversations/last?connection={SRC_A}", headers=a)).json() is None
        finally:
            await local_pool.close()

    asyncio.run(scenario())


def test_hydration_reads_are_owner_scoped(history, pool):
    async def scenario():
        session = uuid.uuid4()
        qid = await _turn(history, session=session, rows=2)
        last = await history.get_last_conversation(user_id=USER_A, source_key=SRC_A)
        assert last and last["id"] == str(session) and last["turn_count"] == 1
        assert await history.get_last_conversation(user_id=USER_B, source_key=SRC_A) is None
        turns = await history.get_conversation_turns(conversation_id=session, user_id=USER_A, limit=10)
        assert turns[0]["turn_id"] == str(qid)
        assert turns[0]["snapshot_status"] == "stored" and turns[0]["has_chart"] is True
        assert turns[0]["has_rerunnable_query"] is True
        assert await history.get_conversation_turns(conversation_id=session, user_id=USER_B, limit=10) == []
        art = await history.get_turn_artifact(conversation_id=session, turn_id=qid, user_id=USER_A)
        assert art["results"]["row_count"] == 2 and art["chart_config"]["series"][0]["type"] == "bar"
        assert await history.get_turn_artifact(conversation_id=session, turn_id=qid, user_id=USER_B) is None
        listed = await history.list_conversations(user_id=USER_A, source_key=None, limit=10)
        assert [c["id"] for c in listed] == [str(session)]
        # An over-cap chart update drops the stored baseline (owner-scoped).
        assert not await history.clear_turn_chart(turn_id=qid, user_id=USER_B)
        assert await history.clear_turn_chart(turn_id=qid, user_id=USER_A)
        turns = await history.get_conversation_turns(conversation_id=session, user_id=USER_A, limit=10)
        assert turns[0]["has_chart"] is False and turns[0]["snapshot_status"] == "stored"
        assert await history.rename_conversation(conversation_id=session, user_id=USER_A, title="Renamed")
        assert (await history.get_conversation(conversation_id=session, user_id=USER_A))["title"] == "Renamed"

    _run(pool, scenario())
