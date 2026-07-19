"""Connection management endpoints (read-only on `settings_services`)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.dependencies import (
    authorized_source_keys,
    get_connection_service,
    get_metadata_loader,
    get_principal,
    require_admin,
    require_source_access,
)
from src.connections import ConnectionNotFound
from src.security.internal_auth import Principal

router = APIRouter(prefix="/api/connections", tags=["connections"])


class SourceAccessGrant(BaseModel):
    subject_type: Literal["user", "role", "group"]
    subject_id: str = Field(min_length=1, max_length=255)


@router.get("")
async def list_connections(principal: Principal = Depends(get_principal)):
    service = get_connection_service()
    connections = await service.list_connections()
    allowed = await authorized_source_keys(principal)
    if allowed is not None:
        connections = [connection for connection in connections if connection.source_key in allowed]
    return {"connections": [c.to_public_dict() for c in connections]}


@router.get("/{source_key}")
async def get_connection(
    source_key: str,
    principal: Principal = Depends(get_principal),
):
    source_key = await require_source_access(principal, source_key)
    service = get_connection_service()
    loader = get_metadata_loader()
    try:
        connection = await service.get_connection(source_key)
    except ConnectionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    summary = await loader.metadata_summary(source_key)
    return {**connection.to_public_dict(), "metadata_summary": summary}


@router.get("/{source_key}/access")
async def list_source_access(
    source_key: str,
    _: Principal = Depends(require_admin),
):
    """List explicit grants for a source (administrators only)."""
    from src.metadata import get_metadata_pool

    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT subject_type, subject_id, created_at
            FROM insights_source_access
            WHERE source_key = $1
            ORDER BY subject_type, subject_id
            """,
            source_key,
        )
    return {"source_key": source_key, "grants": [dict(row) for row in rows]}


@router.put("/{source_key}/access")
async def grant_source_access(
    source_key: str,
    grant: SourceAccessGrant,
    _: Principal = Depends(require_admin),
):
    """Grant a user, application role, or Entra group access to one source."""
    service = get_connection_service()
    try:
        await service.get_connection(source_key)
    except ConnectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    from src.metadata import get_metadata_pool

    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO insights_source_access (source_key, subject_type, subject_id)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            source_key,
            grant.subject_type,
            grant.subject_id.strip(),
        )
    return {"source_key": source_key, "granted": grant.model_dump()}


@router.delete("/{source_key}/access")
async def revoke_source_access(
    source_key: str,
    subject_type: Literal["user", "role", "group"] = Query(...),
    subject_id: str = Query(..., min_length=1, max_length=255),
    _: Principal = Depends(require_admin),
):
    """Revoke one explicit source grant (administrators only)."""
    from src.metadata import get_metadata_pool

    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM insights_source_access
            WHERE source_key = $1 AND subject_type = $2 AND subject_id = $3
            """,
            source_key,
            subject_type,
            subject_id.strip(),
        )
    if result != "DELETE 1":
        raise HTTPException(status_code=404, detail="Source access grant not found")
    return {"source_key": source_key, "revoked": {"subject_type": subject_type, "subject_id": subject_id}}


@router.post("/{source_key}/refresh-metadata")
async def refresh_connection_metadata(
    source_key: str,
    _: Principal = Depends(require_admin),
):
    loader = get_metadata_loader()
    loader.invalidate(source_key)
    return {"status": "ok", "message": f"Metadata cache invalidated for {source_key}"}


@router.post("/{source_key}/warm-cache")
async def warm_connection_cache(
    source_key: str,
    principal: Principal = Depends(get_principal),
):
    """Pre-load and cache metadata for *source_key* without a full query.

    Called by the UI when the user switches to a different connection so the
    next query doesn't pay the metadata-fetch latency.
    """
    source_key = await require_source_access(principal, source_key)
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
