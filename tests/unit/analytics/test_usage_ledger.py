"""Usage ledger (migration 036): the write side, the pure helpers and the
shape of the migration. Offline: the asyncpg pool is a small fake that records
every statement.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from uuid import UUID

import pytest

from src.analytics.usage_ledger import (
    DETAIL_ALLOWLIST,
    PRUNE_BATCH,
    QUESTION_ROUTES,
    UsageLedger,
    filter_detail,
)

ROOT = Path(__file__).resolve().parents[3]
QID = UUID("11111111-1111-1111-1111-111111111111")
FID = UUID("22222222-2222-2222-2222-222222222222")


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        self.pool.calls.append((" ".join(sql.split()), args))
        if self.pool.fail:
            raise RuntimeError("db down")
        if sql.strip().upper().startswith("DELETE"):
            n = self.pool.delete_plan.pop(0) if self.pool.delete_plan else 0
            return f"DELETE {n}"
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        self.pool.calls.append((" ".join(sql.split()), args))
        return self.pool.lock_ok


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, *, fail=False, lock_ok=True, delete_plan=None):
        self.calls = []
        self.fail = fail
        self.lock_ok = lock_ok
        self.delete_plan = list(delete_plan or [])

    def acquire(self):
        return _Acquire(self)


def _run(coro):
    return asyncio.run(coro)


# ── pure helpers ─────────────────────────────────────────────────────────

def test_filter_detail_keeps_only_allowlisted_names_never_values():
    out = filter_detail({
        "method": "ets",
        "refused_by": [{"guard": "min_points", "observed": 3, "required": 12}, "seasonality"],
        "connector_error_type": "timeout",
        "low_confidence": True,
        "params": {"filters": {"region": "EMEA"}},   # dropped
        "rows": [[1, 2, 3]],                          # dropped
        "message": "the customer said ...",           # dropped
    })
    assert set(out) <= DETAIL_ALLOWLIST
    assert out["refused_by"] == ["min_points", "seasonality"]   # names only
    assert "params" not in out and "rows" not in out and "message" not in out
    assert filter_detail(None) == {} and filter_detail("x") == {}


def test_question_routes_cover_sql_ml_and_backfill_values():
    assert set(QUESTION_ROUTES) == {"needs_query", "needs_analysis", "sql"}


# ── writes ───────────────────────────────────────────────────────────────

def test_record_query_inserts_with_on_conflict_and_truncated_question():
    pool = FakePool()
    ledger = UsageLedger(pool, schema_ready=True)
    _run(ledger.record_query(
        user_id="7", source_key="sales_db", query_id=str(QID), session_id=None,
        outcome="success", route="needs_query", skill=None, llm_model="gpt-5.1",
        token_usage={"total_tokens": 1200, "input_tokens": 900}, llm_latency_ms=800,
        execution_time_ms=45, graph_time_ms=1300, row_count=10,
        question="x" * 900, detail={"connector_error_type": None, "rows": [1]},
    ))
    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    assert "INSERT INTO insights_usage_events" in sql and "'query'" in sql
    assert sql.endswith("ON CONFLICT DO NOTHING")
    assert args[0] == "7" and args[1] == "sales_db" and args[2] == QID
    assert args[4] == "success" and args[6] == "needs_query" and args[8] == "gpt-5.1"
    assert args[9] == 1200 and args[10] == 900
    assert len(args[15]) == 500                       # question excerpt
    assert json.loads(args[16]) == {}                 # no allowlisted keys survived


def test_record_query_maps_unknown_outcome_to_error_and_skips_without_turn():
    pool = FakePool()
    ledger = UsageLedger(pool, schema_ready=True)
    _run(ledger.record_query(user_id="7", source_key="s", query_id="not-a-uuid", outcome="success"))
    assert pool.calls == []                           # no turn id → nothing to count
    _run(ledger.record_query(user_id="7", source_key="s", query_id=QID, outcome="weird"))
    assert pool.calls[0][1][4] == "error"


def test_record_feedback_requires_feedback_id_and_validates_thumb():
    pool = FakePool()
    ledger = UsageLedger(pool, schema_ready=True)
    _run(ledger.record_feedback(user_id="7", source_key="s", query_id=QID, feedback_id=None, thumb="thumbs_up"))
    assert pool.calls == []
    _run(ledger.record_feedback(
        user_id="7", source_key="s", query_id=QID, feedback_id=FID, thumb="meh",
        rating=4, feedback_type="report_bug", message="  wrong total  ", question="q",
    ))
    sql, args = pool.calls[0]
    assert "'feedback'" in sql and sql.endswith("ON CONFLICT DO NOTHING")
    assert args[4] == FID and args[5] is None         # invalid thumb dropped, id kept
    assert args[6] == 4 and args[7] == "report_bug" and args[8] == "wrong total"


def test_record_analysis_maps_runner_outcomes():
    pool = FakePool()
    ledger = UsageLedger(pool, schema_ready=True)
    for given, expected in (("ok", "success"), ("guard_failed", "refused"), ("error", "error"), ("boom", "error")):
        _run(ledger.record_analysis(user_id="7", source_key="s", query_id=QID, skill="forecast", outcome=given))
        assert pool.calls[-1][1][4] == expected
    assert "'analysis'" in pool.calls[-1][0]


def test_writes_never_raise_and_are_noops_when_inactive():
    failing = UsageLedger(FakePool(fail=True), schema_ready=True)
    _run(failing.record_query(user_id="7", source_key="s", query_id=QID, outcome="success"))  # swallowed

    off = FakePool()
    for ledger in (UsageLedger(off, schema_ready=False), UsageLedger(off, schema_ready=True, enabled=False),
                   UsageLedger(None, schema_ready=True)):
        assert ledger.active is False
        _run(ledger.record_query(user_id="7", source_key="s", query_id=QID, outcome="success"))
        _run(ledger.record_feedback(user_id="7", source_key="s", query_id=QID, feedback_id=FID, thumb="thumbs_up"))
        _run(ledger.record_analysis(user_id="7", source_key="s", query_id=QID, skill="f", outcome="ok"))
        assert _run(ledger.prune(30)) == 0
    assert off.calls == []


# ── prune ────────────────────────────────────────────────────────────────

def test_prune_batches_under_an_advisory_lock_and_always_unlocks():
    pool = FakePool(delete_plan=[PRUNE_BATCH, PRUNE_BATCH, 17])
    deleted = _run(UsageLedger(pool, schema_ready=True).prune(400))
    assert deleted == 2 * PRUNE_BATCH + 17
    sqls = [c[0] for c in pool.calls]
    assert sqls[0].startswith("SELECT pg_try_advisory_lock")
    deletes = [c for c in pool.calls if c[0].startswith("DELETE")]
    assert len(deletes) == 3                          # stopped after the short batch
    assert all("LIMIT $2" in c[0] and c[1] == (400, PRUNE_BATCH) for c in deletes)
    assert sqls[-1].startswith("SELECT pg_advisory_unlock")


def test_prune_skips_when_another_replica_holds_the_lock():
    pool = FakePool(lock_ok=False)
    assert _run(UsageLedger(pool, schema_ready=True).prune(400)) == 0
    assert not any(c[0].startswith("DELETE") for c in pool.calls)


def test_prune_ignores_non_positive_retention():
    pool = FakePool()
    assert _run(UsageLedger(pool, schema_ready=True).prune(0)) == 0
    assert pool.calls == []


# ── migration 036 shape ──────────────────────────────────────────────────

def test_migration_036_defines_the_ledger_its_guards_and_a_utc_safe_backfill():
    sql = (ROOT / "db/migrations/insights/036_usage_events.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS insights_usage_events" in sql
    assert "GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in sql
    # No FK to the turn: the whole point is surviving conversation retention.
    assert "REFERENCES insights_conversation_sessions" not in sql
    assert "event_type IN ('login', 'query', 'feedback', 'analysis')" in sql
    assert "thumb IN ('thumbs_up', 'thumbs_down', 'cleared')" in sql
    assert "char_length(question) <= 500" in sql
    # Idempotency guards behind ON CONFLICT DO NOTHING.
    assert "uq_insights_usage_events_query" in sql and "WHERE event_type = 'query'" in sql
    assert "uq_insights_usage_events_analysis" in sql
    assert "uq_insights_usage_events_feedback" in sql and "(feedback_id) WHERE event_type = 'feedback'" in sql
    # Feed keyset index matches ORDER BY id DESC.
    assert "(id DESC) WHERE event_type = 'feedback'" in sql
    # Backfill converts the naive timestamp explicitly and is repeat-safe.
    assert "cs.created_at AT TIME ZONE 'UTC'" in sql
    assert sql.count("ON CONFLICT DO NOTHING") >= 2
    assert "FROM insights_answer_feedback f" in sql
    # Documented in the DB.
    assert "COMMENT ON TABLE insights_usage_events" in sql
    for column in ("event_type", "user_id", "query_id", "outcome", "route", "feedback_id", "thumb", "question", "detail"):
        assert f"COMMENT ON COLUMN insights_usage_events.{column} IS" in sql, column
    # Shared metadata DB: only Insights-owned objects.
    assert not re.search(r"\b(metadata_|settings_services|admin_)\w*", sql)


def test_migration_files_are_sequential():
    names = sorted(p.name for p in (ROOT / "db/migrations/insights").glob("*.sql"))
    numbers = [int(name.split("_", 1)[0]) for name in names]
    assert numbers == list(range(1, len(names) + 1)), "migration numbers must be consecutive and unique"
    assert "036_usage_events.sql" in names
