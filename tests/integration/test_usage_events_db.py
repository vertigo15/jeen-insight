"""Real-PostgreSQL tests for the usage ledger (migration 036).

What only a database can prove:

* the 036 backfill copies existing turns and feedback into the ledger,
  interprets the naive ``created_at`` as UTC, and is a no-op when re-run;
* deleting a conversation (retention) leaves the ledger rows in place;
* the writers' ``ON CONFLICT DO NOTHING`` really dedups a retried turn;
* the repository's current-thumb derivation treats ``cleared`` as no thumb;
* the prune deletes only expired rows, in batches, under the advisory lock.

Run against an ephemeral Postgres:

    docker run -d --name jeen-e2e-pg -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e \
        -e POSTGRES_DB=e2e -p 55440:5432 postgres:16-alpine
    JEEN_E2E_DB=1 METADATA_DB_HOST=localhost METADATA_DB_PORT=55440 METADATA_DB_NAME=e2e \
      METADATA_DB_USER=e2e METADATA_DB_PASSWORD=e2e METADATA_DB_SSL=false \
      python3 -m pytest tests/integration/test_usage_events_db.py -q

Skips unless ``JEEN_E2E_DB`` is truthy.
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

USER = "e2e-usage-user"
SRC = "e2e_usage_src"
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "db" / "migrations" / "insights").glob("*.sql"))
M036 = next(p for p in MIGRATIONS if p.name.startswith("036_"))


async def _apply_migrations_upto(conn, last: str) -> None:
    """Baseline (as the migration runner does), then migration files <= *last*."""
    from src.metadata.insights_schema import ensure_insights_baseline

    await ensure_insights_baseline(conn, require_platform=False)
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


async def _reset(conn) -> None:
    await conn.execute("DELETE FROM insights_conversation_sessions WHERE user_id = $1", USER)
    if await conn.fetchval("SELECT to_regclass('insights_conversations') IS NOT NULL"):
        await conn.execute("DELETE FROM insights_conversations WHERE user_id = $1", USER)
    if await conn.fetchval("SELECT to_regclass('insights_usage_events') IS NOT NULL"):
        await conn.execute("DELETE FROM insights_usage_events WHERE user_id = $1", USER)


async def _seed_turn(conn, *, created_at: datetime, status="success", sql="select 1", question="q") -> uuid.UUID:
    session_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO insights_conversations (id, user_id, source_key, source_label, title, created_at, last_activity_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $6)",
        session_id, USER, SRC, SRC, question, created_at,
    )
    return await conn.fetchval(
        """
        INSERT INTO insights_conversation_sessions
            (user_id, source_key, session_id, sequence_number, natural_language_query,
             generated_sql, execution_status, created_at, tokens_used, llm_model)
        VALUES ($1, $2, $3, 1, $4, $5, $6, $7, 42, 'gpt-test')
        RETURNING id
        """,
        USER, SRC, session_id, question, sql, status, created_at.replace(tzinfo=None),
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


# ── backfill ─────────────────────────────────────────────────────────────

def test_036_backfills_turns_and_feedback_as_utc_and_is_idempotent(pool):
    p, _ = pool

    async def scenario():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, "035_answer_feedback.sql")
            # Simulate a database that has not seen 036 yet.
            await conn.execute("DROP TABLE IF EXISTS insights_usage_events")
            await conn.execute("DELETE FROM insights_schema_migrations WHERE revision LIKE '036_%'")
            await conn.execute("ALTER TABLE insights_conversation_sessions ADD COLUMN IF NOT EXISTS graph_time_ms INT")
            await _reset(conn)

            when = datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)
            ok = await _seed_turn(conn, created_at=when, question="revenue by month")
            bad = await _seed_turn(conn, created_at=when + timedelta(hours=1), status="error", question="broken")
            text = await _seed_turn(conn, created_at=when + timedelta(hours=2), sql=None, question="hi")
            pending = await _seed_turn(conn, created_at=when + timedelta(hours=3), status="pending")
            await conn.execute(
                "INSERT INTO insights_answer_feedback (query_id, user_id, source_key, thumb, message) "
                "VALUES ($1, $2, $3, 'thumbs_down', 'wrong total')",
                ok, USER, SRC,
            )

            await _apply_migrations_upto(conn, M036.name)

            rows = await conn.fetch(
                "SELECT event_type, query_id, occurred_at, outcome, route, question, thumb, message, total_tokens, llm_model "
                "FROM insights_usage_events WHERE user_id = $1 ORDER BY id",
                USER,
            )
            by_turn = {(r["event_type"], r["query_id"]): r for r in rows}
            assert ("query", pending) not in by_turn                     # pending turns are not usage
            assert by_turn[("query", ok)]["outcome"] == "success" and by_turn[("query", ok)]["route"] == "sql"
            assert by_turn[("query", bad)]["outcome"] == "error"
            assert by_turn[("query", text)]["route"] == "text"
            assert by_turn[("query", ok)]["total_tokens"] == 42 and by_turn[("query", ok)]["llm_model"] == "gpt-test"
            # Naive TIMESTAMP interpreted as UTC, whatever the session time zone.
            assert by_turn[("query", ok)]["occurred_at"] == when
            fb = by_turn[("feedback", ok)]
            assert fb["thumb"] == "thumbs_down" and fb["message"] == "wrong total" and fb["question"] == "revenue by month"
            before = await conn.fetchval("SELECT COUNT(*) FROM insights_usage_events WHERE user_id = $1", USER)

            # Re-running the backfill statements is a no-op (unique guards).
            await conn.execute(M036.read_text(encoding="utf-8"))
            after = await conn.fetchval("SELECT COUNT(*) FROM insights_usage_events WHERE user_id = $1", USER)
            assert after == before == 4

    _run(pool, scenario())


# ── retention independence + writer dedup ────────────────────────────────

def test_ledger_rows_survive_conversation_deletion_and_writers_dedup(pool):
    from src.analytics import UsageLedger

    p, _ = pool

    async def scenario():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, M036.name)
            await _reset(conn)
            when = datetime.now(timezone.utc) - timedelta(days=1)
            turn = await _seed_turn(conn, created_at=when)
            session_id = await conn.fetchval("SELECT session_id FROM insights_conversation_sessions WHERE id = $1", turn)

        ledger = UsageLedger(p, schema_ready=True)
        for _ in range(3):   # a retried graph run
            await ledger.record_query(user_id=USER, source_key=SRC, query_id=turn, session_id=session_id,
                                      outcome="success", route="needs_query", question="q")
        fid = uuid.uuid4()
        await ledger.record_feedback(user_id=USER, source_key=SRC, query_id=turn, feedback_id=fid, thumb="thumbs_up")
        await ledger.record_feedback(user_id=USER, source_key=SRC, query_id=turn, feedback_id=fid, thumb="thumbs_up")
        await ledger.record_analysis(user_id=USER, source_key=SRC, query_id=turn, skill="forecast", outcome="ok")
        await ledger.record_analysis(user_id=USER, source_key=SRC, query_id=turn, skill="forecast", outcome="ok")

        async with p.acquire() as conn:
            counts = dict(await conn.fetch(
                "SELECT event_type, COUNT(*) FROM insights_usage_events WHERE user_id = $1 GROUP BY 1", USER))
            assert counts == {"query": 1, "feedback": 1, "analysis": 1}
            # Retention deletes the conversation; the ledger keeps every row.
            await conn.execute("DELETE FROM insights_conversations WHERE id = $1", session_id)
            assert await conn.fetchval("SELECT COUNT(*) FROM insights_conversation_sessions WHERE id = $1", turn) == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM insights_usage_events WHERE user_id = $1", USER) == 3

    _run(pool, scenario())


# ── current thumb + reports ──────────────────────────────────────────────

def test_repository_reads_current_thumb_with_cleared_and_counts_activity(pool):
    from src.analytics import UsageAnalyticsRepository, UsageLedger

    p, _ = pool

    async def scenario():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, M036.name)
            await _reset(conn)
        ledger = UsageLedger(p, schema_ready=True)
        t1, t2 = uuid.uuid4(), uuid.uuid4()
        await ledger.record_query(user_id=USER, source_key=SRC, query_id=t1, outcome="success", route="needs_query", question="a")
        await ledger.record_query(user_id=USER, source_key=SRC, query_id=t2, outcome="error", route="needs_query",
                                  error_type="timeout", question="b")
        await ledger.record_query(user_id=USER, source_key=SRC, query_id=uuid.uuid4(), outcome="success", route="greeting", question="hi")
        # t1: up, then cleared  → no current thumb.  t2: down.
        await ledger.record_feedback(user_id=USER, source_key=SRC, query_id=t1, feedback_id=uuid.uuid4(), thumb="thumbs_up")
        await ledger.record_feedback(user_id=USER, source_key=SRC, query_id=t1, feedback_id=uuid.uuid4(), thumb="cleared")
        await ledger.record_feedback(user_id=USER, source_key=SRC, query_id=t2, feedback_id=uuid.uuid4(), thumb="thumbs_down",
                                     message="wrong", question="b")
        async with p.acquire() as conn:
            await conn.execute("INSERT INTO insights_usage_events (event_type, user_id) VALUES ('login', $1)", USER)

        repo = UsageAnalyticsRepository(p)
        ov = await repo.overview(7)
        mine = [u for u in await repo.top_users(7, 100) if u["user_id"] == USER]
        conns = [c for c in await repo.top_connections(7, 100) if c["source_key"] == SRC]
        feed = await repo.feedback_feed(7, connection=SRC)
        series = await repo.timeseries(7)

        assert ov["current"]["thumbs_up_events"] >= 1                 # the raw click happened
        assert mine[0]["thumbs_up"] == 0 and mine[0]["thumbs_down"] == 1  # but the current thumb was withdrawn
        assert mine[0]["questions"] == 2 and mine[0]["success_rate"] == 0.5
        assert conns[0]["questions"] == 2 and conns[0]["thumbs_down_rate"] == 1.0
        assert [f["thumb"] for f in feed["items"]] == ["thumbs_down", "cleared", "thumbs_up"]
        assert feed["items"][0]["message"] == "wrong" and feed["items"][0]["question"] == "b"
        today = datetime.now(timezone.utc).date().isoformat()
        point = next(pt for pt in series if pt["day"] == today)
        assert point["questions"] >= 2 and point["logins"] >= 1 and point["active_users"] >= 1
        assert ov["dau"] >= 1

    _run(pool, scenario())


# ── prune ────────────────────────────────────────────────────────────────

def test_prune_removes_only_expired_rows(pool):
    from src.analytics import UsageLedger
    from src.analytics.usage_ledger import PRUNE_BATCH

    p, _ = pool

    async def scenario():
        async with p.acquire() as conn:
            await _apply_migrations_upto(conn, M036.name)
            await _reset(conn)
            old = datetime.now(timezone.utc) - timedelta(days=500)
            await conn.executemany(
                "INSERT INTO insights_usage_events (event_type, user_id, occurred_at) VALUES ('login', $1, $2)",
                [(USER, old) for _ in range(PRUNE_BATCH + 5)],
            )
            await conn.execute(
                "INSERT INTO insights_usage_events (event_type, user_id) VALUES ('login', $1)", USER)

        deleted = await UsageLedger(p, schema_ready=True).prune(400)
        assert deleted >= PRUNE_BATCH + 5
        async with p.acquire() as conn:
            left = await conn.fetch("SELECT occurred_at FROM insights_usage_events WHERE user_id = $1", USER)
            assert len(left) == 1 and left[0]["occurred_at"] > datetime.now(timezone.utc) - timedelta(days=1)
            # The advisory lock was released.
            assert await conn.fetchval("SELECT pg_try_advisory_lock($1)", 0x4A45454E_0036)
            await conn.execute("SELECT pg_advisory_unlock($1)", 0x4A45454E_0036)

    _run(pool, scenario())
