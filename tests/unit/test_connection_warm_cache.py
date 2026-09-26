from __future__ import annotations

import asyncio
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
async def test_warm_cache_builds_the_agent_while_the_catalog_downloads(monkeypatch):
    """One after the other, a question asked right after page load waited for
    the catalog download and then the agent build (~3 s after a restart)."""
    monkeypatch.setattr(connections, "get_metadata_loader", lambda: MagicMock())
    agent_building = asyncio.Event()

    async def _download(_source_key):
        # Finishes only once the agent build has started next to it.
        await asyncio.wait_for(agent_building.wait(), timeout=1)
        return {"tables": "- FactSales", "columns": "- FactSales.Amount - Type: money"}

    async def _build(_source_key):
        agent_building.set()

    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="mcp")
    catalog_client = MagicMock()
    catalog_client.load_all = AsyncMock(side_effect=_download)
    registry = MagicMock()
    registry.get_agent = AsyncMock(side_effect=_build)
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_catalog_client", catalog_client)
    monkeypatch.setattr(state, "agent_registry", registry)

    result = await connections.warm_connection_cache("AdventureWorks")

    assert result["provider"] == "mcp"
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
async def test_refresh_metadata_also_clears_and_refetches_the_mcp_catalog(monkeypatch):
    """The connection panel's refresh used to clear only the metadata-DB loader,
    so an MCP catalog stayed cached until its TTL ran out."""
    loader = MagicMock()
    monkeypatch.setattr(connections, "get_metadata_loader", lambda: loader)
    server = MagicMock(id=7)
    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="mcp")
    server_service.get_active = AsyncMock(return_value=server)
    catalog_client = MagicMock()
    catalog_client.invalidate = AsyncMock()
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_catalog_client", catalog_client)

    result = await connections.refresh_connection_metadata("AdventureWorks")

    loader.invalidate.assert_called_once_with("AdventureWorks")
    catalog_client.invalidate.assert_awaited_once_with(7, "AdventureWorks")
    catalog_client.refresh_in_background.assert_called_once_with(server, "AdventureWorks")
    assert result["mcp_refreshed"] is True


@pytest.mark.asyncio
async def test_refresh_metadata_leaves_mcp_alone_for_a_db_catalog(monkeypatch):
    loader = MagicMock()
    monkeypatch.setattr(connections, "get_metadata_loader", lambda: loader)
    server_service = MagicMock()
    server_service.get_catalog_source = AsyncMock(return_value="db")
    catalog_client = MagicMock()
    catalog_client.invalidate = AsyncMock()
    monkeypatch.setattr(state, "mcp_server_service", server_service)
    monkeypatch.setattr(state, "mcp_catalog_client", catalog_client)

    result = await connections.refresh_connection_metadata("AdventureWorks")

    loader.invalidate.assert_called_once_with("AdventureWorks")
    catalog_client.invalidate.assert_not_awaited()
    assert result["mcp_refreshed"] is False


def test_page_load_warms_the_connection_it_opens_with():
    """Warm-up used to run only on a connection switch, so the connection shown
    on load paid the full-catalog download on its first question."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "src/static/script.js").read_text(encoding="utf-8")
    load = script[script.index("async function loadConnections()"):script.index("function onConnectionChange(")]
    assert "_warmConnectionCache(active);" in load
    switch = script[script.index("function onConnectionChange("):script.index("function _warmConnectionCache(")]
    assert "_warmConnectionCache(newConnection);" in switch


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
