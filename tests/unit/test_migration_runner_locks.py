"""Bounded locking and verify-only bootstrap for the shared metadata DB.

The migration runner (scripts/run_insights_migrations.py) and the API's boot-time
baseline (src/metadata/insights_schema.py) share a database with Jeen Schema
Modeler, so they must fail fast instead of queuing behind another session, never
interpolate configuration into SQL, and — in verify-only mode — issue no DDL.
These tests drive both with a fake asyncpg connection.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from src.metadata import insights_schema

_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_insights_migrations.py"
_INTERVAL = re.compile(r"^\s*\d+(\.\d+)?\s*(ms|s|sec|min|h|hour|hours|minutes|seconds)?\s*$")


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_insights_migrations", _RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _interval_seconds(raw: str) -> float:
    """Mimic PostgreSQL: accept simple positive-or-zero intervals, reject anything else."""
    if not _INTERVAL.match(raw):
        raise ValueError(f"invalid input syntax for type interval: {raw!r}")
    number = float(re.match(r"\s*(\d+(\.\d+)?)", raw).group(1))
    unit = re.search(r"(ms|s|sec|min|h|hour|hours|minutes|seconds)\s*$", raw)
    factor = {"ms": 0.001, "min": 60, "minutes": 60, "h": 3600, "hour": 3600, "hours": 3600}
    return number * factor.get(unit.group(1) if unit else "s", 1)


class FakeConn:
    def __init__(self, try_lock_results=(), *, existing=None, fail_apply=False):
        self.try_lock_results = list(try_lock_results)
        self.existing = set(existing or ())
        self.fail_apply = fail_apply
        self.executed: list[str] = []
        self.fetched: list[tuple] = []
        self.set_config: list[tuple[str, str]] = []

    async def fetchval(self, sql, *args):
        self.fetched.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return self.try_lock_results.pop(0)
        if "EXTRACT(EPOCH FROM $1::text::interval)" in sql:
            return _interval_seconds(args[0])
        if "set_config" in sql:
            self.set_config.append((args[0], args[1]))
            return args[1]
        if "current_setting('search_path')" in sql:
            return "public"
        if "to_regclass" in sql:
            return args[0] in self.existing
        if "information_schema.columns" in sql:
            return f"{args[0]}.{args[1]}" in self.existing
        return None

    async def execute(self, sql, *args):
        self.executed.append(sql)

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.executed.append("BEGIN")

            async def __aexit__(self, *exc):
                conn.executed.append("ROLLBACK" if exc[0] else "COMMIT")
                return False

        return _Tx()


@pytest.fixture()
def runner(monkeypatch):
    module = _load_runner()
    monkeypatch.setattr(module, "_LOCK_POLL_SECONDS", 0.0)
    return module


# ── advisory lock: bounded wait ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_acquired_after_a_retry(runner):
    conn = FakeConn([False, True])
    await runner._acquire_migration_lock(conn, wait_seconds=5)
    assert sum("pg_try_advisory_lock" in s for s, _ in conn.fetched) == 2


@pytest.mark.asyncio
async def test_lock_wait_is_bounded_and_never_blocks(runner):
    conn = FakeConn([False] * 50)
    with pytest.raises(runner.MigrationLockTimeout):
        await runner._acquire_migration_lock(conn, wait_seconds=0)
    assert sum("pg_try_advisory_lock" in s for s, _ in conn.fetched) == 1
    assert not any("pg_advisory_lock(" in s for s, _ in conn.fetched)
    assert not any("pg_advisory_lock(" in s for s in conn.executed)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-1", "soon", ""])
def test_wait_seconds_must_be_finite_and_non_negative(runner, raw):
    with pytest.raises(runner.MigrationConfigError):
        runner._parse_wait_seconds(raw)


def test_wait_seconds_accepts_plain_numbers(runner):
    assert runner._parse_wait_seconds("120") == 120.0
    assert runner._parse_wait_seconds(" 0 ") == 0.0


# ── session timeouts: parameterised, validated, positive ───────────────────

@pytest.mark.asyncio
async def test_session_timeouts_go_through_set_config(runner, monkeypatch):
    monkeypatch.setattr(runner, "_LOCK_TIMEOUT", "15s")
    monkeypatch.setattr(runner, "_STATEMENT_TIMEOUT", "10min")
    conn = FakeConn()
    await runner._configure_session_timeouts(conn)
    assert conn.set_config == [("lock_timeout", "15s"), ("statement_timeout", "10min")]
    # Nothing from the environment is ever spliced into SQL text.
    assert not any("15s" in s or "10min" in s for s in conn.executed)
    assert not any("15s" in s or "10min" in s for s, _ in conn.fetched)


@pytest.mark.asyncio
async def test_quoted_attack_string_is_rejected_before_any_set(runner, monkeypatch):
    monkeypatch.setattr(runner, "_LOCK_TIMEOUT", "15s'; DROP TABLE app_settings; --")
    conn = FakeConn()
    with pytest.raises(ValueError):
        await runner._configure_session_timeouts(conn)
    assert conn.set_config == []
    assert conn.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("zero", ["0", "0s", "0ms"])
async def test_zero_timeout_would_disable_the_limit_and_is_rejected(runner, monkeypatch, zero):
    monkeypatch.setattr(runner, "_LOCK_TIMEOUT", zero)
    conn = FakeConn()
    with pytest.raises(runner.MigrationConfigError):
        await runner._configure_session_timeouts(conn)
    assert conn.set_config == []


# ── run(): the advisory lock is released even when a revision fails ────────

@pytest.mark.asyncio
async def test_run_unlocks_after_a_failed_revision(runner, monkeypatch):
    conn = FakeConn([True])

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def _pool():
        return _Pool()

    async def _close():
        pass

    async def _baseline(c, **kw):
        pass

    async def _boom(c):
        raise RuntimeError("revision 099 exploded")

    monkeypatch.setattr(runner, "get_metadata_pool", _pool)
    monkeypatch.setattr(runner, "close_metadata_pool", _close)
    monkeypatch.setattr(runner, "ensure_insights_baseline", _baseline)
    monkeypatch.setattr(runner, "_ensure_history", _baseline)
    monkeypatch.setattr(runner, "_apply_sql_files", _boom)
    monkeypatch.setattr(runner, "_LOCK_TIMEOUT", "15s")
    monkeypatch.setattr(runner, "_STATEMENT_TIMEOUT", "10min")

    with pytest.raises(RuntimeError, match="revision 099"):
        await runner.run()
    assert any("pg_advisory_unlock" in s for s in conn.executed)


# ── API boot: verify-only mode issues no DDL ────────────────────────────────

_ALL_PRESENT = set(insights_schema.PLATFORM_TABLES) | set(insights_schema.BASELINE_TABLES) | {
    f"{t}.{c}" for t, c in insights_schema.BASELINE_COLUMNS
}


@pytest.mark.asyncio
async def test_verify_only_mode_passes_without_ddl_when_baseline_exists():
    conn = FakeConn(existing=_ALL_PRESENT)
    await insights_schema.ensure_insights_baseline(conn, require_platform=True, apply=False)
    assert conn.executed == []


@pytest.mark.asyncio
async def test_verify_only_mode_names_every_missing_object():
    present = _ALL_PRESENT - {"insights_filter_preferences", "insights_conversation_sessions.node_trace"}
    conn = FakeConn(existing=present)
    with pytest.raises(insights_schema.InsightsSchemaMissing) as exc:
        await insights_schema.ensure_insights_baseline(conn, require_platform=True, apply=False)
    assert "insights_filter_preferences" in str(exc.value)
    assert "insights_conversation_sessions.node_trace" in str(exc.value)
    assert "SCHEMA_BOOTSTRAP_ON_START" in str(exc.value)
    assert conn.executed == []


@pytest.mark.asyncio
async def test_apply_mode_runs_ddl_inside_one_transaction_with_a_lock_timeout():
    conn = FakeConn(existing=set(insights_schema.PLATFORM_TABLES))
    await insights_schema.ensure_insights_baseline(conn, require_platform=True, apply=True)
    assert conn.executed[0] == "BEGIN"
    assert conn.executed[1].startswith("SET LOCAL lock_timeout")
    assert conn.executed[-1] == "COMMIT"
    assert any("CREATE TABLE IF NOT EXISTS app_settings" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_runner_keeps_its_own_session_timeout_for_the_baseline():
    conn = FakeConn(existing=set(insights_schema.PLATFORM_TABLES))
    await insights_schema.ensure_insights_baseline(
        conn, require_platform=False, apply=True, lock_timeout=None
    )
    assert conn.executed[0] == "BEGIN"
    assert not any("SET LOCAL lock_timeout" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_platform_tables_are_still_required_in_verify_mode():
    conn = FakeConn(existing=_ALL_PRESENT - {"admin_models"})
    with pytest.raises(insights_schema.PlatformSchemaMissing):
        await insights_schema.ensure_insights_baseline(conn, require_platform=True, apply=False)
