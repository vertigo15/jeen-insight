"""Connection management endpoints (read-only on `settings_services`)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.dependencies import get_connection_service, get_metadata_loader
from src.connections import ConnectionNotFound

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
    loader = get_metadata_loader()
    loader.invalidate(source_key)
    return {"status": "ok", "message": f"Metadata cache invalidated for {source_key}"}


@router.post("/{source_key}/warm-cache")
async def warm_connection_cache(source_key: str):
    """Pre-load and cache metadata for *source_key* without a full query.

    Called by the UI when the user switches to a different connection so the
    next query doesn't pay the metadata-fetch latency.
    """
    from src.api import state
    loader = get_metadata_loader()
    bundle = await loader.load_all(source_key)
    # Also warm the AgentRegistry so the agent object is built.
    if state.agent_registry:
        try:
            await state.agent_registry.get_agent(source_key)
        except Exception:  # noqa: BLE001
            pass  # connection might not have a live DB yet; metadata still cached
    return {
        "status": "ok",
        "source_key": source_key,
        "tables": bundle.get("tables", "").count("\n") + 1 if bundle.get("tables") else 0,
    }
