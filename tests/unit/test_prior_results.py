"""PriorResultStore tiers: cache → stored snapshot → re-run (src/agent/prior_results.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.agent.prior_results import (
    SOURCE_CACHE,
    SOURCE_RERUN,
    SOURCE_SNAPSHOT,
    PriorResultStore,
    make_sql_rerun,
)
from src.agent.snapshot_sql import MEMORY_SQL_MARKER
from src.api.result_cache import result_cache

ROWS = [{"a": 1}, {"a": 2}]


def _history(snapshot):
    h = MagicMock()
    h.get_turn_artifact = AsyncMock(return_value={"results": snapshot} if snapshot is not None else None)
    return h


def test_cache_tier_wins():
    result_cache.put(user_id="u", connection="c-cache", query_id="q1", dataset={"columns": ["a"], "rows": ROWS})
    store = PriorResultStore(history_service=_history({"columns": ["a"], "rows": [{"a": 9}]}))
    out = asyncio.run(store.rows(user_id="u", connection="c-cache", query_id="q1", session_id="s"))
    assert out["source"] == SOURCE_CACHE and out["rows"] == ROWS and out["row_count"] == 2


def test_snapshot_tier_when_cache_misses():
    store = PriorResultStore(history_service=_history({"columns": ["a"], "rows": ROWS}))
    out = asyncio.run(store.rows(user_id="u", connection="c-snap", query_id="q-miss", session_id="s"))
    assert out["source"] == SOURCE_SNAPSHOT and out["columns"] == ["a"]


def test_rerun_tier_when_snapshot_missing_and_sql_known():
    rerun = AsyncMock(return_value={"columns": ["a"], "rows": ROWS})
    store = PriorResultStore(history_service=_history(None), rerun=rerun)
    out = asyncio.run(store.rows(user_id="u", connection="c-rerun", query_id="q2", session_id="s", sql="SELECT a FROM t"))
    assert out["source"] == SOURCE_RERUN
    rerun.assert_awaited_once()
    assert rerun.await_args.args[0] == "SELECT a FROM t"


def test_memory_sql_is_never_rerun():
    rerun = AsyncMock(return_value={"columns": ["a"], "rows": ROWS})
    store = PriorResultStore(history_service=_history(None), rerun=rerun)
    out = asyncio.run(store.rows(user_id="u", connection="c-mem", query_id="q3", session_id="s",
                                 sql=f"{MEMORY_SQL_MARKER} computed over t1\nSELECT 1"))
    assert out is None
    rerun.assert_not_awaited()


def test_rerun_error_and_missing_everything_return_none():
    rerun = AsyncMock(return_value={"error": "boom"})
    store = PriorResultStore(history_service=_history(None), rerun=rerun)
    assert asyncio.run(store.rows(user_id="u", connection="c-err", query_id="q4", session_id="s", sql="SELECT 1")) is None
    assert asyncio.run(PriorResultStore().rows(user_id="u", connection="c-none", query_id="q5")) is None
    assert asyncio.run(PriorResultStore().rows(user_id="u", connection="c-none", query_id=None)) is None


def test_row_cap_truncates_and_flags():
    store = PriorResultStore(history_service=_history({"columns": ["a"], "rows": [{"a": i} for i in range(10)]}), max_rows=3)
    out = asyncio.run(store.rows(user_id="u", connection="c-cap", query_id="q6", session_id="s"))
    assert out["row_count"] == 3 and out["truncated"] is True


def test_make_sql_rerun_adapts_runner_and_skips_introspectors():
    runner = MagicMock()
    runner.run_sql = AsyncMock(return_value={"columns": ["a"], "rows": ROWS})
    rerun = make_sql_rerun(runner)
    asyncio.run(rerun("SELECT 1", 5))
    kwargs = runner.run_sql.await_args.kwargs
    assert kwargs["limit"] == 5 and kwargs["max_rows"] == 5
    assert make_sql_rerun(object()) is None
