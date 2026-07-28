"""Per-user connection management: list, OAuth connect/callback, revoke.

Per-user connectors are Entra-bound: the caller must be an SSO Principal. The
OAuth callback binds the returned mailbox identity to the logged-in identity and
rejects a different mailbox (no cross-account linking in v1).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.dependencies import (
    ensure_identity,
    get_audit_service,
    get_grant_service,
    get_identity_service,
    get_principal,
    get_registry_service,
    require_connectors_enabled,
)
from src.connectors import oauth
from src.connectors.grant_service import GrantError
from src.connectors.providers import get_provider
from src.security.crypto import CryptoError
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/me/connections",
    tags=["connectors-user"],
    dependencies=[Depends(require_connectors_enabled)],
)


@router.get("")
async def list_my_connections(
    principal: Principal = Depends(get_principal),
    registry=Depends(get_registry_service),
    grants=Depends(get_grant_service),
    identities=Depends(get_identity_service),
) -> Dict[str, Any]:
    identity = await ensure_identity(principal)
    identity_id = identity["id"]

    connectors = await registry.list_connectors()
    grant_list = {g["connector_id"]: g for g in await grants.list_grants(identity_id)}

    available: List[Dict[str, Any]] = []
    for c in connectors:
        if not c["is_enabled"]:
            continue
        # API-key connectors (e.g. Tavily) are admin-configured and have no
        # per-user OAuth "Connect"; they never appear in this list.
        if c.get("auth_kind") == "api_key":
            continue
        grant_ids = c.get("group_grants") or []
        allowed, reason = await identities.can_use_connector(
            identity_id, c["id"], group_grant_ids=grant_ids
        )
        if not allowed:
            continue
        g = grant_list.get(c["id"])
        available.append({
            "connector_id": c["id"],
            "key": c["key"],
            "display_name": c["display_name"],
            "category": c["category"],
            "connected": bool(g and g["status"] == "active"),
            "external_account": g["external_account"] if g else None,
            "status": g["status"] if g else None,
        })
    return {"connections": available}


class AuthorizeRequest(BaseModel):
    redirect_uri: str


@router.post("/{connector_id}/authorize")
async def authorize(
    connector_id: str,
    body: AuthorizeRequest,
    principal: Principal = Depends(get_principal),
    registry=Depends(get_registry_service),
    grants=Depends(get_grant_service),
    identities=Depends(get_identity_service),
) -> Dict[str, Any]:
    identity = await ensure_identity(principal)
    connector = await registry.get_connector(connector_id)
    if not connector or not connector["is_enabled"]:
        raise HTTPException(status_code=404, detail="Connector unavailable")

    # API-key connectors are configured by an admin and have no per-user OAuth.
    if connector.get("auth_kind") == "api_key":
        raise HTTPException(
            status_code=400,
            detail="This integration does not use per-user sign-in.",
        )

    grant_ids = connector.get("group_grants") or []
    allowed, reason = await identities.can_use_connector(
        identity["id"], connector_id, group_grant_ids=grant_ids
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=f"Not entitled to connect ({reason})")

    if not await registry.has_client_secret(connector_id):
        raise HTTPException(status_code=409, detail="Connector is not fully configured by an admin")

    version = connector.get("current_version") or {}
    manifest = version.get("manifest") or {}
    config = manifest.get("config") or {}
    provider = get_provider(connector["provider"])
    if provider is None:
        raise HTTPException(status_code=500, detail="Provider adapter unavailable")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = oauth.generate_pkce()
    try:
        await grants.create_oauth_session(
            state=state,
            identity_id=identity["id"],
            connector_id=connector_id,
            connector_version_id=version["id"],
            redirect_uri=body.redirect_uri,
            code_verifier=verifier,
            oidc_nonce=nonce,
        )
        authorize_url = provider.authorize_url(
            config=config, manifest=manifest, redirect_uri=body.redirect_uri,
            state=state, code_challenge=challenge, nonce=nonce,
        )
    except GrantError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"authorize_url": authorize_url}


class CallbackRequest(BaseModel):
    code: str
    state: str


@router.post("/oauth/callback")
async def oauth_callback(
    body: CallbackRequest,
    principal: Principal = Depends(get_principal),
    registry=Depends(get_registry_service),
    grants=Depends(get_grant_service),
    identities=Depends(get_identity_service),
    audit=Depends(get_audit_service),
) -> Dict[str, Any]:
    # Bind the callback to the SAME logged-in identity that started the flow.
    # Prevents authorization-code injection / login-CSRF where a victim completes
    # an attacker-initiated (or vice-versa) flow and tokens land on the wrong id.
    caller = await ensure_identity(principal)
    session = await grants.consume_oauth_session(body.state)
    if not session:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in state")
    if str(session["identity_id"]) != str(caller["id"]):
        await audit.log(
            event_type="grant.rejected", actor_user_id=principal.user_id,
            identity_id=caller["id"], connector_id=session["connector_id"],
            outcome="state_identity_mismatch",
        )
        raise HTTPException(status_code=403, detail="Sign-in state does not belong to you")

    identity = caller

    connector = await registry.get_connector(session["connector_id"])
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    version = await registry.get_version(session["connector_version_id"])
    manifest = (version or {}).get("manifest") or {}
    config = manifest.get("config") or {}
    provider = get_provider(connector["provider"])
    if provider is None:
        raise HTTPException(status_code=500, detail="Provider adapter unavailable")

    try:
        client_secret = await registry.get_client_secret(session["connector_id"])
    except CryptoError as exc:
        # The stored secret exists but this deployment's APP_ENCRYPTION_KEY cannot
        # unwrap it (commonly two environments sharing one database). Retrying the
        # consent flow cannot fix it, so say so instead of returning a 500.
        logger.error("oauth callback: client secret is undecryptable: %s", exc)
        await audit.log(
            event_type="grant.failed", actor_user_id=principal.user_id,
            connector_id=session["connector_id"], outcome="client_secret_undecryptable",
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "This deployment cannot read its stored connector credentials. "
                "Ask an admin to check APP_ENCRYPTION_KEY."
            ),
        ) from exc
    try:
        token = await provider.exchange_code(
            config=config, manifest=manifest, client_secret=client_secret,
            code=body.code, redirect_uri=session["redirect_uri"],
            code_verifier=session["code_verifier"],
        )
    except Exception as exc:  # noqa: BLE001
        await audit.log(
            event_type="grant.failed", actor_user_id=principal.user_id,
            connector_id=session["connector_id"], outcome="token_exchange_error",
            detail={"error": str(exc)[:200]},
        )
        raise HTTPException(status_code=502, detail="Sign-in with the provider failed") from exc

    # Fail-closed identity binding: verify aud/tid/iss/nonce/exp and require the
    # returned object id to match the signed-in identity. Missing/invalid claims
    # (or a different mailbox) are rejected — no silent pass-through.
    import os as _os

    from src.config import settings as _settings

    expected_tenant = (
        (identity.get("tenant_id") or "").strip()
        or (_settings.CONNECTORS_TENANT_ID or "").strip()
        or (_os.getenv("AZURE_AD_TENANT_ID") or "").strip()
    )
    try:
        bound = provider.validate_and_bind(
            token,
            config=config,
            expected_nonce=session.get("oidc_nonce") or "",
            expected_tenant=expected_tenant,
            expected_object_id=identity["object_id"],
        )
    except ValueError as exc:
        await audit.log(
            event_type="grant.rejected", actor_user_id=principal.user_id,
            identity_id=identity["id"], connector_id=session["connector_id"],
            outcome="binding_rejected", detail={"reason": str(exc)[:200]},
        )
        raise HTTPException(
            status_code=409,
            detail="The connected account could not be verified against your identity.",
        ) from exc

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=int(token.expires_in)) if token.expires_in else None
    try:
        grant_id = await grants.upsert_grant(
            identity_id=identity["id"],
            connector_id=session["connector_id"],
            connector_version_id=session["connector_version_id"],
            external_account=bound.get("upn") or identity.get("upn"),
            scopes=token.scope,
            refresh_token=token.refresh_token,
            access_token=token.access_token,
            access_expires_at=expires_at,
        )
    except GrantError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    await audit.log(
        event_type="grant.created", actor_user_id=principal.user_id, actor_email=principal.email,
        identity_id=identity["id"], connector_id=session["connector_id"], grant_id=grant_id,
        outcome="connected", detail={"account_domain": (bound.get("upn") or "").split("@")[-1]},
    )
    return {"status": "connected", "connector_id": session["connector_id"]}


@router.post("/{connector_id}/revoke")
async def revoke(
    connector_id: str,
    principal: Principal = Depends(get_principal),
    grants=Depends(get_grant_service),
    audit=Depends(get_audit_service),
) -> Dict[str, Any]:
    identity = await ensure_identity(principal)
    grant = await grants.get_grant(identity["id"], connector_id)
    if not grant:
        raise HTTPException(status_code=404, detail="No connection to revoke")
    await grants.revoke_grant(grant["id"])
    await audit.log(
        event_type="grant.revoked", actor_user_id=principal.user_id,
        identity_id=identity["id"], connector_id=connector_id, grant_id=grant["id"],
        outcome="revoked",
    )
    return {"status": "revoked"}
