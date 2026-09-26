"""Unit tests for the MCP service layer.

Covers:
  - McpServer dataclass helpers (get_tool_for_need, is_ready, health_status)
  - McpServerService CRUD (mocked pool)
  - McpCacheService L1 in-memory behaviour (no DB required)
  - McpCatalogClient formatters and _map_tool_to_need heuristics
  - McpCatalogClient.load_all cache-hit and cache-miss paths (mocked)
  - McpCatalogClient.run_health_check output shape (mocked HTTP)

No real DB or HTTP connections are made.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.metadata.mcp_server_service import (
    McpServer,
    McpServerService,
    NEED_LIST_TABLES,
    NEED_DESCRIBE_TABLE,
    NEED_LIST_RELATIONSHIPS,
    NEED_BUSINESS_GLOSSARY,
    NEED_KNOWLEDGE_PAIRS,
    NEED_LIST_SOURCES,
    NEED_SEARCH_COLUMN_VALUES,
    REQUIRED_NEEDS,
)
from src.metadata.mcp_cache_service import (
    McpCacheService,
    CacheResult,
    KEY_CATALOG,
    KEY_TABLES,
    KEY_COLUMNS,
    KEY_CONNECTIONS,
    SOURCE_GLOBAL,
    NO_CACHE_TTL,
)
from src.metadata.mcp_catalog_client import (
    McpCatalogClient,
    _fmt_tables,
    _fmt_columns,
    _fmt_relationships,
    _fmt_knowledge_pairs,
    _fmt_business_terms,
    _normalise_list,
    _flatten_columns,
    _normalise_connections,
    _parse_catalog_markdown,
    normalize_columns_markdown,
    build_date_column_index,
    restore_date_columns,
    restore_date_columns_from_index,
    _map_tool_to_need,
    _empty_bundle,
    _normalise_value_search,
    _value_search_arguments,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_server(health=None, is_active=True, transport="http", auth_type="none") -> McpServer:
    return McpServer(
        id=1,
        is_active=is_active,
        server_name="jeen-catalog-mcp",
        endpoint="https://mcp.jeen.internal/catalog",
        transport=transport,
        auth_type=auth_type,
        bearer_token=None,
        cache_ttl_seconds=900,
        health=health,
        last_checked_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _health_with_tools(tool_map: dict) -> dict:
    """Build a minimal health blob with the given need→tool_name mapping."""
    return {
        "status": "healthy",
        "latency_ms": 100,
        "ping_ms": 20,
        "protocol": "2025-06-18",
        "sdk": "mcp-python 1.9.2",
        "server_version": "1.0.0",
        "capabilities": ["tools"],
        "tools": [
            {"name": tool_name, "description": f"Tool for {need}", "need": need}
            for need, tool_name in tool_map.items()
        ],
        "checked_at": "2026-06-01T00:00:00Z",
    }


def _mock_pool(fetchrow_return=None, fetch_return=None, execute_return="UPDATE 1"):
    """Return a minimal asyncpg pool mock."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch    = AsyncMock(return_value=fetch_return or [])
    conn.execute  = AsyncMock(return_value=execute_return)
    conn.transaction = MagicMock(return_value=_async_ctx(conn))

    acquire_ctx = _async_ctx(conn)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


class _async_ctx:
    """Minimal async context manager that yields the given value."""
    def __init__(self, value):
        self._v = value
    async def __aenter__(self):
        return self._v
    async def __aexit__(self, *_):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# McpServer dataclass
# ═══════════════════════════════════════════════════════════════════════════════

class TestMcpServer:

    def test_get_tool_for_need_found(self):
        health = _health_with_tools({NEED_LIST_TABLES: "catalog.list_tables"})
        server = _make_server(health=health)
        assert server.get_tool_for_need(NEED_LIST_TABLES) == "catalog.list_tables"

    def test_get_tool_for_need_missing(self):
        health = _health_with_tools({NEED_LIST_TABLES: "catalog.list_tables"})
        server = _make_server(health=health)
        assert server.get_tool_for_need(NEED_DESCRIBE_TABLE) is None

    def test_get_tool_for_need_no_health(self):
        server = _make_server(health=None)
        assert server.get_tool_for_need(NEED_LIST_TABLES) is None

    def test_is_ready_true_when_required_needs_mapped(self):
        health = _health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES:  "get_catalog_prompt",
        })
        server = _make_server(health=health)
        assert server.is_ready is True

    def test_is_ready_false_when_missing_required_need(self):
        # Only list_connections mapped — get_catalog_prompt missing
        health = _health_with_tools({NEED_LIST_SOURCES: "list_connections"})
        server = _make_server(health=health)
        assert server.is_ready is False

    def test_is_ready_false_no_health(self):
        server = _make_server(health=None)
        assert server.is_ready is False

    def test_health_status_healthy(self):
        server = _make_server(health={"status": "healthy"})
        assert server.health_status == "healthy"

    def test_health_status_none_when_no_health(self):
        server = _make_server(health=None)
        assert server.health_status is None

    def test_to_dict_hides_token_by_default(self):
        server = _make_server()
        server = McpServer(
            **{**server.__dict__, "bearer_token": "secret-token"}
        )
        d = server.to_dict()
        assert "bearer_token" not in d
        assert d["has_token"] is True

    def test_to_dict_exposes_token_when_requested(self):
        server = _make_server()
        server = McpServer(**{**server.__dict__, "bearer_token": "secret-token"})
        d = server.to_dict(include_token=True)
        assert d["bearer_token"] == "secret-token"

    def test_required_needs_are_list_sources_and_list_tables(self):
        # list_connections + get_catalog_prompt are the two critical tools
        assert REQUIRED_NEEDS == {NEED_LIST_SOURCES, NEED_LIST_TABLES}


# ═══════════════════════════════════════════════════════════════════════════════
# McpServerService — catalog_source toggle
# ═══════════════════════════════════════════════════════════════════════════════

class TestMcpServerServiceCatalogSource:

    @pytest.mark.asyncio
    async def test_get_catalog_source_defaults_to_db(self):
        pool = _mock_pool(fetchrow_return=None)
        svc  = McpServerService(pool)
        assert await svc.get_catalog_source() == "db"

    @pytest.mark.asyncio
    async def test_get_catalog_source_returns_stored_value(self):
        row = MagicMock()
        row.__getitem__ = lambda s, k: "mcp"
        pool = _mock_pool(fetchrow_return=row)
        svc  = McpServerService(pool)
        assert await svc.get_catalog_source() == "mcp"

    @pytest.mark.asyncio
    async def test_set_catalog_source_valid_values(self):
        pool = _mock_pool()
        svc  = McpServerService(pool)
        # Should not raise
        await svc.set_catalog_source("db")
        await svc.set_catalog_source("mcp")

    @pytest.mark.asyncio
    async def test_set_catalog_source_invalid_raises(self):
        pool = _mock_pool()
        svc  = McpServerService(pool)
        with pytest.raises(ValueError, match="must be 'db' or 'mcp'"):
            await svc.set_catalog_source("invalid")


# ═══════════════════════════════════════════════════════════════════════════════
# McpServerService — server CRUD
# ═══════════════════════════════════════════════════════════════════════════════

def _server_row(server_name="jeen-catalog-mcp", is_active=True, health=None):
    """Simulate an asyncpg Record for insights_mcp_servers."""
    row = MagicMock()
    data = {
        "id": 1, "is_active": is_active,
        "server_name": server_name,
        "endpoint": "https://mcp.jeen.internal/catalog",
        "transport": "http", "auth_type": "none", "bearer_token": None,
        # Envelope-encryption columns (NULL when no bearer token is stored).
        "token_algo": None, "token_kek_id": None, "token_ciphertext": None,
        "token_nonce": None, "token_wrapped_dek": None, "token_dek_nonce": None,
        "cache_ttl_seconds": 900,
        "health": health, "last_checked_at": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    row.__getitem__ = lambda s, k: data[k]
    return row


class TestMcpServerServiceCrud:

    @pytest.mark.asyncio
    async def test_get_active_returns_none_when_no_row(self):
        pool = _mock_pool(fetchrow_return=None)
        svc  = McpServerService(pool)
        assert await svc.get_active() is None

    @pytest.mark.asyncio
    async def test_get_active_returns_server_when_row_exists(self):
        pool = _mock_pool(fetchrow_return=_server_row())
        svc  = McpServerService(pool)
        server = await svc.get_active()
        assert server is not None
        assert server.server_name == "jeen-catalog-mcp"
        assert server.is_active is True

    @pytest.mark.asyncio
    async def test_delete_returns_true_on_success(self):
        pool = _mock_pool(execute_return="DELETE 1")
        svc  = McpServerService(pool)
        assert await svc.delete(1) is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        pool = _mock_pool(execute_return="DELETE 0")
        svc  = McpServerService(pool)
        assert await svc.delete(99) is False

    @pytest.mark.asyncio
    async def test_save_health_persists_blob(self):
        row = _server_row(health={"status": "healthy", "tools": []})
        pool = _mock_pool(fetchrow_return=row)
        svc  = McpServerService(pool)
        result = await svc.save_health(1, {"status": "healthy", "tools": []})
        # Result correctly hydrated from the mocked row
        assert result is not None
        assert result.server_name == "jeen-catalog-mcp"

    @pytest.mark.asyncio
    async def test_clear_health_calls_execute(self):
        pool = _mock_pool()
        svc  = McpServerService(pool)
        # Should complete without error; health cleared
        await svc.clear_health(1)


# ═══════════════════════════════════════════════════════════════════════════════
# McpCacheService — L1 in-memory behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestMcpCacheServiceL1:

    @pytest.mark.asyncio
    async def test_no_cache_ttl_returns_none(self):
        pool  = _mock_pool()
        cache = McpCacheService(pool)
        result = await cache.get(1, "AdventureWorks", KEY_TABLES, NO_CACHE_TTL)
        assert result is None

    @pytest.mark.asyncio
    async def test_set_then_get_returns_payload_from_l1(self):
        pool  = _mock_pool()
        cache = McpCacheService(pool)
        payload = [{"table_name": "DimProduct"}]

        await cache.set(1, "AdventureWorks", KEY_TABLES, payload, ttl_seconds=900)
        result = await cache.get(1, "AdventureWorks", KEY_TABLES, ttl_seconds=900)

        assert result is not None
        assert result.source == "l1"
        assert result.payload == payload
        assert result.is_stale is False

    @pytest.mark.asyncio
    async def test_l1_miss_after_ttl_expiry(self):
        # Pool returns None from fetchrow → L2 is empty too
        pool  = _mock_pool(fetchrow_return=None)
        cache = McpCacheService(pool)
        payload = [{"table_name": "DimProduct"}]

        # Plant an already-expired L1 entry.
        l1_key = (1, "AdventureWorks", KEY_TABLES)
        result_obj = CacheResult(payload=payload, source="l1", is_stale=False)
        cache._l1[l1_key] = (time.monotonic() - 1, result_obj)  # already expired

        result = await cache.get(1, "AdventureWorks", KEY_TABLES, ttl_seconds=900)
        assert result is None  # L2 also empty

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("expired", "marked", "stale", "invalidated"),
        [
            (False, False, False, False),
            (True, False, True, False),   # past its TTL: may be served while refreshing
            (False, True, True, True),    # explicitly cleared: must be refetched
        ],
    )
    async def test_l2_row_says_whether_it_expired_or_was_invalidated(self, expired, marked, stale, invalidated):
        now = datetime.now(tz=timezone.utc)
        pool = _mock_pool(fetchrow_return={
            "payload": '"- FactSales"',
            "fetched_at": now - timedelta(hours=2),
            "expires_at": now - timedelta(minutes=1) if expired else now + timedelta(minutes=30),
            "is_stale": marked,
        })
        cache = McpCacheService(pool)

        result = await cache.get(1, "AdventureWorks", KEY_TABLES, ttl_seconds=3600)

        assert result.payload == "- FactSales"
        assert (result.is_stale, result.invalidated) == (stale, invalidated)
        assert ((1, "AdventureWorks", KEY_TABLES) in cache._l1) is not stale, "only a fresh row warms L1"

    @pytest.mark.asyncio
    async def test_write_from_before_an_invalidation_is_refused(self):
        cache = McpCacheService(_mock_pool())
        cache._upsert_db = AsyncMock()
        before = cache.generation(1, "AdventureWorks")

        await cache.invalidate(1, "AdventureWorks")
        stored = await cache.set(1, "AdventureWorks", KEY_CATALOG, {"tables": "- Old"}, 3600, generation=before)

        assert stored is False
        assert cache._l1 == {}
        cache._upsert_db.assert_not_awaited()
        other = cache.generation(1, "Trinity")
        assert await cache.set(1, "Trinity", KEY_CATALOG, {"tables": "- T"}, 3600, generation=other) is True

    @pytest.mark.asyncio
    async def test_invalidation_during_the_l2_write_waits_for_it_then_marks_it(self):
        """If the invalidation's UPDATE could run before the upsert commits, the
        outdated copy would stay fresh in L2. It waits for the write instead,
        and the write's L1 entry is ignored as soon as the generation moves."""
        pool = _mock_pool()
        cache = McpCacheService(pool)
        events = []
        conn = pool.acquire.return_value._v
        conn.execute = AsyncMock(side_effect=lambda *_a: events.append("marked stale") or "UPDATE 1")
        before = cache.generation(1, "AdventureWorks")
        seen_mid_write = []
        invalidation = None

        async def _upsert(*_args):
            nonlocal invalidation
            events.append("upsert started")
            invalidation = asyncio.create_task(cache.invalidate(1, "AdventureWorks"))
            for _ in range(3):
                await asyncio.sleep(0)
            seen_mid_write.append(await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600))
            events.append("upsert committed")

        cache._upsert_db = _upsert

        await cache.set(1, "AdventureWorks", KEY_CATALOG, {"tables": "- Old"}, 3600, generation=before)
        await invalidation

        assert events == ["upsert started", "upsert committed", "marked stale"]
        assert seen_mid_write == [None], "the pre-invalidation L1 entry is not served while the write finishes"
        assert await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600) is None

    @pytest.mark.asyncio
    async def test_l2_read_racing_an_invalidation_is_not_warmed_into_l1(self):
        """The SELECT can see the row before the invalidation's UPDATE lands."""
        now = datetime.now(tz=timezone.utc)
        fresh_row = {"payload": {"tables": "- Old"}, "fetched_at": now, "expires_at": now + timedelta(minutes=30), "is_stale": False}
        pool = _mock_pool()
        cache = McpCacheService(pool)
        conn = pool.acquire.return_value._v

        async def _fetchrow(*_args):
            await cache.invalidate(1, "AdventureWorks")
            return fresh_row

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        result = await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600)

        assert result.invalidated and result.is_stale
        assert (1, "AdventureWorks", KEY_CATALOG) not in cache._l1

    @pytest.mark.asyncio
    async def test_read_while_an_invalidation_waits_for_its_update_is_not_served_fresh(self):
        """After a write commits and before the invalidation's UPDATE runs, the
        L2 row still looks fresh; a read in that window must not trust it."""
        now = datetime.now(tz=timezone.utc)
        pool = _mock_pool(fetchrow_return={
            "payload": {"tables": "- Old"}, "fetched_at": now,
            "expires_at": now + timedelta(minutes=30), "is_stale": False,
        })
        cache = McpCacheService(pool)
        conn = pool.acquire.return_value._v
        update_gate = asyncio.Event()

        async def _execute(*_args):
            await update_gate.wait()
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)
        invalidation = asyncio.create_task(cache.invalidate(1, "AdventureWorks"))
        for _ in range(3):
            await asyncio.sleep(0)
        assert not invalidation.done()

        result = await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600)
        status = await cache.get_status(1, "AdventureWorks")

        assert result.invalidated and result.is_stale
        assert (1, "AdventureWorks", KEY_CATALOG) not in cache._l1
        assert status["cache_hit"] is False
        update_gate.set()
        await invalidation
        assert cache._pending == {}, "the pending marker is cleared once the UPDATE ran"

    @pytest.mark.asyncio
    async def test_failed_invalidation_keeps_distrusting_the_rows_it_could_not_mark(self):
        """The UPDATE failed, so L2 still says fresh: that must not bring the
        pre-invalidation copy back once the invalidation is over."""
        now = datetime.now(tz=timezone.utc)
        pool = _mock_pool(fetchrow_return={
            "payload": {"tables": "- Old"}, "fetched_at": now,
            "expires_at": now + timedelta(minutes=30), "is_stale": False,
        })
        conn = pool.acquire.return_value._v
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
        cache = McpCacheService(pool)

        with pytest.raises(RuntimeError):
            await cache.invalidate(1, "AdventureWorks")

        assert cache._pending == {}
        result = await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600)
        assert result.invalidated, "the unmarked row is not served as fresh"
        assert (1, "AdventureWorks", KEY_CATALOG) not in cache._l1
        assert (await cache.get(1, "Trinity", KEY_CATALOG, 3600)).invalidated is False, "other sources are unaffected"

        # A fetch after the failure still caches in L1 and is served from it.
        await cache.set(1, "AdventureWorks", KEY_CATALOG, {"tables": "- New"}, 3600,
                        generation=cache.generation(1, "AdventureWorks"))
        assert (await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600)).payload == {"tables": "- New"}

        # Once an invalidation of the scope succeeds, L2 is trusted again.
        conn.execute = AsyncMock(return_value="UPDATE 1")
        await cache.invalidate(1, "AdventureWorks")
        assert cache._unmarked == set()
        assert (await cache.get(1, "AdventureWorks", KEY_CATALOG, 3600)).invalidated is False

    @pytest.mark.asyncio
    async def test_startup_warm_skips_the_old_per_section_rows(self):
        now = datetime.now(tz=timezone.utc)

        def row(source_key, cache_key):
            return {"source_key": source_key, "cache_key": cache_key, "payload": '"x"',
                    "fetched_at": now, "expires_at": now + timedelta(minutes=30)}

        pool = _mock_pool(fetch_return=[
            row("AdventureWorks", KEY_CATALOG),
            row(SOURCE_GLOBAL, KEY_CONNECTIONS),
            row("AdventureWorks", KEY_TABLES),
            row("AdventureWorks", KEY_COLUMNS),
            row("AdventureWorks", KEY_CONNECTIONS),
            row("AdventureWorks", "tables_rich"),
        ])
        cache = McpCacheService(pool)

        assert await cache.warm_from_db(1) == 3
        assert set(cache._l1) == {
            (1, "AdventureWorks", KEY_CATALOG), (1, SOURCE_GLOBAL, KEY_CONNECTIONS), (1, "AdventureWorks", "tables_rich"),
        }

    @pytest.mark.asyncio
    async def test_server_wide_invalidation_changes_every_source_generation(self):
        cache = McpCacheService(_mock_pool())
        before = {source: cache.generation(1, source) for source in ("AdventureWorks", "Trinity")}

        await cache.invalidate(1)

        assert all(cache.generation(1, source) != gen for source, gen in before.items())
        assert cache.generation(2, "AdventureWorks") == (0, 0), "other servers are untouched"

    @pytest.mark.asyncio
    async def test_invalidate_clears_l1_for_source(self):
        pool  = _mock_pool()
        cache = McpCacheService(pool)
        payload = [{"table_name": "DimProduct"}]

        await cache.set(1, "AdventureWorks", KEY_TABLES, payload, ttl_seconds=900)
        await cache.set(1, "Trinity",        KEY_TABLES, payload, ttl_seconds=900)

        await cache.invalidate(1, source_key="AdventureWorks")

        # AdventureWorks and global connections should be gone from L1
        assert (1, "AdventureWorks", KEY_TABLES) not in cache._l1
        # Trinity should be untouched
        assert (1, "Trinity", KEY_TABLES) in cache._l1

    @pytest.mark.asyncio
    async def test_invalidate_all_clears_entire_l1(self):
        pool  = _mock_pool()
        cache = McpCacheService(pool)
        payload = [{"x": 1}]

        await cache.set(1, "AdventureWorks", KEY_TABLES,   payload, 900)
        await cache.set(1, "Trinity",        KEY_COLUMNS,  payload, 900)

        await cache.invalidate(1)  # source_key=None → all

        assert len([k for k in cache._l1 if k[0] == 1]) == 0

    @pytest.mark.asyncio
    async def test_no_cache_ttl_set_is_noop(self):
        pool  = _mock_pool()
        cache = McpCacheService(pool)

        await cache.set(1, "AdventureWorks", KEY_TABLES, [{"x": 1}], NO_CACHE_TTL)
        assert (1, "AdventureWorks", KEY_TABLES) not in cache._l1


# ═══════════════════════════════════════════════════════════════════════════════
# McpCatalogClient — formatters
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatters:

    def test_fmt_tables_normal(self):
        rows = [
            {"table_name": "DimProduct", "table_description": "Product dimension"},
            {"table_name": "FactSales",  "table_description": ""},
        ]
        out = _fmt_tables(rows)
        assert "- DimProduct - Product dimension" in out
        assert "- FactSales" in out

    def test_fmt_tables_empty(self):
        assert _fmt_tables([]) == "No tables registered."

    def test_fmt_columns_includes_type_and_pk(self):
        rows = [{"table_name": "DimProduct", "column_name": "ProductKey",
                 "data_type": "integer", "description": "PK",
                 "is_primary_key": True, "is_nullable": False}]
        out = _fmt_columns(rows)
        assert "DimProduct.ProductKey" in out
        assert "Type: integer" in out
        assert "PK: true" in out
        assert "NOT NULL" in out

    def test_fmt_columns_empty(self):
        assert _fmt_columns([]) == "No columns registered."

    def test_flatten_columns_unwraps_the_list_columns_envelope_and_scopes_to_the_table(self):
        # The schema-modeler tool answers with one envelope and ignores `table`.
        envelope = [{
            "connection_id": 6, "count": 3, "table_name": None,
            "tables": [{"table_name": '"public"."dimcustomer"'}],
            "columns": [
                {"table_name": '"public"."dimcustomer"', "column_name": "customerkey", "data_type": "integer", "is_primary_key": True},
                {"table_name": '"public"."dimcustomer"', "column_name": "firstname", "data_type": "character varying"},
                {"table_name": '"public"."dimdate"', "column_name": "datekey", "data_type": "integer"},
            ],
        }]
        everything = _flatten_columns(envelope, None)
        assert [c["column"] for c in everything] == ["customerkey", "firstname", "datekey"]
        assert everything[0]["table"] == '"public"."dimcustomer"' and everything[0]["data_type"] == "integer"

        # Any spelling of the table scopes: quoted, schema-qualified or bare.
        for spelling in ('"public"."dimcustomer"', "public.dimcustomer", "DimCustomer"):
            scoped = _flatten_columns(envelope, spelling)
            assert [c["column"] for c in scoped] == ["customerkey", "firstname"], spelling

        # Flat records from other servers pass through with the same field names.
        flat = _flatten_columns([{"table": "dimdate", "column": "datekey", "data_type": "integer"}], "dimdate")
        assert flat == [{"table": "dimdate", "column": "datekey", "data_type": "integer"}]

    def test_fmt_relationships_list_literal(self):
        rows = [{"relation": "FactSales.ProductKey → DimProduct.ProductKey"}]
        out = _fmt_relationships(rows)
        assert out.startswith("[")
        assert "FactSales.ProductKey" in out

    def test_fmt_relationships_empty(self):
        assert _fmt_relationships([]) == "No relationships registered."

    def test_fmt_knowledge_pairs(self):
        rows = [{"category": "Sales", "question": "Total sales?",
                 "sql_statement": "SELECT SUM(amount) FROM FactSales", "tags": "sales"}]
        out = _fmt_knowledge_pairs(rows)
        assert "Category: Sales" in out
        assert "Total sales?" in out

    def test_fmt_business_terms(self):
        rows = [{"term": "ARR", "definition": "Annual Recurring Revenue", "category": "Finance"}]
        out = _fmt_business_terms(rows)
        assert "Term: ARR" in out
        assert "Annual Recurring Revenue" in out

    def test_empty_bundle_has_all_keys(self):
        bundle = _empty_bundle()
        expected_keys = {"tables", "columns", "relationships", "sources",
                         "knowledge_pairs", "business_terms",
                         "column_statistics", "column_samples"}
        assert set(bundle.keys()) == expected_keys


# ═══════════════════════════════════════════════════════════════════════════════
# McpCatalogClient — _normalise_list
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormaliseList:

    def test_list_passthrough(self):
        assert _normalise_list([1, 2, 3]) == [1, 2, 3]

    def test_dict_with_items_key(self):
        assert _normalise_list({"items": [1, 2]}) == [1, 2]

    def test_dict_with_data_key(self):
        assert _normalise_list({"data": [{"a": 1}]}) == [{"a": 1}]

    def test_dict_without_list_key_wrapped(self):
        assert _normalise_list({"x": 1}) == [{"x": 1}]

    def test_json_string(self):
        assert _normalise_list('[{"name": "t1"}]') == [{"name": "t1"}]

    def test_empty_list(self):
        assert _normalise_list([]) == []

    def test_none_returns_empty(self):
        assert _normalise_list(None) == []


# ═══════════════════════════════════════════════════════════════════════════════
# McpCatalogClient — _map_tool_to_need heuristics
# ═══════════════════════════════════════════════════════════════════════════════

class TestMapToolToNeed:

    @pytest.mark.parametrize("tool_name, expected_need", [
        # Actual jeen-metadata-provider tool names
        ("list_connections",    NEED_LIST_SOURCES),
        ("get_catalog_prompt",  NEED_LIST_TABLES),
        ("get_filtered_prompt", NEED_DESCRIBE_TABLE),
        # Generic fallback keywords
        ("catalog.list_tables",  NEED_LIST_TABLES),
        ("catalog.relationships",NEED_LIST_RELATIONSHIPS),
        ("catalog.glossary",     NEED_BUSINESS_GLOSSARY),
        ("knowledge_pairs_tool", NEED_KNOWLEDGE_PAIRS),
        ("search_column_values", NEED_SEARCH_COLUMN_VALUES),
    ])
    def test_known_tool_names(self, tool_name, expected_need):
        assert _map_tool_to_need(tool_name) == expected_need

    def test_unmapped_tool_returns_none(self):
        assert _map_tool_to_need("analytics.run_query") is None
        assert _map_tool_to_need("ping") is None


class TestValueSearchNormalisation:
    def test_uses_discovered_camel_case_arguments(self):
        descriptor = {
            "input_schema": {
                "properties": {
                    "connectionId": {},
                    "tableName": {},
                    "columnName": {},
                    "searchTerm": {},
                    "maxResults": {},
                }
            }
        }
        assert _value_search_arguments(
            descriptor,
            connection_id=7,
            table="Product",
            column="Name",
            query="mountaiin",
            limit=500,
        ) == {
            "connectionId": 7,
            "tableName": "Product",
            "columnName": "Name",
            "searchTerm": "mountaiin",
            "maxResults": 100,
        }

    def test_normalises_values_and_never_assumes_complete(self):
        result = _normalise_value_search(
            {
                "matches": [
                    {"canonical_value": "Mountain-300"},
                    {"value": "Mountain-300"},
                    {"label": "Mountain-500"},
                ]
            }
        )
        assert result["values"] == ["Mountain-300", "Mountain-500"]
        assert result["complete"] is False
        assert result["source"] == "mcp"
        # Every match keeps its own provenance for reverse lookups.
        assert [m["value"] for m in result["matches"]] == ["Mountain-300", "Mountain-300", "Mountain-500"]

    def test_reverse_lookup_matches_carry_their_column(self):
        result = _normalise_value_search(
            {"matches": [
                {"value": "Moscow", "table": "dim_customer", "column": "city", "score": 0.62, "count": 12},
                {"value": "Moscow", "table_name": "dim_dealer", "column_name": "city", "similarity": 0.62},
            ], "complete": False, "snapshot": "2026-09-01"}
        )
        assert result["values"] == ["Moscow"]
        assert result["snapshot"] == "2026-09-01"
        assert [(m["table"], m["column"], m["score"]) for m in result["matches"]] == [
            ("dim_customer", "city", 0.62), ("dim_dealer", "city", 0.62),
        ]

    def test_honours_explicit_completeness(self):
        assert _normalise_value_search(
            {"values": ["APAC"], "truncated": False}
        )["complete"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# _normalise_connections
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormaliseConnections:

    def test_standard_server_response(self):
        raw = {
            "connections": [
                {"connection_id": 6,  "name": "AdventureWorksDW", "service_type": "Postgres",
                 "description": "AW DW", "owner": None, "ai_domain_context": "DW context"},
                {"connection_id": 34, "name": "doc",              "service_type": "Postgres",
                 "description": None,   "owner": None, "ai_domain_context": None},
            ]
        }
        items = _normalise_connections(raw)
        assert len(items) == 2
        assert items[0]["source_key"]    == "AdventureWorksDW"
        assert items[0]["connection_id"] == 6
        assert items[0]["database_type"] == "postgres"
        assert items[0]["is_active"]     is True
        assert items[1]["description"]   is None

    def test_empty_connections(self):
        assert _normalise_connections({"connections": []}) == []

    def test_bare_list_passthrough(self):
        raw = [{"connection_id": 1, "name": "db1", "service_type": "Postgres"}]
        items = _normalise_connections(raw)
        assert items[0]["source_key"] == "db1"

    def test_unexpected_type_returns_empty(self):
        assert _normalise_connections("unexpected") == []
        assert _normalise_connections(None)          == []


# ═══════════════════════════════════════════════════════════════════════════════
# _parse_catalog_markdown
# ═══════════════════════════════════════════════════════════════════════════════

_SAMPLE_MARKDOWN = """
# Database Schema

## Domain Context
AdventureWorksDW is a data warehouse.

## SQL Dialect
PostgreSQL — use :: for casting.

## Knowledge Pairs
Q: Total sales?
SQL:
SELECT SUM(salesamount) FROM factinternetsales

## Business Terms
- ARR: Annual Recurring Revenue

## Tables
- dimproduct — Product dimension
- factinternetsales — Internet sales facts

## Columns
dimproduct.productkey — Type: integer

## Relationships (Foreign Keys)
- factinternetsales.productkey → dimproduct.productkey

## Source
Name: AdventureWorksDW
Type: Postgres
"""


class TestParseCatalogMarkdown:

    def test_all_keys_present(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        assert set(sections.keys()) == {
            "tables", "columns", "relationships",
            "sources", "knowledge_pairs", "business_terms",
            "column_statistics", "column_samples",
        }

    def test_tables_section_extracted(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        assert "dimproduct" in sections["tables"]
        assert "factinternetsales" in sections["tables"]

    def test_columns_section_extracted(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        assert "dimproduct.productkey" in sections["columns"]

    def test_relationships_section_extracted(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        assert "factinternetsales.productkey" in sections["relationships"]


# The schema-modeler MCP groups columns under a table header. Downstream
# parsers (ML planner, filter grounder, SQL column allowlist) expect one flat
# typed line per column, so the adapter rewrites the grouped shape.
_GROUPED_COLUMNS = """Column definitions grouped by table (includes data type, PK flag, and description):

"public"."dimdate": Contains date dimension data for time-based analysis.
  - "calendaryear" (smallint) [distinct_count=6, distinct_is_approximate=false]
  - "datekey" (integer) [PK] [distinct_count=2191, distinct_is_approximate=false]: PK - surrogate
  - "fulldatealternatekey" (date) [NOT NULL]

"public"."factinternetsales": Internet sales facts.
  - "orderdate" (timestamp without time zone) [distinct_count=1124, distinct_is_approximate=false]
  - "salesamount" (numeric(19,4)) [distinct_count=130, distinct_is_approximate=false]: Sales amount in USD
  - "salesordernumber" (character varying) [distinct_count=27659, distinct_is_approximate=false]
"""


class TestNormalizeColumnsMarkdown:

    def test_grouped_shape_becomes_flat_typed_lines(self):
        text = normalize_columns_markdown(_GROUPED_COLUMNS)
        lines = text.splitlines()
        assert lines[0] == '- "public"."dimdate"."calendaryear" - Type: smallint, Distinct: 6'
        assert lines[1] == '- "public"."dimdate"."datekey" - Type: integer, PK: true, Distinct: 2191, Description: PK - surrogate'
        assert lines[2] == '- "public"."dimdate"."fulldatealternatekey" - Type: date, NOT NULL'
        assert '- "public"."factinternetsales"."salesamount" - Type: numeric(19,4), Distinct: 130, Description: Sales amount in USD' in lines
        # The intro sentence is prose, not a column.
        assert not any("Column definitions" in line for line in lines)

    def test_planner_and_grounder_see_dates_and_measures(self):
        from src.agent.analysis_planner import catalog_candidates
        from src.agent.langgraph_agent.nodes.filtering import column_types

        cands = catalog_candidates(normalize_columns_markdown(_GROUPED_COLUMNS))
        fis = cands["factinternetsales"]
        assert fis.schema == "public"
        assert fis.date_columns == ["orderdate"]
        assert fis.numeric_columns == ["salesamount"]
        assert fis.text_columns == ["salesordernumber"]
        assert cands["dimdate"].date_columns == ["fulldatealternatekey"]
        assert cands["dimdate"].numeric_columns == ["calendaryear", "datekey"]
        # Passed through raw, the same text yields no usable table at all.
        raw = catalog_candidates(_GROUPED_COLUMNS)
        assert "factinternetsales" not in raw
        types = column_types(normalize_columns_markdown(_GROUPED_COLUMNS))
        assert types[("factinternetsales", "orderdate")] == "timestamp without time zone"

    def test_flat_db_shape_is_untouched(self):
        flat = "- factinternetsales.orderdate - Type: timestamp, Description: Order date\n- dimproduct.productkey - Type: integer, PK: true"
        assert normalize_columns_markdown(flat) == flat
        assert normalize_columns_markdown("") == ""

    def test_idempotent(self):
        once = normalize_columns_markdown(_GROUPED_COLUMNS)
        assert normalize_columns_markdown(once) == once

    def test_parse_catalog_markdown_normalises_columns(self):
        md = "## Tables\n- \"public\".\"factinternetsales\": facts\n\n## Columns\n" + _GROUPED_COLUMNS
        sections = _parse_catalog_markdown(md)
        assert '"public"."factinternetsales"."orderdate" - Type: timestamp without time zone' in sections["columns"]


class TestRestoreDateColumns:
    FULL = "\n".join([
        '- "public"."factinternetsales"."orderdatekey" - Type: integer',
        '- "public"."factinternetsales"."orderdate" - Type: timestamp without time zone',
        '- "public"."factinternetsales"."shipdate" - Type: timestamp without time zone',
        '- "public"."factinternetsales"."salesamount" - Type: money',
        '- "public"."factinternetsales"."unitprice" - Type: money',
        '- "public"."dimcustomer"."customerkey" - Type: integer, PK: true',
        '- "public"."dimcustomer"."datefirstpurchase" - Type: date',
        '- "public"."dimproduct"."productkey" - Type: integer, PK: true',
        '- "public"."dimproduct"."startdate" - Type: timestamp without time zone',
    ])

    def test_adds_only_the_date_columns_of_tables_the_filter_kept(self):
        filtered = "\n".join([
            '- "public"."factinternetsales"."orderdatekey" - Type: integer',
            '- "public"."factinternetsales"."salesamount" - Type: money',
            '- "public"."dimcustomer"."customerkey" - Type: integer, PK: true',
        ])
        out = restore_date_columns(filtered, self.FULL).splitlines()
        assert out[:3] == filtered.splitlines()
        assert '- "public"."factinternetsales"."orderdate" - Type: timestamp without time zone' in out
        assert '- "public"."factinternetsales"."shipdate" - Type: timestamp without time zone' in out
        assert '- "public"."dimcustomer"."datefirstpurchase" - Type: date' in out
        # Measures the filter dropped stay dropped; tables it did not pick stay out.
        assert not any("unitprice" in line for line in out)
        assert not any("dimproduct" in line for line in out)

    def test_noop_when_a_date_column_is_already_present_or_inputs_are_empty(self):
        filtered = '- "public"."factinternetsales"."orderdate" - Type: timestamp without time zone\n- "public"."factinternetsales"."salesamount" - Type: money'
        assert restore_date_columns(filtered, self.FULL) == filtered
        assert restore_date_columns("", self.FULL) == ""
        assert restore_date_columns(filtered, "") == filtered
        # A bare "time" column is not a time axis.
        assert restore_date_columns('- t.k - Type: integer', '- t.opens_at - Type: time') == '- t.k - Type: integer'

    def test_idempotent(self):
        filtered = '- "public"."factinternetsales"."salesamount" - Type: money'
        once = restore_date_columns(filtered, self.FULL)
        assert restore_date_columns(once, self.FULL) == once


class TestParseCatalogMarkdownSections:
    """The remaining sections of the sample prompt (continues TestParseCatalogMarkdown)."""

    def test_knowledge_pairs_extracted(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        assert "Total sales" in sections["knowledge_pairs"]

    def test_business_terms_extracted(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        assert "ARR" in sections["business_terms"]

    def test_sources_combines_domain_context_and_source(self):
        sections = _parse_catalog_markdown(_SAMPLE_MARKDOWN)
        # Both ## Domain Context and ## Source go into sources
        assert "AdventureWorksDW" in sections["sources"]

    def test_statistics_and_samples_are_preserved(self):
        sections = _parse_catalog_markdown(
            "## Column Statistics\n- orders.total - min: 1, max: 100\n"
            "## Sample Values\n- orders.status - paid, pending\n"
        )
        assert sections["column_statistics"] == "- orders.total - min: 1, max: 100"
        assert sections["column_samples"] == "- orders.status - paid, pending"

    def test_empty_string_returns_all_empty_keys(self):
        sections = _parse_catalog_markdown("")
        for v in sections.values():
            assert v == ""

    def test_missing_section_returns_empty_string(self):
        # Markdown with only Tables section
        md = "## Tables\n- orders\n"
        sections = _parse_catalog_markdown(md)
        assert "orders" in sections["tables"]
        assert sections["columns"]         == ""
        assert sections["relationships"]   == ""
        assert sections["business_terms"]  == ""
        assert sections["knowledge_pairs"] == ""


# ═══════════════════════════════════════════════════════════════════════════════
# McpCatalogClient — load_all (cache hit path)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMcpCatalogClientLoadAll:

    def _make_client(self, server, cached_payloads: dict | None = None):
        """
        Build a McpCatalogClient with a real McpCacheService using a mocked pool.

        cached_payloads: the full-catalog bundle (section → text) to pre-seed
        into L1 as the single ("AdventureWorks", KEY_CATALOG) entry.
        """
        srv_svc = AsyncMock()
        srv_svc.get_active = AsyncMock(return_value=server)

        pool  = _mock_pool()
        cache = McpCacheService(pool)

        if cached_payloads:
            result = CacheResult(
                payload=dict(cached_payloads), source="l1",
                fetched_at=datetime.now(tz=timezone.utc), is_stale=False
            )
            cache._l1[(server.id, "AdventureWorks", KEY_CATALOG)] = (time.monotonic() + 900, result)

        return McpCatalogClient(srv_svc, cache)

    @pytest.mark.asyncio
    async def test_load_all_returns_empty_bundle_when_no_active_server(self):
        srv_svc = AsyncMock()
        srv_svc.get_active = AsyncMock(return_value=None)
        cache   = McpCacheService(_mock_pool())
        client  = McpCatalogClient(srv_svc, cache)

        bundle = await client.load_all("AdventureWorks")
        assert bundle == _empty_bundle()

    @pytest.mark.asyncio
    async def test_load_all_serves_from_l1_cache(self):
        # With new design, catalog sections are pre-formatted strings (not row lists)
        health = _health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES:  "get_catalog_prompt",
        })
        server = _make_server(health=health)

        # Cache pre-formatted strings (as _ensure_catalog would store them)
        client = self._make_client(server, cached_payloads={
            "tables":          "- DimProduct - Product dim",
            "columns":         "- DimProduct.ProductKey, Type: integer, PK: true",
            "relationships":   "[(factinternetsales.productkey → dimproduct.productkey,)]",
            "business_terms":  "- Term: ARR | Definition: Annual Recurring Revenue",
            "knowledge_pairs": "Q: Total sales? SQL: SELECT SUM(salesamount) FROM factinternetsales",
            "sources":         "AdventureWorks | postgres | (Active: True)",
        })

        bundle = await client.load_all("AdventureWorks")

        assert "DimProduct" in bundle["tables"]
        assert "DimProduct.ProductKey" in bundle["columns"]
        assert "AdventureWorks" in bundle["sources"]
        assert "No tables registered." not in bundle["tables"]
        assert bundle["column_samples"] == "", "a section the cached bundle lacks gets its default"
        bundle["tables"] = "mutated"
        assert (await client.load_all("AdventureWorks"))["tables"] != "mutated", "callers get their own copy"

    @pytest.mark.asyncio
    async def test_load_all_returns_empty_bundle_when_server_has_no_health(self):
        server = _make_server(health=None)
        client = self._make_client(server)

        with patch.object(
            McpCatalogClient, "_jsonrpc",
            new_callable=AsyncMock,
            side_effect=Exception("unreachable"),
        ):
            bundle = await client.load_all("AdventureWorks")

        # All keys present, all fallback to empty strings
        for key in _empty_bundle():
            assert key in bundle

    @pytest.mark.asyncio
    async def test_load_filtered_calls_question_aware_prompt_tool(self):
        health = _health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
            NEED_DESCRIBE_TABLE: "get_filtered_prompt",
        })
        server = _make_server(health=health)
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        client._call_tool = AsyncMock(return_value={
            "prompt": (
                "## Tables\n- DimDate\n"
                "## Columns\n- DimDate.DateKey - Type: integer\n"
                "## Source\nAdventureWorks | postgres"
            ),
            "meta": {"filtered": True},
        })

        bundle = await client.load_filtered(
            "AdventureWorks", "sales by month"
        )

        # The question-aware tool shapes the bundle; the full catalog is read
        # (from cache when warm) only to restore dropped date columns.
        client._call_tool.assert_any_await(
            server,
            "get_filtered_prompt",
            {
                "connection_id": 42,
                "question": "sales by month",
            },
        )
        assert "DimDate" in bundle["tables"]
        assert "DimDate.DateKey" in bundle["columns"]
        assert "AdventureWorks" in bundle["sources"]

    @pytest.mark.asyncio
    async def test_filtered_prompt_stays_dynamic_while_full_catalog_becomes_warm(self):
        health = _health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
            NEED_DESCRIBE_TABLE: "get_filtered_prompt",
        })
        server = _make_server(health=health)
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        full_started = asyncio.Event()
        filtered_started = asyncio.Event()

        # Each call answers only once the other one is under way, so if the full
        # catalog still loaded after the filtered call, this would time out.
        async def _tool(_server, tool, args):
            if tool == "get_filtered_prompt":
                filtered_started.set()
                await asyncio.wait_for(full_started.wait(), timeout=2)
                question = args["question"]
                return {"prompt": (
                    f"## Tables\n- FactSales\n## Columns\n- FactSales.Amount - Type: money\n"
                    f"## Business Terms\n- {question}\n## Source\nAdventureWorks"
                )}
            full_started.set()
            await asyncio.wait_for(filtered_started.wait(), timeout=2)
            return (
                "## Tables\n- FactSales\n"
                "## Columns\n- FactSales.Amount - Type: money\n- FactSales.OrderDate - Type: date\n"
                "## Source\nAdventureWorks"
            )

        client._call_tool = AsyncMock(side_effect=_tool)
        first, first_timing = await client.load_filtered_with_meta("AdventureWorks", "sales by month")
        second, second_timing = await client.load_filtered_with_meta("AdventureWorks", "sales by region")

        filtered_calls = [
            call for call in client._call_tool.await_args_list
            if call.args[1] == "get_filtered_prompt"
        ]
        full_calls = [
            call for call in client._call_tool.await_args_list
            if call.args[1] == "get_catalog_prompt"
        ]
        assert len(filtered_calls) == 2, "each question must receive its own filtered prompt"
        assert len(full_calls) == 1, "the reusable full catalog should be warm after the first query"
        assert "sales by month" in first["business_terms"]
        assert "sales by region" in second["business_terms"]
        assert set(first_timing) == {
            "connection_ms", "filtered_tool_ms", "parse_ms", "full_restore_ms", "full_restore_cache", "total_ms",
        }
        assert first_timing["full_restore_cache"] == "miss"
        assert second_timing["full_restore_cache"] == "hit"
        # The restore still added the date column the filtered prompt dropped.
        assert "FactSales.OrderDate" in first["columns"]
        assert "FactSales.OrderDate" in second["columns"]

    @pytest.mark.asyncio
    async def test_load_filtered_restores_only_the_kept_tables_date_columns(self):
        """The filtered prompt kept a fact table's keys and measure but dropped its
        timestamp; the (cached) full catalog puts the date back and nothing else."""
        health = _health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
            NEED_DESCRIBE_TABLE: "get_filtered_prompt",
        })
        server = _make_server(health=health)
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        client._call_tool = AsyncMock(return_value={"prompt": (
            "## Tables\n- FactInternetSales\n"
            "## Columns\n- FactInternetSales.OrderDateKey - Type: integer\n- FactInternetSales.SalesAmount - Type: money\n"
            "## Source\nAdventureWorks | postgres"
        )})
        client._load_full = AsyncMock(return_value=({**_empty_bundle(), "columns": (
            "- FactInternetSales.OrderDateKey - Type: integer\n"
            "- FactInternetSales.OrderDate - Type: timestamp\n"
            "- FactInternetSales.UnitPrice - Type: money\n"
            "- DimCustomer.DateFirstPurchase - Type: date\n"
        )}, "hit"))

        bundle, timing = await client.load_filtered_with_meta("AdventureWorks", "what drove the change last quarter")

        lines = bundle["columns"].splitlines()
        assert "- FactInternetSales.OrderDate - Type: timestamp" in lines
        assert not any("UnitPrice" in line for line in lines), "measures the filter dropped stay dropped"
        assert not any("DimCustomer" in line for line in lines), "tables the filter did not pick stay out"
        client._load_full.assert_awaited_once_with(server, "AdventureWorks")
        assert timing["full_restore_cache"] == "hit"

        # A full-catalog failure leaves the filtered bundle as it came.
        client._load_full = AsyncMock(side_effect=RuntimeError("mcp down"))
        bundle = await client.load_filtered("AdventureWorks", "what drove the change last quarter")
        assert bundle["columns"].splitlines() == [
            "- FactInternetSales.OrderDateKey - Type: integer", "- FactInternetSales.SalesAmount - Type: money",
        ]

    @pytest.mark.asyncio
    async def test_full_restore_timing_includes_date_column_merge_work(self):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
            NEED_DESCRIBE_TABLE: "get_filtered_prompt",
        }))
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        client._call_tool = AsyncMock(return_value={"prompt": (
            "## Tables\n- FactSales\n"
            "## Columns\n- FactSales.Amount - Type: money\n"
            "## Source\nAdventureWorks"
        )})
        client._load_full = AsyncMock(return_value=({
            **_empty_bundle(),
            "columns": "- FactSales.OrderDate - Type: date",
        }, "hit"))

        def _slow_restore(filtered, index):
            time.sleep(0.02)
            return restore_date_columns_from_index(filtered, index)

        with patch(
            "src.metadata.mcp_catalog_client.restore_date_columns_from_index",
            side_effect=_slow_restore,
        ):
            _bundle, timing = await client.load_filtered_with_meta(
                "AdventureWorks", "sales by month"
            )

        assert timing["full_restore_ms"] >= 15

    @pytest.mark.asyncio
    async def test_concurrent_full_catalog_misses_share_one_provider_call(self):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
        }))
        client = self._make_client(server)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        populated = {**_empty_bundle(), "tables": "- FactSales"}

        async def _ensure(_server, _source_key, _generation=None):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return populated

        client._ensure_catalog = _ensure
        first = asyncio.create_task(client.load_all("AdventureWorks"))
        await started.wait()
        second = asyncio.create_task(client.load_all("AdventureWorks"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert calls == 1
        assert results == [populated, populated]
        assert results[0] is not results[1], "each waiter gets its own copy of the shared result"

    @pytest.mark.asyncio
    async def test_cancelling_one_coalesced_waiter_does_not_cancel_the_other(self):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
        }))
        client = self._make_client(server)
        started = asyncio.Event()
        release = asyncio.Event()
        populated = {**_empty_bundle(), "tables": "- FactSales"}

        async def _ensure(_server, _source_key, _generation=None):
            started.set()
            await release.wait()
            return populated

        client._ensure_catalog = _ensure
        cancelled = asyncio.create_task(client.load_all("AdventureWorks"))
        await started.wait()
        survivor = asyncio.create_task(client.load_all("AdventureWorks"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        assert await survivor == populated
        assert client._catalog_inflight == {}

    @pytest.mark.asyncio
    async def test_cancelled_only_waiter_does_not_leak_inflight_entry(self):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
        }))
        client = self._make_client(server)
        started = asyncio.Event()
        release = asyncio.Event()

        async def _ensure(_server, _source_key, _generation=None):
            started.set()
            await release.wait()

        client._ensure_catalog = _ensure
        waiter = asyncio.create_task(client.load_all("AdventureWorks"))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert client._catalog_inflight, "shielded provider task should finish independently"
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client._catalog_inflight == {}

    @pytest.mark.asyncio
    async def test_shared_catalog_failure_is_evicted_for_retry(self):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
        }))
        client = self._make_client(server)
        calls = 0

        async def _ensure(_server, _source_key, _generation=None):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            raise RuntimeError("provider failed")

        client._ensure_catalog = _ensure
        results = await asyncio.gather(
            client.load_all("AdventureWorks"),
            client.load_all("AdventureWorks"),
        )
        assert results == [_empty_bundle(), _empty_bundle()]
        assert calls == 1
        assert client._catalog_inflight == {}

    @pytest.mark.asyncio
    async def test_shutdown_cancels_shared_catalog_fetch_and_rejects_new_work(self):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
        }))
        client = self._make_client(server)
        started = asyncio.Event()

        async def _ensure(_server, _source_key, _generation=None):
            started.set()
            await asyncio.Event().wait()

        client._ensure_catalog = _ensure
        waiter = asyncio.create_task(client.load_all("AdventureWorks"))
        await started.wait()
        await client.aclose()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert client._catalog_inflight == {}
        with pytest.raises(RuntimeError, match="closing"):
            await client.load_all("AdventureWorks")

    # ── Shared full catalog: stale-while-refresh, invalidation, TTL 0 ─────────

    @staticmethod
    def _stub_cache(client, *, stale=False, invalidated=False, empty=False):
        """The full catalog cached as its single entry, optionally past its TTL or invalidated."""
        payloads = {
            "tables": "- FactSales",
            "columns": "- FactSales.Amount - Type: money\n- FactSales.OrderDate - Type: date",
            "relationships": "[]",
            "business_terms": "",
            "knowledge_pairs": "",
            "sources": "AdventureWorks | postgres",
        }

        async def _get(_server_id, _source_key, cache_key, _ttl):
            if empty or cache_key != KEY_CATALOG:
                return None
            return CacheResult(
                payload=payloads, source="l2_stale" if stale or invalidated else "l1",
                is_stale=stale or invalidated, invalidated=invalidated,
            )

        client._cache_svc.get = _get
        return payloads

    def _counting_ensure(self, client, bundle=None, *, fail=False):
        calls = []

        async def _ensure(_server, source_key, _generation=None):
            calls.append(source_key)
            await asyncio.sleep(0)
            if fail:
                raise RuntimeError("provider down")
            return bundle or {**_empty_bundle(), "tables": "- FreshTable"}

        client._ensure_catalog = _ensure
        return calls

    def _catalog_server(self, ttl=3600):
        server = _make_server(health=_health_with_tools({
            NEED_LIST_SOURCES: "list_connections",
            NEED_LIST_TABLES: "get_catalog_prompt",
        }))
        server.cache_ttl_seconds = ttl
        return server

    @pytest.mark.asyncio
    async def test_expired_catalog_is_served_at_once_and_refreshed_once_in_background(self):
        client = self._make_client(self._catalog_server())
        self._stub_cache(client, stale=True)
        calls = self._counting_ensure(client)

        first, second = await asyncio.gather(
            client.load_all("AdventureWorks"), client.load_all("AdventureWorks"),
        )

        assert first["tables"] == second["tables"] == "- FactSales", "the expired copy is served, not awaited"
        for _ in range(3):
            await asyncio.sleep(0)
        assert calls == ["AdventureWorks"], "one shared background refresh"
        assert client._catalog_inflight == {}

    @pytest.mark.asyncio
    async def test_invalidated_catalog_is_refetched_before_answering(self):
        client = self._make_client(self._catalog_server())
        self._stub_cache(client, invalidated=True)
        calls = self._counting_ensure(client)

        bundle = await client.load_all("AdventureWorks")

        assert bundle["tables"] == "- FreshTable"
        assert calls == ["AdventureWorks"]

    @pytest.mark.asyncio
    async def test_provider_failure_falls_back_to_any_earlier_copy(self):
        server = self._catalog_server()
        client = self._make_client(server)
        self._stub_cache(client, invalidated=True)
        self._counting_ensure(client, fail=True)

        bundle, state = await client._load_full(server, "AdventureWorks")

        assert bundle["tables"] == "- FactSales", "stale-if-error beats an empty catalog"
        assert state == "fallback", "the trace must not claim a refresh is running"

    @pytest.mark.asyncio
    async def test_provider_failure_without_any_copy_is_reported_as_failed(self):
        server = self._catalog_server()
        client = self._make_client(server)
        self._stub_cache(client, empty=True)
        self._counting_ensure(client, fail=True)

        bundle, state = await client._load_full(server, "AdventureWorks")

        assert (bundle, state) == (_empty_bundle(), "failed")

    @pytest.mark.asyncio
    async def test_catalog_is_cached_as_one_entry(self):
        """One entry: a reader can never combine sections from two fetches, and
        a cold L1 costs one L2 row instead of eight."""
        server = self._catalog_server()
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        client._call_tool = AsyncMock(return_value=(
            "## Tables\n- FactSales\n## Columns\n- FactSales.Amount - Type: money\n## Source\nAdventureWorks"
        ))

        await client.load_all("AdventureWorks")

        assert list(client._cache_svc._l1) == [(server.id, "AdventureWorks", KEY_CATALOG)]
        payload = client._cache_svc._l1[(server.id, "AdventureWorks", KEY_CATALOG)][1].payload
        assert set(payload) == set(_empty_bundle())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("old_finishes_first", [True, False])
    async def test_refresh_does_not_join_or_keep_a_fetch_that_predates_it(self, old_finishes_first):
        """A fetch that was already running when the catalog was invalidated may
        hold the old catalog: the refresh starts its own fetch, and the old one
        reaches only its own waiter, never the cache — whichever finishes first."""
        server = self._catalog_server()
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        gates = [asyncio.Event(), asyncio.Event()]
        versions = []

        async def _tool(_server, _tool_name, _args):
            version = len(versions)
            versions.append(version)
            await gates[version].wait()
            return f"## Tables\n- Version{version}\n## Columns\n- Version{version}.Id - Type: integer\n## Source\nAdventureWorks"

        client._call_tool = _tool
        before = asyncio.create_task(client.load_all("AdventureWorks"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert versions == [0]

        await client.invalidate(server.id, "AdventureWorks")
        client.refresh_in_background(server, "AdventureWorks")
        for _ in range(5):
            await asyncio.sleep(0)
        assert versions == [0, 1], "the refresh started its own fetch"

        for gate in (gates if old_finishes_first else reversed(gates)):
            gate.set()
            for _ in range(5):
                await asyncio.sleep(0)
        assert (await before)["tables"] == "- Version0", "its own waiter still gets the old fetch"
        assert client._catalog_inflight == {} and client._catalog_tasks == set()
        assert (await client.load_all("AdventureWorks"))["tables"] == "- Version1"

    @pytest.mark.asyncio
    async def test_connection_list_fetched_before_a_refresh_is_not_cached(self):
        server = self._catalog_server()
        client = self._make_client(server)

        async def _list_connections(_server, _tool, _args):
            await client.invalidate(server.id, "AdventureWorks")
            return {"connections": [{"connection_id": 7, "name": "AdventureWorks", "service_type": "Postgres"}]}

        client._call_tool = _list_connections

        assert await client._get_connections(server)
        assert (server.id, SOURCE_GLOBAL, KEY_CONNECTIONS) not in client._cache_svc._l1

    @pytest.mark.asyncio
    async def test_no_cache_ttl_returns_the_catalog_just_fetched(self):
        """TTL 0 stores nothing; load_all used to re-read the (empty) cache and
        return an empty bundle. It must return what the provider sent."""
        server = self._catalog_server(ttl=NO_CACHE_TTL)
        client = self._make_client(server)
        client._resolve_connection_id = AsyncMock(return_value=42)
        client._call_tool = AsyncMock(return_value=(
            "## Tables\n- FactSales\n## Columns\n- FactSales.Amount - Type: money\n## Source\nAdventureWorks"
        ))

        bundle = await client.load_all("AdventureWorks")

        assert "FactSales" in bundle["tables"]
        assert "FactSales.Amount" in bundle["columns"]
        assert client._cache_svc._l1 == {}, "TTL 0 must not write the cache"

    @pytest.mark.asyncio
    async def test_date_index_is_built_once_per_cached_catalog(self):
        client = self._make_client(self._catalog_server())
        payloads = self._stub_cache(client)
        with patch(
            "src.metadata.mcp_catalog_client.build_date_column_index", wraps=build_date_column_index,
        ) as build:
            for _ in range(3):
                bundle = await client.load_all("AdventureWorks")
            # An L2 read decodes a new string object with the same text.
            l2_copy = (payloads["columns"] + " ")[:-1]
            assert l2_copy is not payloads["columns"]
            view = client._columns_view(1, "AdventureWorks", l2_copy)
            assert build.call_count == 1
            client._columns_view(1, "AdventureWorks", "- FactSales.ShipDate - Type: date")
            assert build.call_count == 2, "a changed catalog is indexed again"
        assert bundle["columns"] == view.text
        assert [column for column, _line in view.dates["factsales"]] == ["orderdate"]

    @pytest.mark.asyncio
    async def test_background_refresh_is_skipped_with_caching_off(self):
        server = self._catalog_server(ttl=NO_CACHE_TTL)
        client = self._make_client(server)

        client.refresh_in_background(server, "AdventureWorks")

        assert client._catalog_inflight == {}

    @pytest.mark.asyncio
    async def test_pooled_http_client_is_reused_and_closed(self):
        server = _make_server()
        client = self._make_client(server)
        fake = MagicMock()
        fake.is_closed = False
        fake.aclose = AsyncMock()
        with patch("src.metadata.mcp_catalog_client.httpx.AsyncClient", return_value=fake) as ctor:
            assert client._client() is fake
            assert client._client() is fake
            ctor.assert_called_once()
            await client.aclose()
        fake.aclose.assert_awaited_once()
        assert client._http_client is None


class TestMcpColumnValueSearch:
    @pytest.mark.asyncio
    async def test_value_search_requires_an_explicit_user_scope_declaration(self):
        server = _make_server(health={
            **_health_with_tools({
                NEED_SEARCH_COLUMN_VALUES: "search_column_values",
            }),
            "tools": [{
                "name": "search_column_values",
                "need": NEED_SEARCH_COLUMN_VALUES,
                "annotations": {"user_scoped": "false"},
            }],
        })
        srv_svc = AsyncMock()
        srv_svc.get_active = AsyncMock(return_value=server)
        client = McpCatalogClient(srv_svc, McpCacheService(_mock_pool()))

        assert await client.value_search_preserves_user_visibility() is False

    @pytest.mark.asyncio
    async def test_search_uses_discovered_schema_and_normalises_response(self):
        server = _make_server(health={
            **_health_with_tools({
                NEED_SEARCH_COLUMN_VALUES: "search_column_values",
            }),
            "tools": [{
                "name": "search_column_values",
                "need": NEED_SEARCH_COLUMN_VALUES,
                "input_schema": {
                    "properties": {
                        "connectionId": {},
                        "tableName": {},
                        "columnName": {},
                        "searchTerm": {},
                        "maxResults": {},
                    }
                },
            }],
        })
        srv_svc = AsyncMock()
        srv_svc.get_active = AsyncMock(return_value=server)
        client = McpCatalogClient(srv_svc, McpCacheService(_mock_pool()))
        client._resolve_connection_id = AsyncMock(return_value=9)
        client._call_tool = AsyncMock(return_value={
            "values": [{"canonical_value": "Mountain-300"}],
            "complete": True,
        })

        result = await client.search_column_values(
            "AdventureWorks",
            table="Product",
            column="ModelName",
            query="mountaiin",
            limit=20,
        )

        assert result["values"] == ["Mountain-300"]
        assert result["complete"] is True
        assert result["source"] == "mcp"
        client._call_tool.assert_awaited_once_with(
            server,
            "search_column_values",
            {
                "connectionId": 9,
                "tableName": "Product",
                "columnName": "ModelName",
                "searchTerm": "mountaiin",
                "maxResults": 20,
            },
        )

        # A reverse lookup omits the column so the provider searches every
        # captured column of the source.
        client._call_tool.reset_mock()
        await client.search_column_values("AdventureWorks", table=None, column=None, query="mosco", limit=20)
        client._call_tool.assert_awaited_once_with(
            server,
            "search_column_values",
            {"connectionId": 9, "searchTerm": "mosco", "maxResults": 20},
        )


# ═══════════════════════════════════════════════════════════════════════════════
# McpCatalogClient — inspector test calls
# ═══════════════════════════════════════════════════════════════════════════════

class TestMcpCatalogClientInspectorCalls:

    def _make_client(self):
        return McpCatalogClient(AsyncMock(), McpCacheService(_mock_pool()))

    @pytest.mark.asyncio
    async def test_test_call_preserves_complete_mcp_result_envelope(self):
        client = self._make_client()
        server = _make_server()
        response = {
            "content": [
                {"type": "text", "text": '{"tables": 42}'},
                {"type": "resource_link", "uri": "mcp://catalog/tables"},
            ],
            "structuredContent": {"tables": 42},
            "_meta": {"requestId": "test-1"},
            "isError": False,
        }

        with patch.object(client, "_jsonrpc", new_callable=AsyncMock, return_value=response) as rpc:
            result = await client.call_tool_for_test(server, "list_tables", {"connection_id": 1})

        assert result == response
        rpc.assert_awaited_once_with(
            server,
            "tools/call",
            {"name": "list_tables", "arguments": {"connection_id": 1}},
        )

    @pytest.mark.asyncio
    async def test_test_call_keeps_tool_level_error_as_structured_result(self):
        client = self._make_client()
        response = {"content": [{"type": "text", "text": "Access denied"}], "isError": True}

        with patch.object(client, "_jsonrpc", new_callable=AsyncMock, return_value=response):
            result = await client.call_tool_for_test(_make_server(), "restricted_tool")

        assert result["isError"] is True
        assert result["content"][0]["text"] == "Access denied"


# ═══════════════════════════════════════════════════════════════════════════════
# McpCatalogClient — run_health_check
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunHealthCheck:

    def _make_client(self, server):
        srv_svc = AsyncMock()
        srv_svc.get_active    = AsyncMock(return_value=server)
        srv_svc.save_health   = AsyncMock(return_value=server)
        cache  = McpCacheService(_mock_pool())
        return McpCatalogClient(srv_svc, cache), srv_svc

    @pytest.mark.asyncio
    async def test_health_check_ok_shape(self):
        server = _make_server()
        client, srv_svc = self._make_client(server)

        init_result = {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "mcp-python", "version": "1.9.2"},
            "capabilities": {"tools": {}, "resources": {}},
        }
        tools_result = {
            "tools": [
                {"name": "catalog.list_tables",    "description": "List tables"},
                {"name": "catalog.describe_table", "description": "Describe columns"},
                {"name": "catalog.glossary",       "description": "Business terms"},
            ]
        }

        async def _fake_jsonrpc(self, server, method, params):
            if method == "initialize":
                return init_result
            if method == "tools/list":
                return tools_result
            return {}

        with patch.object(McpCatalogClient, "_jsonrpc", new=_fake_jsonrpc):
            result = await client.run_health_check(server)

        assert result["ok"] is True
        health = result["health"]
        assert health["status"] == "healthy"
        assert health["protocol"] == "2025-06-18"
        assert health["sdk"] == "mcp-python"
        assert len(health["tools"]) == 3

        # Tools correctly mapped to needs
        needs = {t["need"] for t in health["tools"]}
        assert NEED_LIST_TABLES    in needs
        assert NEED_DESCRIBE_TABLE in needs
        assert NEED_BUSINESS_GLOSSARY in needs

        # Persisted to DB
        srv_svc.save_health.assert_awaited_once_with(server.id, health)

    @pytest.mark.asyncio
    async def test_health_check_returns_error_on_init_failure(self):
        server = _make_server()
        client, srv_svc = self._make_client(server)

        async def _fail_jsonrpc(self, server, method, params):
            raise Exception("connection refused")

        with patch.object(McpCatalogClient, "_jsonrpc", new=_fail_jsonrpc):
            result = await client.run_health_check(server)

        assert result["ok"] is False
        assert "connection refused" in result["error"]
        srv_svc.save_health.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_health_check_latency_ms_is_non_negative(self):
        server = _make_server()
        client, _ = self._make_client(server)

        async def _fast_jsonrpc(self, server, method, params):
            if method == "initialize":
                return {"protocolVersion": "2025-06-18", "serverInfo": {},
                        "capabilities": {"tools": {}}}
            return {"tools": []}

        with patch.object(McpCatalogClient, "_jsonrpc", new=_fast_jsonrpc):
            result = await client.run_health_check(server)

        assert result["ok"] is True
        assert result["health"]["latency_ms"] >= 0
        assert result["health"]["ping_ms"] >= 0
