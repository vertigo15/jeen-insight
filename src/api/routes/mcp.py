"""MCP catalog management routes.

Provides the API surface for the Metadata & Catalog settings panel.

Endpoints
---------
GET  /api/mcp/status               — catalog_source + DB stats + server list
PUT  /api/mcp/catalog-source       — switch catalog_source ('db' | 'mcp')
GET  /api/mcp/servers              — list saved MCP servers
POST /api/mcp/servers              — create a server
PUT  /api/mcp/servers/{id}         — update server config (clears health)
DELETE /api/mcp/servers/{id}       — delete server
POST /api/mcp/servers/{id}/activate   — activate + switch to mcp mode
POST /api/mcp/servers/{id}/deactivate — deactivate + switch to db mode
POST /api/mcp/servers/{id}/health-check — run health check
POST /api/mcp/refresh              — invalidate metadata cache for a connection
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.api import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _srv_svc():
    svc = state.mcp_server_service
    if svc is None:
        raise HTTPException(503, "MCP service not initialised")
    return svc


def _cache_svc():
    svc = state.mcp_cache_service
    if svc is None:
        raise HTTPException(503, "MCP cache service not initialised")
    return svc


def _catalog_client():
    client = state.mcp_catalog_client
    if client is None:
        raise HTTPException(503, "MCP catalog client not initialised")
    return client


# ── Pydantic models ───────────────────────────────────────────────────────────

class SetCatalogSourceRequest(BaseModel):
    catalog_source: str  # 'db' | 'mcp'


class CreateServerRequest(BaseModel):
    server_name: str
    endpoint: str
    transport: str = "http"
    auth_type: str = "none"
    bearer_token: Optional[str] = None
    cache_ttl_seconds: int = 900


class UpdateServerRequest(BaseModel):
    server_name: Optional[str] = None
    endpoint: Optional[str] = None
    transport: Optional[str] = None
    auth_type: Optional[str] = None
    bearer_token: Optional[str] = None
    cache_ttl_seconds: Optional[int] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_mcp_status(
    connection: Optional[str] = Query(None, description="source_key to fetch DB stats for"),
):
    """
    Return the full Metadata & Catalog panel state:
      - catalog_source ('db' | 'mcp')  — per-connection when connection is provided
      - DB stats for the requested connection (table/column/term counts)
      - List of saved MCP servers
      - Active server summary
    """
    svc = _srv_svc()

    # Per-connection catalog source + TTL.
    conn_cfg       = await svc.get_connection_config(connection) if connection else {}
    catalog_source = conn_cfg.get("catalog_source", "db")
    conn_ttl       = conn_cfg.get("cache_ttl_seconds", 900)
    servers        = await svc.list_all()
    active_server  = next((s for s in servers if s.is_active), None)

    # DB stats
    db_info: Dict[str, Any] = {}
    if connection and state.metadata_loader:
        try:
            summary = await state.metadata_loader.metadata_summary(connection)
            db_info = {
                "provider":       "Schema Modeler",
                "database":       _db_name(),
                "tables":         summary.get("tables", 0),
                "columns":        summary.get("columns", 0),
                "business_terms": summary.get("business_terms", 0),
                "knowledge_pairs":summary.get("knowledge_pairs", 0),
                "cache_status":   _db_cache_status(connection),
            }
        except Exception as exc:
            logger.warning("mcp/status: metadata_summary failed: %s", exc)

    # Cache status for MCP mode
    mcp_cache_status: Dict[str, Any] = {}
    if active_server and state.mcp_cache_service and connection:
        try:
            mcp_cache_status = await state.mcp_cache_service.get_status(
                active_server.id, connection
            )
        except Exception:
            pass

    return {
        "catalog_source":    catalog_source,
        "cache_ttl_seconds": conn_ttl,
        "connection":        connection,
        "db":                db_info,
        "mcp_cache":         mcp_cache_status,
        "active_server_id":  active_server.id if active_server else None,
        "servers":           [s.to_dict() for s in servers],
    }


@router.put("/catalog-source")
async def set_catalog_source(
    body: SetCatalogSourceRequest,
    connection: Optional[str] = Query(None, description="source_key to update (per-connection)"),
):
    """Switch catalog source for a connection ('db' | 'mcp')."""
    if body.catalog_source not in ("db", "mcp"):
        raise HTTPException(400, "catalog_source must be 'db' or 'mcp'")

    svc = _srv_svc()
    try:
        await svc.set_catalog_source(body.catalog_source, source_key=connection)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Invalidate catalog cache for this connection when switching sources.
    if state.mcp_cache_service:
        active = await svc.get_active()
        if active:
            await state.mcp_cache_service.invalidate(active.id, source_key=connection)
    if state.metadata_loader:
        state.metadata_loader.invalidate(connection)

    return {"catalog_source": body.catalog_source, "connection": connection}


# ── Server CRUD ───────────────────────────────────────────────────────────────

@router.get("/servers")
async def list_servers():
    servers = await _srv_svc().list_all()
    return {"servers": [s.to_dict() for s in servers]}


@router.post("/servers")
async def create_server(body: CreateServerRequest):
    svc = _srv_svc()
    server = await svc.create(
        server_name=body.server_name,
        endpoint=body.endpoint,
        transport=body.transport,
        auth_type=body.auth_type,
        bearer_token=body.bearer_token or None,
        cache_ttl_seconds=body.cache_ttl_seconds,
    )
    return server.to_dict()


@router.put("/servers/{server_id}")
async def update_server(server_id: int, body: UpdateServerRequest):
    svc = _srv_svc()

    # Build kwargs — only include fields that were provided
    fields: Dict[str, Any] = {}
    if body.server_name   is not None: fields["server_name"]       = body.server_name
    if body.endpoint      is not None: fields["endpoint"]          = body.endpoint
    if body.transport     is not None: fields["transport"]         = body.transport
    if body.auth_type     is not None: fields["auth_type"]         = body.auth_type
    if body.bearer_token  is not None: fields["bearer_token"]      = body.bearer_token
    if body.cache_ttl_seconds is not None: fields["cache_ttl_seconds"] = body.cache_ttl_seconds

    server = await svc.update(server_id, **fields)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    # Saving config clears cached data for this server
    if state.mcp_cache_service:
        await state.mcp_cache_service.invalidate(server_id)

    return server.to_dict()


@router.delete("/servers/{server_id}")
async def delete_server(server_id: int):
    svc = _srv_svc()
    deleted = await svc.delete(server_id)
    if not deleted:
        raise HTTPException(404, f"Server {server_id} not found")
    return {"ok": True, "deleted_id": server_id}


# ── Activation ────────────────────────────────────────────────────────────────

# Required catalog need keys that must be mapped before a server can go live.
_REQUIRED_NEEDS = {"list_tables", "describe_table"}


@router.post("/servers/{server_id}/activate")
async def activate_server(
    server_id: int,
    connection: Optional[str] = Query(None, description="source_key to switch to mcp (optional)"),
):
    """Activate this server as the global active MCP server.

    If the server has been health-checked and required catalog needs are still
    unmapped, returns 422 so the client can surface the error via toast.
    Untested servers (health=None) may still be activated.
    """
    svc    = _srv_svc()
    server = await svc.get_by_id(server_id)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    # Guard: reject if the server was checked but required needs are still unmapped.
    if server.health and server.health.get("status") in ("healthy", "degraded"):
        mapped = {t.get("need") for t in server.health.get("tools", []) if t.get("need")}
        missing = _REQUIRED_NEEDS - mapped
        if missing:
            labels = {"list_tables": "List tables", "describe_table": "Describe table / columns"}
            raise HTTPException(
                422,
                f"{', '.join(labels.get(n, n) for n in sorted(missing))} "
                "required but unmapped — run a health check that satisfies these needs first.",
            )

    activated = await svc.activate(server_id)
    if not activated:
        raise HTTPException(500, "Activation failed")
    return activated.to_dict()


@router.post("/servers/{server_id}/deactivate")
async def deactivate_server(server_id: int):
    """Deactivate this server and revert catalog_source to 'db'."""
    svc  = _srv_svc()
    server = await svc.deactivate(server_id)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")
    return server.to_dict()


# ── Health check ──────────────────────────────────────────────────────────────

@router.post("/servers/{server_id}/health-check")
async def run_health_check(server_id: int):
    """Run a full health check against the server (initialize + tools/list)."""
    svc    = _srv_svc()
    server = await svc.get_by_id(server_id)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    client = _catalog_client()
    result = await client.run_health_check(server)

    if not result.get("ok"):
        # Persist the failure status so the UI dot shows red
        await svc.save_test_result(
            server_id, ok=False, message=result.get("error", "Unknown error")
        )
        return result   # ok=False with error message — not a 500

    # Re-read from DB to return the updated health blob
    updated = await svc.get_by_id(server_id)
    return {
        "ok":     True,
        "server": updated.to_dict() if updated else None,
        "health": result.get("health"),
    }


# ── Cache refresh ─────────────────────────────────────────────────────────────

@router.put("/connection-ttl")
async def set_connection_ttl(
    connection: str = Query(..., description="source_key to update"),
    cache_ttl_seconds: int = Query(..., description="TTL in seconds: 0|300|900|3600|86400"),
):
    """Update the per-connection catalog cache TTL."""
    if cache_ttl_seconds not in (0, 300, 900, 3600, 86400):
        raise HTTPException(400, "cache_ttl_seconds must be 0, 300, 900, 3600, or 86400")
    svc = _srv_svc()
    await svc.set_connection_ttl(connection, cache_ttl_seconds)
    return {"connection": connection, "cache_ttl_seconds": cache_ttl_seconds}


@router.post("/refresh")
async def refresh_catalog(
    connection: Optional[str] = Query(None, description="source_key to invalidate"),
):
    """
    Invalidate the catalog cache so the next query fetches fresh data.

    - DB mode  → invalidates MetadataLoader in-memory cache
    - MCP mode → marks insights_mcp_cache rows stale
    """
    svc    = _srv_svc()
    source = await svc.get_catalog_source()

    if source == "mcp" and state.mcp_cache_service:
        active = await svc.get_active()
        if active:
            await state.mcp_cache_service.invalidate(active.id, source_key=connection)

    if state.metadata_loader:
        state.metadata_loader.invalidate(connection)

    return {"ok": True, "connection": connection, "catalog_source": source}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _db_name() -> str:
    """Return the metadata DB name from app settings."""
    try:
        from src.config import settings as cfg
        return cfg.METADATA_DB_NAME
    except Exception:
        return "—"


def _db_cache_status(source_key: str) -> Dict[str, Any]:
    """Return a basic DB cache status dict for the UI status chip."""
    if not state.metadata_loader:
        return {"hit": False}
    cache   = state.metadata_loader._cache
    key     = f"tables_rich::{source_key}"
    import time
    entry   = cache.get(key)
    if entry and entry[0] > time.monotonic():
        return {"hit": True, "expires_in_s": int(entry[0] - time.monotonic())}
    return {"hit": False}
