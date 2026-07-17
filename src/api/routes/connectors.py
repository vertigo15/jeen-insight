"""Admin connector registry, catalog, group authorization, and master switch.

All routes require an admin Principal. The master-switch read/write endpoints are
intentionally NOT gated by the switch (an admin must be able to turn it on).
Every other admin route is gated so a disabled feature exposes nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.dependencies import (
    get_audit_service,
    get_identity_service,
    get_registry_service,
    require_admin,
    require_connectors_enabled,
    resolve_tenant_id,
)
from src.connectors.catalog import list_catalog
from src.connectors.registry_service import RegistryError
from src.security.app_flags import get_connectors_enabled, set_connectors_enabled
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connectors", tags=["connectors-admin"])


# ── Master switch (not gated by the switch itself) ──────────────────────────

@router.get("/feature")
async def get_feature(principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"enabled": await get_connectors_enabled(use_cache=False)}


class FeatureToggle(BaseModel):
    enabled: bool


@router.put("/feature")
async def set_feature(
    body: FeatureToggle,
    principal: Principal = Depends(require_admin),
    audit=Depends(get_audit_service),
) -> Dict[str, Any]:
    value = await set_connectors_enabled(body.enabled)
    await audit.log(
        event_type="feature.toggled",
        actor_user_id=principal.user_id,
        actor_email=principal.email,
        outcome="enabled" if value else "disabled",
    )
    return {"enabled": value}


# ── Catalog (curated native providers) ──────────────────────────────────────

@router.get("/catalog", dependencies=[Depends(require_connectors_enabled)])
async def get_catalog(principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"catalog": [e.to_public() for e in list_catalog()]}


# ── Connector CRUD ──────────────────────────────────────────────────────────

@router.get("", dependencies=[Depends(require_connectors_enabled)])
async def list_connectors(
    principal: Principal = Depends(require_admin), registry=Depends(get_registry_service)
) -> Dict[str, Any]:
    return {"connectors": await registry.list_connectors()}


class CreateConnector(BaseModel):
    catalog_key: str
    display_name: Optional[str] = None


@router.post("", dependencies=[Depends(require_connectors_enabled)])
async def create_connector(
    body: CreateConnector,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
) -> Dict[str, Any]:
    try:
        return await registry.create_connector(
            catalog_key=body.catalog_key,
            display_name=body.display_name,
            created_by=principal.email or principal.user_id,
        )
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class EnabledToggle(BaseModel):
    enabled: bool


@router.patch("/{connector_id}/enabled", dependencies=[Depends(require_connectors_enabled)])
async def set_enabled(
    connector_id: str,
    body: EnabledToggle,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
    audit=Depends(get_audit_service),
) -> Dict[str, Any]:
    result = await registry.set_enabled(connector_id, body.enabled)
    if not result:
        raise HTTPException(status_code=404, detail="Connector not found")
    await audit.log(
        event_type="connector.enabled" if body.enabled else "connector.disabled",
        actor_user_id=principal.user_id, actor_email=principal.email,
        connector_id=connector_id, outcome="ok",
    )
    return result


class ConnectorConfig(BaseModel):
    config: Dict[str, Any]


@router.put("/{connector_id}/config", dependencies=[Depends(require_connectors_enabled)])
async def set_config(
    connector_id: str,
    body: ConnectorConfig,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
) -> Dict[str, Any]:
    try:
        result = await registry.set_config(
            connector_id, body.config, created_by=principal.email or principal.user_id
        )
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Connector not found")
    return result


class ClientSecret(BaseModel):
    # client_id is non-secret config; the secret is encrypted at rest.
    client_id: Optional[str] = None
    tenant_id: Optional[str] = None
    secret: str


@router.put("/{connector_id}/client-secret", dependencies=[Depends(require_connectors_enabled)])
async def set_client_secret(
    connector_id: str,
    body: ClientSecret,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
    audit=Depends(get_audit_service),
) -> Dict[str, Any]:
    connector = await registry.get_connector(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    # Persist non-secret client_id/tenant_id into the (immutable, versioned) config.
    if body.client_id or body.tenant_id:
        cfg = dict((connector.get("current_version") or {}).get("config") or {})
        if body.client_id:
            cfg["client_id"] = body.client_id.strip()
        if body.tenant_id:
            cfg["tenant_id"] = body.tenant_id.strip()
        await registry.set_config(connector_id, cfg, created_by=principal.email or principal.user_id)
    try:
        await registry.set_client_secret(
            connector_id, body.secret, created_by=principal.email or principal.user_id
        )
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit.log(
        event_type="connector.client_secret_set",
        actor_user_id=principal.user_id, actor_email=principal.email,
        connector_id=connector_id, outcome="ok",
    )
    return {"ok": True}


@router.delete("/{connector_id}", dependencies=[Depends(require_connectors_enabled)])
async def delete_connector(
    connector_id: str,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
) -> Dict[str, Any]:
    ok = await registry.delete_connector(connector_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Connector not found")
    return {"deleted": True}


# ── group -> connector gating ───────────────────────────────────────────────

class GroupGrant(BaseModel):
    group_object_id: str
    tenant_id: Optional[str] = None


@router.post("/{connector_id}/groups", dependencies=[Depends(require_connectors_enabled)])
async def add_group_grant(
    connector_id: str,
    body: GroupGrant,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
) -> Dict[str, Any]:
    tenant = (body.tenant_id or "").strip() or resolve_tenant_id(principal)
    return await registry.add_group_grant(
        connector_id=connector_id, tenant_id=tenant,
        group_object_id=body.group_object_id.strip(),
        created_by=principal.email or principal.user_id,
    )


@router.delete(
    "/{connector_id}/groups/{group_object_id}",
    dependencies=[Depends(require_connectors_enabled)],
)
async def remove_group_grant(
    connector_id: str,
    group_object_id: str,
    principal: Principal = Depends(require_admin),
    registry=Depends(get_registry_service),
) -> Dict[str, Any]:
    ok = await registry.remove_group_grant(connector_id, group_object_id)
    return {"removed": ok}


# ── group -> role mapping ───────────────────────────────────────────────────

class GroupRole(BaseModel):
    group_object_id: str
    role: str
    tenant_id: Optional[str] = None


@router.get("/group-roles", dependencies=[Depends(require_connectors_enabled)])
async def list_group_roles(
    principal: Principal = Depends(require_admin), identities=Depends(get_identity_service)
) -> Dict[str, Any]:
    return {"group_roles": await identities.list_group_roles()}


@router.post("/group-roles", dependencies=[Depends(require_connectors_enabled)])
async def set_group_role(
    body: GroupRole,
    principal: Principal = Depends(require_admin),
    identities=Depends(get_identity_service),
) -> Dict[str, Any]:
    tenant = (body.tenant_id or "").strip() or resolve_tenant_id(principal)
    try:
        return await identities.set_group_role(
            tenant_id=tenant, group_object_id=body.group_object_id.strip(),
            role=body.role, created_by=principal.email or principal.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/group-roles/{role_id}", dependencies=[Depends(require_connectors_enabled)])
async def delete_group_role(
    role_id: str,
    principal: Principal = Depends(require_admin),
    identities=Depends(get_identity_service),
) -> Dict[str, Any]:
    return {"deleted": await identities.delete_group_role(role_id)}


# ── Audit (admin) ───────────────────────────────────────────────────────────

@router.get("/audit", dependencies=[Depends(require_connectors_enabled)])
async def list_audit(
    limit: int = 200,
    principal: Principal = Depends(require_admin),
    audit=Depends(get_audit_service),
) -> Dict[str, Any]:
    return {"events": await audit.list_recent(limit=limit)}
