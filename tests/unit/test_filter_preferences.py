"""Unit tests for remembered filter-grounding choices."""

from __future__ import annotations

from typing import Any, List

import pytest

from src.metadata.filter_preferences import FilterPreferenceStore, preference_rows


class TestPreferenceRows:
    def test_column_choice_writes_literal_and_role_rows(self):
        rows = preference_rows([{"literal": "Mosco", "table": "Dim_Dealer", "column": "City"}])
        assert {(r["literal"], r["role"]) for r in rows} == {("mosco", "city"), ("", "city")}
        literal_row = next(r for r in rows if r["literal"] == "mosco")
        assert (literal_row["table"], literal_row["column"], literal_row["any"]) == ("dim_dealer", "city", False)

    def test_value_choice_keeps_the_exact_value(self):
        rows = preference_rows([{"literal": "mosco", "table": "dim_customer", "column": "city", "value": "Moscow"}])
        literal_row = next(r for r in rows if r["literal"] == "mosco")
        assert literal_row["value"] == "Moscow"
        # The role-level row never carries a value: it is about the column, not the literal.
        assert next(r for r in rows if r["literal"] == "").get("value") is None

    def test_any_of_choice_is_literal_level_only(self):
        rows = preference_rows([{"literal": "mosco", "any": True}])
        assert rows == [{"literal": "mosco", "role": "", "table": None, "column": None, "any": True, "value": None}]

    def test_garbage_is_ignored(self):
        assert preference_rows([None, "x", {"literal": ""}, {"table": "t"}]) == []


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def fetch(self, sql, *args):
        self.pool.calls.append(("fetch", sql, args))
        return self.pool.rows

    async def execute(self, sql, *args):
        self.pool.calls.append(("execute", sql, args))


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, rows: List[Any] = ()):
        self.rows = list(rows)
        self.calls: List[Any] = []

    def acquire(self):
        return _Acquire(self)


@pytest.mark.asyncio
async def test_store_saves_upserts_and_loads_per_user_and_source():
    pool = _Pool(rows=[{"literal_norm": "mosco", "role_column": "city", "table_name": "dim_dealer",
                        "column_name": "city", "any_of": False, "chosen_value": None}])
    store = FilterPreferenceStore(pool)
    written = await store.save("u1", "sales", [{"literal": "mosco", "table": "dim_dealer", "column": "city"}])
    assert written == 2
    executes = [c for c in pool.calls if c[0] == "execute"]
    assert all("ON CONFLICT (user_id, source_key, literal_norm, role_column)" in c[1] for c in executes)
    assert executes[0][2][:2] == ("u1", "sales")

    loaded = await store.load("u1", "sales")
    assert loaded == [{"literal": "mosco", "role": "city", "table": "dim_dealer", "column": "city", "any": False, "value": None}]
    fetch = next(c for c in pool.calls if c[0] == "fetch")
    assert fetch[2][:2] == ("u1", "sales")


@pytest.mark.asyncio
async def test_store_without_a_pool_is_inert():
    store = FilterPreferenceStore()
    assert store.available is False
    assert await store.load("u1", "sales") == []
    assert await store.save("u1", "sales", [{"literal": "x", "table": "t", "column": "c"}]) == 0
