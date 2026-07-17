"""Server-authorized action gate with DB-enforced single execution.

Contract (plan):
  - The model may only PROPOSE a named action against an opaque result handle
    (a snapshot id). It never supplies recipients or authorizes execution.
  - The server collects + validates recipients, re-checks every policy and
    entitlement, renders the payload from the server-held snapshot, and executes
    atomically.
  - Single execution is enforced in the DB: a one-time nonce + a conditional
    ``pending -> attempted`` transition (row-version bump) writes a durable
    'attempted' record BEFORE the provider call. On an unknown outcome we do NOT
    retry (prevents duplicate sends).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from src.connectors.audit_service import AuditService, redact_recipients
from src.connectors.grant_service import GrantService
from src.connectors.identity_service import IdentityService
from src.connectors.providers import get_provider
from src.connectors.recipients import validate_recipients
from src.connectors.registry_service import ConnectorRegistryService
from src.connectors.snapshot_service import SnapshotService
from src.security.app_flags import get_connectors_enabled

logger = logging.getLogger(__name__)


class ActionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ActionGate:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        registry: ConnectorRegistryService,
        grants: GrantService,
        snapshots: SnapshotService,
        identities: IdentityService,
        audit: AuditService,
    ) -> None:
        self.pool = pool
        self.registry = registry
        self.grants = grants
        self.snapshots = snapshots
        self.identities = identities
        self.audit = audit

    # ── Propose (model / UI initiates) ──────────────────────────────────────
    async def propose(
        self,
        *,
        owner_user_id: str,
        identity_id: str,
        connector_id: str,
        action: str,
        snapshot_id: str,
        proposal_ttl: int = 900,
    ) -> Dict[str, Any]:
        connector = await self.registry.get_connector(connector_id)
        if not connector or not connector["is_enabled"]:
            raise ActionError("Connector unavailable", status_code=404)

        version = connector.get("current_version") or {}
        manifest = version.get("manifest") or {}
        action_names = {a.get("name") for a in manifest.get("actions", [])}
        if action not in action_names:
            raise ActionError(f"Unknown action {action!r} for this connector")

        # Entitlement (group gating + local exceptions, fail-closed on stale).
        grant_ids = await self.registry.list_group_grant_ids(connector_id)
        allowed, reason = await self.identities.can_use_connector(
            identity_id, connector_id, group_grant_ids=grant_ids
        )
        if not allowed:
            raise ActionError(f"Not entitled to use this connector ({reason})", status_code=403)

        grant = await self.grants.get_grant(identity_id, connector_id)
        if not grant or grant["status"] != "active":
            raise ActionError("Connect this integration before using it", status_code=409)

        snap = await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id)
        if not snap:
            raise ActionError("Result is no longer available to export", status_code=404)

        proposal_id = uuid.uuid4()
        nonce = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=proposal_ttl)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_action_proposals
                    (id, nonce, owner_user_id, identity_id, connector_id,
                     connector_version_id, grant_id, snapshot_id, action, params, status, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'{}'::jsonb,'pending',$10)
                """,
                proposal_id, nonce, owner_user_id, identity_id, connector_id,
                version.get("id"), grant["id"], snapshot_id, action, expires,
            )
        await self.audit.log(
            event_type="action.proposed",
            actor_user_id=owner_user_id,
            identity_id=identity_id,
            connector_id=connector_id,
            grant_id=grant["id"],
            proposal_id=str(proposal_id),
            snapshot_id=snapshot_id,
            outcome="pending",
            detail={"action": action},
        )
        return {
            "proposal_id": str(proposal_id),
            "nonce": nonce,
            "action": action,
            "connector": {"id": connector["id"], "display_name": connector["display_name"]},
            "snapshot": snap,
            "config": manifest.get("config", {}),
            "expires_at": expires.isoformat(),
        }

    # ── Preview (server-derived confirmation; no side effects) ──────────────
    async def preview(
        self,
        *,
        proposal_id: str,
        nonce: str,
        owner_user_id: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return a server-derived confirmation summary WITHOUT executing.

        Validates ownership, the confirmation nonce, proposal state, entitlement,
        and the recipient policy, then reports the normalized recipients, which of
        them are external, and the exact server-held result being exported. The
        browser must present this (with the external-recipient warning) and only
        then call ``execute``. This is a read-only step — it never claims the
        one-time nonce or mutates the proposal.
        """
        if not await get_connectors_enabled():
            raise ActionError("Connectors feature is disabled", status_code=404)

        prow = await self._load_proposal(proposal_id)
        if not prow or prow["owner_user_id"] != owner_user_id:
            raise ActionError("Proposal not found", status_code=404)
        if not secrets.compare_digest(prow["nonce"], nonce):
            raise ActionError("Invalid confirmation token", status_code=403)
        if prow["status"] != "pending":
            raise ActionError("This action was already submitted", status_code=409)
        if prow["expires_at"] and prow["expires_at"] <= datetime.now(timezone.utc):
            raise ActionError("This action request has expired", status_code=410)

        connector_id = str(prow["connector_id"])
        identity_id = str(prow["identity_id"])
        grant_id = str(prow["grant_id"]) if prow["grant_id"] else None

        connector = await self.registry.get_connector(connector_id)
        if not connector or not connector["is_enabled"]:
            raise ActionError("Connector unavailable", status_code=404)
        version_id = str(prow["connector_version_id"]) if prow["connector_version_id"] else None
        pinned = await self.registry.get_version(version_id) if version_id else None
        if pinned is None:
            pinned = connector.get("current_version") or {}
        manifest = pinned.get("manifest") or {}
        config = manifest.get("config", {})

        grant_ids = await self.registry.list_group_grant_ids(connector_id)
        allowed, reason = await self.identities.can_use_connector(
            identity_id, connector_id, group_grant_ids=grant_ids
        )
        if not allowed:
            raise ActionError(f"Not entitled to use this connector ({reason})", status_code=403)
        grant = await self.grants.get_grant_by_id(grant_id) if grant_id else None
        if not grant or grant["status"] != "active":
            raise ActionError("Integration is not connected", status_code=409)

        snapshot_id = str(prow["snapshot_id"]) if prow["snapshot_id"] else None
        snap = await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id) if snapshot_id else None
        if not snap:
            raise ActionError("Result is no longer available to export", status_code=410)

        validated = await self._validate_params(
            action=prow["action"], params=params, config=config, grant=grant
        )
        external = validated.get("_external", [])
        return {
            "proposal_id": proposal_id,
            "action": prow["action"],
            "connector": {"id": connector["id"], "display_name": connector["display_name"]},
            "sender": grant.get("external_account"),
            "recipients": validated.get("recipients", []),
            "external_recipients": external,
            "has_external": bool(external),
            "subject": validated.get("subject"),
            "note": validated.get("note"),
            "snapshot": {
                "row_count": snap.get("row_count"),
                "columns": snap.get("columns"),
                "payload_hash": snap.get("payload_hash"),
            },
            "expires_at": prow["expires_at"].isoformat() if prow["expires_at"] else None,
        }

    # ── Execute (server re-validates + runs once) ───────────────────────────
    async def execute(
        self,
        *,
        proposal_id: str,
        nonce: str,
        owner_user_id: str,
        actor_email: Optional[str],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not await get_connectors_enabled():
            raise ActionError("Connectors feature is disabled", status_code=404)

        prow = await self._load_proposal(proposal_id)
        if not prow or prow["owner_user_id"] != owner_user_id:
            raise ActionError("Proposal not found", status_code=404)
        if not secrets.compare_digest(prow["nonce"], nonce):
            raise ActionError("Invalid confirmation token", status_code=403)
        if prow["status"] != "pending":
            raise ActionError("This action was already submitted", status_code=409)
        if prow["expires_at"] and prow["expires_at"] <= datetime.now(timezone.utc):
            raise ActionError("This action request has expired", status_code=410)

        connector_id = str(prow["connector_id"])
        identity_id = str(prow["identity_id"])
        grant_id = str(prow["grant_id"]) if prow["grant_id"] else None

        connector = await self.registry.get_connector(connector_id)
        if not connector or not connector["is_enabled"]:
            raise ActionError("Connector unavailable", status_code=404)

        # Execute against the IMMUTABLE version pinned at propose time — not the
        # connector's current version — so an admin editing config/scopes between
        # propose and execute cannot alter what the user confirmed (TOCTOU).
        version_id = str(prow["connector_version_id"]) if prow["connector_version_id"] else None
        pinned = await self.registry.get_version(version_id) if version_id else None
        if pinned is None:
            pinned = connector.get("current_version") or {}
        manifest = pinned.get("manifest") or {}
        config = manifest.get("config", {})

        # Re-check entitlement + grant at execute time (defense in depth).
        grant_ids = await self.registry.list_group_grant_ids(connector_id)
        allowed, reason = await self.identities.can_use_connector(
            identity_id, connector_id, group_grant_ids=grant_ids
        )
        if not allowed:
            raise ActionError(f"Not entitled to use this connector ({reason})", status_code=403)
        grant = await self.grants.get_grant_by_id(grant_id) if grant_id else None
        if not grant or grant["status"] != "active":
            raise ActionError("Integration is not connected", status_code=409)

        snapshot_id = str(prow["snapshot_id"]) if prow["snapshot_id"] else None
        snap_payload = (
            await self.snapshots.get_payload(snapshot_id, owner_user_id=owner_user_id)
            if snapshot_id else None
        )
        if snap_payload is None:
            raise ActionError("Result is no longer available to export", status_code=410)

        # Validate + normalize params on the server (never from the model).
        validated_params = await self._validate_params(
            action=prow["action"], params=params, config=config, grant=grant
        )

        confirmation = {
            "action": prow["action"],
            "connector_id": connector_id,
            "connector_version_id": str(prow["connector_version_id"]),
            "snapshot_id": snapshot_id,
            "snapshot_hash": (await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id) or {}).get("payload_hash"),
            "recipients": sorted(validated_params.get("recipients", [])),
            "subject": validated_params.get("subject"),
        }
        confirmation_hash = _hash(_canonical(confirmation))

        # Atomic single-execution transition: pending -> attempted. Writes the
        # durable 'attempted' record BEFORE any provider call.
        claimed = await self._claim_for_execution(
            proposal_id=proposal_id,
            confirmation_hash=confirmation_hash,
            params=validated_params,
        )
        if not claimed:
            raise ActionError("This action was already submitted", status_code=409)

        await self.audit.log(
            event_type="action.attempted",
            actor_user_id=owner_user_id,
            actor_email=actor_email,
            identity_id=identity_id,
            connector_id=connector_id,
            grant_id=grant_id,
            proposal_id=proposal_id,
            snapshot_id=snapshot_id,
            outcome="attempted",
            detail={
                "action": prow["action"],
                "recipients": redact_recipients(validated_params.get("recipients", [])),
                "external_count": len(validated_params.get("_external", [])),
            },
        )

        # Obtain a usable access token (refresh if needed).
        try:
            access_token = await self._ensure_access_token(
                connector_id=connector_id, grant_id=grant_id, manifest=manifest, config=config
            )
        except Exception as exc:  # noqa: BLE001
            await self._finalize(proposal_id, status="failed", error=f"token: {exc}")
            await self.audit.log(
                event_type="action.failed", actor_user_id=owner_user_id, proposal_id=proposal_id,
                connector_id=connector_id, outcome="token_error", detail={"error": str(exc)[:200]},
            )
            raise ActionError("Could not obtain authorization for your account. Reconnect and retry.", status_code=502)

        provider = get_provider(connector["provider"])
        if provider is None:
            await self._finalize(proposal_id, status="failed", error="no provider adapter")
            raise ActionError("Connector provider unavailable", status_code=500)

        try:
            result = await provider.execute(
                action=prow["action"],
                params=validated_params,
                snapshot_payload=snap_payload,
                access_token=access_token,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - unknown outcome: do NOT retry
            await self._finalize(proposal_id, status="failed", error=str(exc)[:500])
            await self.audit.log(
                event_type="action.failed", actor_user_id=owner_user_id, proposal_id=proposal_id,
                connector_id=connector_id, grant_id=grant_id, outcome="provider_error",
                detail={"error": str(exc)[:200]},
            )
            raise ActionError("The action could not be completed. It was not retried to avoid duplicates.", status_code=502)

        accepted = bool(result.get("accepted"))
        await self._finalize(
            proposal_id,
            status="succeeded" if accepted else "failed",
            provider_result={"status_code": result.get("status_code"), "accepted": accepted},
        )
        await self.grants.touch_used(grant_id)
        await self.audit.log(
            event_type="action.succeeded" if accepted else "action.failed",
            actor_user_id=owner_user_id, actor_email=actor_email, identity_id=identity_id,
            connector_id=connector_id, grant_id=grant_id, proposal_id=proposal_id,
            snapshot_id=snapshot_id, outcome="accepted" if accepted else "rejected",
            detail={"status_code": result.get("status_code")},
        )
        return {
            "proposal_id": proposal_id,
            "status": "accepted" if accepted else "failed",
            "accepted": accepted,
            "message": (
                "Accepted for delivery (202)." if accepted
                else f"Provider returned {result.get('status_code')}."
            ),
        }

    # ── internals ────────────────────────────────────────────────────────────

    async def _validate_params(
        self, *, action: str, params: Dict[str, Any], config: Dict[str, Any], grant: Dict[str, Any]
    ) -> Dict[str, Any]:
        if action != "send_email":
            raise ActionError(f"Unsupported action {action!r}")
        subject = (params.get("subject") or "").strip()
        if not subject:
            raise ActionError("Subject is required")
        note = (params.get("note") or "").strip()
        raw_recipients = params.get("recipients") or []
        if isinstance(raw_recipients, str):
            raw_recipients = [r for r in raw_recipients.replace(";", ",").split(",")]
        if not raw_recipients:
            raise ActionError("At least one recipient is required")

        sender = (grant.get("external_account") or "").strip().lower()
        sender_domain = sender.split("@", 1)[1] if "@" in sender else ""
        vr = validate_recipients(
            [str(r) for r in raw_recipients],
            sender_domain=sender_domain,
            allowlist=config.get("recipient_domain_allowlist") or [],
            allow_external=bool(config.get("allow_external_recipients")),
        )
        if vr.invalid:
            raise ActionError(f"Invalid recipient(s): {', '.join(vr.invalid[:5])}")
        if vr.rejected:
            raise ActionError(
                f"Recipient domain not allowed by policy: {', '.join(vr.rejected[:5])}",
                status_code=403,
            )
        if not vr.valid:
            raise ActionError("No valid recipients")
        return {
            "recipients": vr.valid,
            "subject": subject[:200],
            "note": note[:2000],
            "_external": vr.external,
        }

    async def _ensure_access_token(
        self, *, connector_id: str, grant_id: str, manifest: Dict[str, Any], config: Dict[str, Any]
    ) -> str:
        current = await self.grants.get_access_token(grant_id)
        now = datetime.now(timezone.utc)
        if current and current.get("value"):
            exp = current.get("expires_at")
            if exp is None or exp > now + timedelta(seconds=60):
                return current["value"]

        refresh = await self.grants.get_refresh_token(grant_id)
        if not refresh:
            raise RuntimeError("no refresh token; reconnect required")
        provider = get_provider(manifest.get("provider"))
        if provider is None:
            raise RuntimeError("provider adapter missing")
        client_secret = await self.registry.get_client_secret(connector_id)
        token = await provider.refresh(
            config=config, manifest=manifest, client_secret=client_secret, refresh_token=refresh
        )
        expires_at = None
        if token.expires_in:
            expires_at = now + timedelta(seconds=int(token.expires_in))
        await self.grants.store_access_token(grant_id, token.access_token, expires_at)
        return token.access_token

    async def _load_proposal(self, proposal_id: str) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM connector_action_proposals WHERE id=$1", proposal_id
            )

    async def _claim_for_execution(
        self, *, proposal_id: str, confirmation_hash: str, params: Dict[str, Any]
    ) -> bool:
        # Strip internal-only keys before persisting params.
        persist = {k: v for k, v in params.items() if not k.startswith("_")}
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE connector_action_proposals
                   SET status='attempted',
                       nonce_used_at=NOW(),
                       attempted_at=NOW(),
                       row_version=row_version+1,
                       confirmation_hash=$2,
                       params=$3::jsonb
                 WHERE id=$1 AND status='pending' AND nonce_used_at IS NULL
                RETURNING id
                """,
                proposal_id, confirmation_hash, json.dumps(persist),
            )
        return row is not None

    async def _finalize(
        self,
        proposal_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        provider_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE connector_action_proposals
                   SET status=$2, completed_at=NOW(), error=$3,
                       provider_result=$4::jsonb, row_version=row_version+1
                 WHERE id=$1
                """,
                proposal_id, status, error,
                json.dumps(provider_result) if provider_result is not None else None,
            )
