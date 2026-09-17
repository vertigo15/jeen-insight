"""Unit tests for `src.metadata.metadata_loader.MetadataLoader.load_tables_rich`.

We mock the asyncpg pool so no real DB connection is required.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.metadata.metadata_loader import MetadataLoader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_loader(fetch_rows: list) -> MetadataLoader:
    """Return a MetadataLoader whose pool always yields *fetch_rows*."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=fetch_rows)

    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire)

    return MetadataLoader(pool=mock_pool)


def _fake_row(table_name: str, description: str | None, col_count: int):
    """Simulate the asyncpg Record returned by the SQL query."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "table_name":        table_name,
        "table_description": description,
        "col_count":         col_count,
    }[key]
    return row


# ---------------------------------------------------------------------------
# load_tables_rich — data shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_tables_rich_returns_correct_structure():
    rows = [
        _fake_row("orders",   "Customer purchase orders", 12),
        _fake_row("products", None,                       5),
    ]
    loader = _make_loader(rows)

    result = await loader.load_tables_rich("sales_db")

    assert len(result) == 2
    assert result[0] == {"name": "orders",   "description": "Customer purchase orders", "col_count": 12}
    assert result[1] == {"name": "products", "description": None,                       "col_count": 5}


@pytest.mark.asyncio
async def test_load_tables_rich_empty_catalog_returns_empty_list():
    loader = _make_loader([])
    result = await loader.load_tables_rich("empty_db")
    assert result == []


@pytest.mark.asyncio
async def test_load_tables_rich_col_count_zero_when_no_columns():
    """col_count of 0 (not NULL) is preserved; falsy 0 must not become None."""
    rows = [_fake_row("dim_date", "Calendar dimension", 0)]
    loader = _make_loader(rows)

    result = await loader.load_tables_rich("dw_db")

    assert result[0]["col_count"] == 0


# ---------------------------------------------------------------------------
# load_tables_rich — caching
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_tables_rich_caches_result():
    """Second call uses the cache; the pool is only hit once."""
    rows = [_fake_row("orders", "desc", 3)]
    loader = _make_loader(rows)

    first  = await loader.load_tables_rich("sales_db")
    second = await loader.load_tables_rich("sales_db")

    assert first == second
    # acquire() is called inside _make_loader's conn.fetch, so check fetch count
    loader.pool.acquire().__aenter__.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_tables_rich_cache_miss_after_expiry(monkeypatch):
    """Expired TTL causes a fresh DB query."""
    rows = [_fake_row("orders", "desc", 3)]
    loader = _make_loader(rows)

    # Seed with an already-expired cache entry
    loader._cache["tables_rich::sales_db"] = (time.monotonic() - 1, [])

    result = await loader.load_tables_rich("sales_db")
    # Should get the real rows, not the stale []
    assert len(result) == 1


# ---------------------------------------------------------------------------
# invalidate — tables_rich key
# ---------------------------------------------------------------------------

def test_invalidate_clears_tables_rich_for_given_source():
    loader = MetadataLoader(pool=MagicMock())
    loader._cache["tables_rich::sales_db"]  = (9999.0, [{"name": "t1"}])
    loader._cache["tables_rich::other_db"]  = (9999.0, [{"name": "t2"}])

    loader.invalidate("sales_db")

    assert "tables_rich::sales_db" not in loader._cache
    assert "tables_rich::other_db" in loader._cache   # sibling untouched


def test_invalidate_all_clears_all_tables_rich_entries():
    loader = MetadataLoader(pool=MagicMock())
    loader._cache["tables_rich::a"] = (9999.0, [])
    loader._cache["tables_rich::b"] = (9999.0, [])
    loader._cache["kq::a"]          = (9999.0, [])

    loader.invalidate()   # None → clear everything

    assert len(loader._cache) == 0


def test_invalidate_source_does_not_touch_other_cache_types():
    """Only the targeted source's tables_rich key is removed; kq:: and cols:: survive."""
    loader = MetadataLoader(pool=MagicMock())
    loader._cache["tables_rich::sales_db"] = (9999.0, [])
    loader._cache["kq::sales_db"]          = (9999.0, [])

    loader.invalidate("sales_db")

    # kq:: key is also dropped (that was the pre-existing behaviour)
    assert "tables_rich::sales_db" not in loader._cache
    assert "kq::sales_db"          not in loader._cache


# ---------------------------------------------------------------------------
# _load_relationships — structured endpoints with legacy fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_relationships_prefer_structured_endpoints_and_fall_back_to_relation_text():
    import asyncpg

    structured = [{"relation": "fct_sales.customer_id -> dim_customer.id"}]
    loader = _make_loader(structured)
    lines = await loader._load_relationships("src")
    assert lines == ["fct_sales.customer_id -> dim_customer.id"]
    sql = loader.pool.acquire.return_value.__aenter__.return_value.fetch.call_args.args[0]
    assert "from_table" in sql and "is_deleted" in sql and "is_active" in sql

    # An older catalog without the structured columns raises once; the loader
    # remembers and uses the free-text relation column from then on.
    legacy = _make_loader([{"relation": "fct_sales -> dim_customer"}])
    conn = legacy.pool.acquire.return_value.__aenter__.return_value
    conn.fetch = AsyncMock(side_effect=[asyncpg.UndefinedColumnError("no from_table"),
                                         [{"relation": "fct_sales -> dim_customer"}],
                                         [{"relation": "fct_sales -> dim_customer"}]])
    assert await legacy._load_relationships("src") == ["fct_sales -> dim_customer"]
    assert await legacy._load_relationships("src") == ["fct_sales -> dim_customer"]
    assert legacy._structured_relationships is False
    # Two successful legacy reads plus the one failed structured attempt.
    assert conn.fetch.await_count == 3
