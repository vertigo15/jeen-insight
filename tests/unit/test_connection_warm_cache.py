from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api import state
from src.api.lifespan import _restore_mcp_l1_cache, _warm_caches
from src.api.routes import connections


@pytest.mark.asyncio
async def test_warm_cache_uses_mcp_catalog_when_source_is_mcp(monkeypatch):
    loader = MagicMock()
    loader.load_all = AsyncMock()
    monkeypatch.setattr(connections, "get_metadata_loader", lambda: loader)

    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="mcp")
    catalog_client = MagicMock()
    catalog_client.load_all = AsyncMock(return_value={
        "tables": "- FactSales\n- DimDate",
        "columns": "- FactSales.Amount - Type: money",
    })
    registry = MagicMock()
    registry.get_agent = AsyncMock()
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_catalog_client", catalog_client)
    monkeypatch.setattr(state, "agent_registry", registry)

    result = await connections.warm_connection_cache("AdventureWorks")

    assert result == {
        "status": "ok",
        "source_key": "AdventureWorks",
        "provider": "mcp",
        "tables": 2,
    }
    catalog_client.load_all.assert_awaited_once_with("AdventureWorks")
    loader.load_all.assert_not_awaited()
    registry.get_agent.assert_awaited_once_with("AdventureWorks")


@pytest.mark.asyncio
async def test_warm_cache_falls_back_to_metadata_loader(monkeypatch):
    loader = MagicMock()
    loader.load_all = AsyncMock(return_value={
        "tables": "- FactSales",
        "columns": "- FactSales.Amount - Type: money",
    })
    monkeypatch.setattr(connections, "get_metadata_loader", lambda: loader)

    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="db")
    catalog_client = MagicMock()
    catalog_client.load_all = AsyncMock()
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_catalog_client", catalog_client)
    monkeypatch.setattr(state, "agent_registry", None)

    result = await connections.warm_connection_cache("AdventureWorks")

    assert result["provider"] == "db"
    loader.load_all.assert_awaited_once_with("AdventureWorks")
    catalog_client.load_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_mcp_mode_skips_fetching_every_db_catalog():
    loader = MagicMock()
    loader.load_all = AsyncMock()
    connection_service = MagicMock()
    connection_service.list_connections = AsyncMock()
    prompt_cache = MagicMock()
    prompt_cache.get_content = AsyncMock(return_value="system")

    await _warm_caches(
        loader,
        connection_service,
        prompt_cache,
        warm_metadata=False,
    )

    connection_service.list_connections.assert_not_awaited()
    loader.load_all.assert_not_awaited()
    prompt_cache.get_content.assert_awaited_once_with("jeen_insights_system")


@pytest.mark.asyncio
async def test_startup_mcp_without_active_server_falls_back_to_db_mode(monkeypatch):
    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="mcp")
    server_service.get_active = AsyncMock(return_value=None)
    cache_service = MagicMock()
    cache_service.warm_from_db = AsyncMock()
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_cache_service", cache_service)

    assert await _restore_mcp_l1_cache() is False
    cache_service.warm_from_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_mcp_restores_l2_entries_and_stays_in_mcp_mode(monkeypatch):
    server = MagicMock(id=7, server_name="catalog-mcp")
    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="mcp")
    server_service.get_active = AsyncMock(return_value=server)
    cache_service = MagicMock()
    cache_service.warm_from_db = AsyncMock(return_value=12)
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_cache_service", cache_service)

    assert await _restore_mcp_l1_cache() is True
    cache_service.warm_from_db.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_startup_mcp_restore_failure_does_not_enable_bulk_db_warming(monkeypatch):
    server = MagicMock(id=7, server_name="catalog-mcp")
    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="mcp")
    server_service.get_active = AsyncMock(return_value=server)
    cache_service = MagicMock()
    cache_service.warm_from_db = AsyncMock(side_effect=RuntimeError("L2 unavailable"))
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_cache_service", cache_service)

    assert await _restore_mcp_l1_cache() is True
    cache_service.warm_from_db.assert_awaited_once_with(7)
