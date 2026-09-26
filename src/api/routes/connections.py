"""Connection management endpoints (read-only on `settings_services`)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from src.api.dependencies import get_connection_service, get_metadata_loader
from src.connections import ConnectionNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connections", tags=["connections"])


@router.get("")
async def list_connections():
    service = get_connection_service()
    connections = await service.list_connections()
    return {"connections": [c.to_public_dict() for c in connections]}


@router.get("/{source_key}")
async def get_connection(source_key: str):
    service = get_connection_service()
    loader = get_metadata_loader()
    try:
        connection = await service.get_connection(source_key)
    except ConnectionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    summary = await loader.metadata_summary(source_key)
    return {**connection.to_public_dict(), "metadata_summary": summary}


@router.post("/{source_key}/refresh-metadata")
async def refresh_connection_metadata(source_key: str):
    """Drop this connection's cached catalog (metadata DB and MCP) and refetch it.

    The MCP refetch runs in the background, so the next question already finds
    the fresh catalog instead of paying for the download itself.
    """
    from src.api import state
    loader = get_metadata_loader()
    loader.invalidate(source_key)
    mcp_refreshed = False
    if state.mcp_server_service and state.mcp_catalog_client:
        try:
            if await state.mcp_server_service.get_catalog_source(source_key) == "mcp":
                active = await state.mcp_server_service.get_active()
                if active:
                    await state.mcp_catalog_client.invalidate(active.id, source_key)
                    state.mcp_catalog_client.refresh_in_background(active, source_key)
                    mcp_refreshed = True
        except Exception as exc:  # noqa: BLE001 - the metadata-DB cache is already cleared
            logger.warning("refresh-metadata: MCP cache refresh failed for %s: %s", source_key, exc)
    return {
        "status": "ok",
        "message": f"Metadata cache invalidated for {source_key}",
        "mcp_refreshed": mcp_refreshed,
    }


@router.post("/{source_key}/warm-cache")
async def warm_connection_cache(source_key: str):
    """Pre-load and cache metadata for *source_key* without a full query.

    Called by the UI when the page loads and when the user switches to a
    different connection, so the next query doesn't pay the metadata-fetch
    latency. The agent is built at the same time as the catalog downloads:
    one after the other, a question asked right away waited for both.
    """
    from src.api import state
    loader = get_metadata_loader()

    async def _catalog():
        if state.mcp_server_service and state.mcp_catalog_client:
            try:
                source = await state.mcp_server_service.get_catalog_source(source_key)
                if source == "mcp":
                    bundle = await state.mcp_catalog_client.load_all(source_key)
                    if bundle.get("tables", "").strip() not in ("", "No tables registered."):
                        return bundle, "mcp"
            except Exception:  # noqa: BLE001
                pass
        return await loader.load_all(source_key), "db"

    async def _agent():
        if state.agent_registry:
            try:
                await state.agent_registry.get_agent(source_key)
            except Exception:  # noqa: BLE001
                pass  # connection might not have a live DB yet; metadata still cached

    (bundle, provider), _ = await asyncio.gather(_catalog(), _agent())
    return {
        "status": "ok",
        "source_key": source_key,
        "provider": provider,
        "tables": bundle.get("tables", "").count("\n") + 1 if bundle.get("tables") else 0,
    }
