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

import json
import time
from datetime import datetime, timezone
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
    REQUIRED_NEEDS,
)
from src.metadata.mcp_cache_service import (
    McpCacheService,
    CacheResult,
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
    _normalise_connections,
    _parse_catalog_markdown,
    _map_tool_to_need,
    _empty_bundle,
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
                         "knowledge_pairs", "business_terms"}
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
    ])
    def test_known_tool_names(self, tool_name, expected_need):
        assert _map_tool_to_need(tool_name) == expected_need

    def test_unmapped_tool_returns_none(self):
        assert _map_tool_to_need("analytics.run_query") is None
        assert _map_tool_to_need("ping") is None


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

        cached_payloads: dict of cache_key → payload to pre-seed into L1.
        "connections" key: stored per source_key ("AdventureWorks", KEY_CONNECTIONS)
                           NOT under SOURCE_GLOBAL (global list uses a separate path).
        """
        srv_svc = AsyncMock()
        srv_svc.get_active = AsyncMock(return_value=server)

        pool  = _mock_pool()
        cache = McpCacheService(pool)

        if cached_payloads:
            for ck, payload in cached_payloads.items():
                # "connections" in this context is the per-source sources text
                source_key = "AdventureWorks"
                l1_key = (server.id, source_key, ck)
                result = CacheResult(
                    payload=payload, source="l1",
                    fetched_at=datetime.now(tz=timezone.utc), is_stale=False
                )
                cache._l1[l1_key] = (time.monotonic() + 900, result)

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
            # sources is stored per source_key under KEY_CONNECTIONS
            "connections":     "AdventureWorks | postgres | (Active: True)",
        })

        bundle = await client.load_all("AdventureWorks")

        assert "DimProduct" in bundle["tables"]
        assert "DimProduct.ProductKey" in bundle["columns"]
        assert "AdventureWorks" in bundle["sources"]
        assert "No tables registered." not in bundle["tables"]

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

        client._call_tool.assert_awaited_once_with(
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
