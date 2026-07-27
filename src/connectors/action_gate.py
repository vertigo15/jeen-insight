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

from src.connectors.action_policy import (
    ACTION_POLICY_VERSION,
    ActionPolicyError,
    get_action_policy,
    scopes_satisfied,
)
from src.connectors.audit_service import AuditService, redact_recipients
from src.connectors.grant_service import GrantService
from src.connectors.identity_service import IdentityService
from src.connectors.providers import get_provider
from src.connectors.rate_limiter import RateLimiter
from src.connectors.registry_service import ConnectorRegistryService
from src.connectors.snapshot_service import SnapshotService
from src.connectors.tool_result_service import ToolResultService
from src.security.app_flags import get_agent_tools_enabled, get_connectors_enabled

# Distributed caps (per fixed window). Coarse but cross-replica.
_PROPOSE_PER_USER_PER_MIN = 20
_EXEC_PER_USER_PER_MIN = 10
_EXEC_PER_CONNECTOR_PER_MIN = 30
_EXEC_PER_USER_PER_DAY = 200

logger = logging.getLogger(__name__)


class ActionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _approval_hash(
    *,
    action: str,
    connector_id: Any,
    connector_version_id: Any,
    snapshot_id: Any,
    snapshot_hash: Optional[str],
    params: Dict[str, Any],
) -> str:
    """Bind the exact approved payload + immutable context into one hash.

    Computed at preview time (persisted) and recomputed at execute time from the
    STORED approved params. A mismatch means the pinned version, snapshot, policy
    version or params changed between approval and execution, so execution is
    refused. Internal-only params (``_``-prefixed) are excluded so the hash is
    stable across validate() calls.
    """
    approval = {
        "action": action,
        "connector_id": str(connector_id),
        "connector_version_id": str(connector_version_id) if connector_version_id else None,
        "snapshot_id": str(snapshot_id) if snapshot_id else None,
        "snapshot_hash": snapshot_hash,
        "params": {k: v for k, v in (params or {}).items() if not str(k).startswith("_")},
        "policy_version": ACTION_POLICY_VERSION,
    }
    return _hash(_canonical(approval))


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
        tool_results: Optional[ToolResultService] = None,
        rate_limiter: Optional["RateLimiter"] = None,
    ) -> None:
        self.pool = pool
        self.registry = registry
        self.grants = grants
        self.snapshots = snapshots
        self.identities = identities
        self.audit = audit
        self.tool_results = tool_results
        self.rate = rate_limiter

    # ── Propose (model / UI initiates) ──────────────────────────────────────
    async def propose(
        self,
        *,
        owner_user_id: str,
        identity_id: str,
        connector_id: str,
        action: str,
        snapshot_id: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        origin: str = "user",
        proposal_ttl: int = 900,
    ) -> Dict[str, Any]:
        origin = "agent" if str(origin) == "agent" else "user"
        # Agent-originated proposals require BOTH master switches, read fresh so a
        # disable takes effect immediately (not after the flag cache TTL).
        if origin == "agent":
            if not (
                await get_connectors_enabled(use_cache=False)
                and await get_agent_tools_enabled(use_cache=False)
            ):
                raise ActionError("Agent tool-calling is disabled", status_code=403)

        # Distributed proposal cap (per user / minute) — cross-replica.
        if not await self._rate_ok(
            f"propose:user:{owner_user_id}", limit=_PROPOSE_PER_USER_PER_MIN, window=60
        ):
            raise ActionError("Too many action requests. Please slow down.", status_code=429)

        connector = await self.registry.get_connector(connector_id)
        if not connector or not connector["is_enabled"]:
            raise ActionError("Connector unavailable", status_code=404)

        # The typed, server-owned policy is the authority for what this action
        # requires — never a manifest value or a caller-supplied field. Unknown
        # (connector, action) pairs fail closed.
        policy = get_action_policy(connector["key"], action)
        if policy is None:
            raise ActionError(f"Action {action!r} is not permitted for this connector", status_code=403)

        version = connector.get("current_version") or {}
        manifest = version.get("manifest") or {}

        # Entitlement (group gating + local exceptions, fail-closed on stale).
        grant_ids = await self.registry.list_group_grant_ids(connector_id)
        allowed, reason = await self.identities.can_use_connector(
            identity_id, connector_id, group_grant_ids=grant_ids
        )
        if not allowed:
            raise ActionError(f"Not entitled to use this connector ({reason})", status_code=403)

        grant = None
        grant_id = None
        if policy.requires_grant:
            grant = await self.grants.get_grant(identity_id, connector_id)
            if not grant or grant["status"] != "active":
                raise ActionError("Connect this integration before using it", status_code=409)
            grant_id = grant["id"]

        snap = None
        if policy.requires_snapshot:
            if not snapshot_id:
                raise ActionError("This action requires a result to act on", status_code=400)
            snap = await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id)
            if not snap:
                raise ActionError("Result is no longer available to export", status_code=404)
        elif snapshot_id:
            # A snapshot was offered for an action that doesn't need one; only
            # accept it if it genuinely belongs to this owner (else drop it).
            snap = await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id)
            snapshot_id = snap["id"] if snap else None

        # Draft params (e.g. planner-suggested args) are stored for the confirm
        # card to prefill; they are re-validated + approved at preview time.
        draft = {k: v for k, v in (params or {}).items() if not str(k).startswith("_")}

        proposal_id = uuid.uuid4()
        nonce = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=proposal_ttl)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_action_proposals
                    (id, nonce, owner_user_id, identity_id, connector_id,
                     connector_version_id, grant_id, snapshot_id, action, params,
                     origin, status, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,'pending',$12)
                """,
                proposal_id, nonce, owner_user_id, identity_id, connector_id,
                version.get("id"), grant_id, snapshot_id, action, json.dumps(draft),
                origin, expires,
            )
        await self.audit.log(
            event_type="action.proposed",
            actor_user_id=owner_user_id,
            identity_id=identity_id,
            connector_id=connector_id,
            grant_id=grant_id,
            proposal_id=str(proposal_id),
            snapshot_id=snapshot_id,
            outcome="pending",
            detail={"action": action, "origin": origin},
        )
        return {
            "proposal_id": str(proposal_id),
            "nonce": nonce,
            "action": action,
            "origin": origin,
            "connector": {"id": connector["id"], "display_name": connector["display_name"]},
            "snapshot": snap,
            "params": draft,
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
        # Preview may run while pending (first look) or confirmed (re-preview after
        # an edit); once claimed for execution it is immutable.
        if prow["status"] not in ("pending", "confirmed"):
            raise ActionError("This action was already submitted", status_code=409)
        if prow["expires_at"] and prow["expires_at"] <= datetime.now(timezone.utc):
            raise ActionError("This action request has expired", status_code=410)

        connector_id = str(prow["connector_id"])
        identity_id = str(prow["identity_id"])
        grant_id = str(prow["grant_id"]) if prow["grant_id"] else None

        connector = await self.registry.get_connector(connector_id)
        if not connector or not connector["is_enabled"]:
            raise ActionError("Connector unavailable", status_code=404)
        policy = get_action_policy(connector["key"], prow["action"])
        if policy is None:
            raise ActionError("Action is not permitted for this connector", status_code=403)

        version_id = str(prow["connector_version_id"]) if prow["connector_version_id"] else None
        pinned = await self.registry.get_version(version_id) if version_id else None
        # Never fall back to the connector's current version: execute what the
        # user is about to confirm must be the exact pinned version.
        if version_id and pinned is None:
            raise ActionError("This integration changed; start the action again.", status_code=409)
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

        grant = None
        if policy.requires_grant:
            grant = await self.grants.get_grant_by_id(grant_id) if grant_id else None
            if not grant or grant["status"] != "active":
                raise ActionError("Integration is not connected", status_code=409)

        snapshot_id = str(prow["snapshot_id"]) if prow["snapshot_id"] else None
        snap = (
            await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id)
            if snapshot_id else None
        )
        if policy.requires_snapshot and not snap:
            raise ActionError("Result is no longer available to export", status_code=410)

        # Merge stored draft (e.g. planner-suggested args) with any client edits,
        # then validate on the server. Draft supplies defaults; explicit non-None
        # client values override.
        stored_draft = self._json(prow["params"])
        effective = {**stored_draft, **{k: v for k, v in (params or {}).items() if v is not None}}
        validated = await self._validate_params(
            connector_key=connector["key"], action=prow["action"],
            params=effective, config=config, grant=grant,
        )

        # Persist the server-approved params + a hash binding them to the pinned
        # version + snapshot. Execute runs ONLY these stored params.
        persist = {k: v for k, v in validated.items() if not str(k).startswith("_")}
        approval_hash = _approval_hash(
            action=prow["action"], connector_id=connector_id,
            connector_version_id=version_id, snapshot_id=snapshot_id,
            snapshot_hash=snap.get("payload_hash") if snap else None, params=persist,
        )
        await self._persist_approval(proposal_id, params=persist, approval_hash=approval_hash)

        external = validated.get("_external", [])
        return {
            "proposal_id": proposal_id,
            "action": prow["action"],
            "connector": {"id": connector["id"], "display_name": connector["display_name"]},
            "sender": grant.get("external_account") if grant else None,
            "recipients": validated.get("recipients", []),
            "external_recipients": external,
            "has_external": bool(external),
            "subject": validated.get("subject"),
            "note": validated.get("note"),
            # Generic, server-normalized approved params (safe to display) so the
            # confirm card can summarize non-email actions (Slack channel, Jira
            # project/type/summary). Internal-only keys (prefixed '_') are excluded.
            "params": persist,
            "snapshot": {
                "row_count": snap.get("row_count"),
                "columns": snap.get("columns"),
                "payload_hash": snap.get("payload_hash"),
            } if snap else None,
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
        # Server-enforced approval: only a previewed+approved proposal ('confirmed')
        # may execute, and only its STORED approved params run.
        if prow["status"] == "pending":
            raise ActionError("Preview and confirm this action first", status_code=428)
        if prow["status"] != "confirmed":
            raise ActionError("This action was already submitted", status_code=409)
        if prow["expires_at"] and prow["expires_at"] <= datetime.now(timezone.utc):
            raise ActionError("This action request has expired", status_code=410)

        # Agent-originated executions require BOTH switches, read fresh so a
        # disable is an immediate kill switch.
        if str(prow["origin"]) == "agent":
            if not (
                await get_connectors_enabled(use_cache=False)
                and await get_agent_tools_enabled(use_cache=False)
            ):
                raise ActionError("Agent tool-calling is disabled", status_code=403)

        connector_id = str(prow["connector_id"])
        identity_id = str(prow["identity_id"])
        grant_id = str(prow["grant_id"]) if prow["grant_id"] else None

        connector = await self.registry.get_connector(connector_id)
        if not connector or not connector["is_enabled"]:
            raise ActionError("Connector unavailable", status_code=404)
        policy = get_action_policy(connector["key"], prow["action"])
        if policy is None:
            raise ActionError("Action is not permitted for this connector", status_code=403)

        # Execute against the IMMUTABLE version pinned at propose time — not the
        # connector's current version — so an admin editing config/scopes between
        # propose and execute cannot alter what the user confirmed (TOCTOU). A
        # pinned version that no longer exists is a hard failure (never fall back).
        version_id = str(prow["connector_version_id"]) if prow["connector_version_id"] else None
        pinned = await self.registry.get_version(version_id) if version_id else None
        if version_id and pinned is None:
            raise ActionError("This integration changed; start the action again.", status_code=409)
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
        grant = None
        if policy.requires_grant:
            grant = await self.grants.get_grant_by_id(grant_id) if grant_id else None
            if not grant or grant["status"] != "active":
                raise ActionError("Integration is not connected", status_code=409)
            # Per-action OAuth scope check (defense in depth; runs before the
            # single-execution claim so a missing scope never consumes the nonce).
            if not scopes_satisfied(policy.required_scopes, grant.get("scopes")):
                raise ActionError(
                    "Your connection is missing a required permission; reconnect it.",
                    status_code=403,
                )

        snapshot_id = str(prow["snapshot_id"]) if prow["snapshot_id"] else None
        snap_meta = (
            await self.snapshots.get_meta(snapshot_id, owner_user_id=owner_user_id)
            if snapshot_id else None
        )
        snap_payload = (
            await self.snapshots.get_payload(snapshot_id, owner_user_id=owner_user_id)
            if snapshot_id else None
        )
        if policy.requires_snapshot and (snap_meta is None or snap_payload is None):
            raise ActionError("Result is no longer available to export", status_code=410)

        # Run ONLY the server-approved params stored at preview — never client- or
        # model-supplied params at execute time. Re-validate (defense in depth) and
        # require the approval hash to still bind the pinned version + snapshot.
        stored = self._json(prow["params"])
        validated_params = await self._validate_params(
            connector_key=connector["key"], action=prow["action"],
            params=stored, config=config, grant=grant,
        )
        recomputed = _approval_hash(
            action=prow["action"], connector_id=connector_id,
            connector_version_id=version_id, snapshot_id=snapshot_id,
            snapshot_hash=snap_meta.get("payload_hash") if snap_meta else None,
            params={k: v for k, v in validated_params.items() if not str(k).startswith("_")},
        )
        if not prow["confirmation_hash"] or not secrets.compare_digest(
            str(prow["confirmation_hash"]), recomputed
        ):
            raise ActionError(
                "This action changed since you approved it; review and try again.",
                status_code=409,
            )

        # Distributed execution caps: per-user/min, per-connector/min, per-user/day.
        for key, limit, window in (
            (f"exec:user:{owner_user_id}", _EXEC_PER_USER_PER_MIN, 60),
            (f"exec:conn:{connector_id}", _EXEC_PER_CONNECTOR_PER_MIN, 60),
            (f"exec:user:{owner_user_id}:day", _EXEC_PER_USER_PER_DAY, 86400),
        ):
            if not await self._rate_ok(key, limit=limit, window=window):
                raise ActionError("Action rate/budget exceeded. Try again later.", status_code=429)

        # Atomic single-execution transition: confirmed -> attempted, writing the
        # durable 'attempted' audit record in the SAME transaction (so a claimed
        # execution ALWAYS has an audit trail — not best-effort).
        claimed = await self._claim_for_execution(
            proposal_id=proposal_id,
            audit={
                "event_type": "action.attempted",
                "actor_user_id": owner_user_id,
                "actor_email": actor_email,
                "identity_id": identity_id,
                "connector_id": connector_id,
                "grant_id": grant_id,
                "proposal_id": proposal_id,
                "snapshot_id": snapshot_id,
                "outcome": "attempted",
                "detail": {
                    "action": prow["action"],
                    "origin": str(prow["origin"]),
                    "recipients": redact_recipients(validated_params.get("recipients", [])),
                    "external_count": len(validated_params.get("_external", [])),
                },
            },
        )
        if not claimed:
            raise ActionError("This action was already submitted", status_code=409)

        provider = get_provider(connector["provider"])
        if provider is None:
            await self._finalize(proposal_id, status="failed", error="no provider adapter")
            raise ActionError("Connector provider unavailable", status_code=500)

        # Select the credential per the server-owned policy auth model: an OAuth
        # delegated token (refreshed if needed) or an admin-stored API key.
        access_token: Optional[str] = None
        api_key: Optional[str] = None
        if policy.auth_kind == "oauth":
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
        else:  # api_key
            api_key = await self.registry.get_api_key(connector_id)
            if not api_key:
                await self._finalize(proposal_id, status="failed", error="no api key configured")
                raise ActionError("This integration is not fully configured.", status_code=409)

        try:
            result = await provider.execute(
                action=prow["action"],
                params=validated_params,
                snapshot_payload=snap_payload,
                config=config,
                access_token=access_token,
                api_key=api_key,
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

        # ── READ tools: capture untrusted data into an encrypted TTL artifact ──
        # and return a continuation ref. Reads have no external side effect, so a
        # failed read is SAFE to re-propose (unlike writes, which never retry).
        if policy.is_read:
            if not accepted:
                await self._finalize(
                    proposal_id, status="failed",
                    provider_result={"status_code": result.get("status_code"), "accepted": False},
                )
                await self.audit.log(
                    event_type="action.failed", actor_user_id=owner_user_id, proposal_id=proposal_id,
                    connector_id=connector_id, outcome="read_failed",
                    detail={"status_code": result.get("status_code")},
                )
                raise ActionError(
                    "The read tool returned no data. You can try again.", status_code=502
                )
            if self.tool_results is None:
                await self._finalize(proposal_id, status="failed", error="no artifact store")
                raise ActionError("Read tools are not available.", status_code=500)
            artifact = await self.tool_results.store(
                proposal_id=proposal_id,
                owner_user_id=owner_user_id,
                identity_id=identity_id,
                session_id=None,
                connector_version_id=version_id,
                payload=result.get("data") or {},
            )
            await self._finalize(
                proposal_id, status="succeeded",
                provider_result={"status_code": result.get("status_code"), "accepted": True},
            )
            await self.audit.log(
                event_type="action.succeeded", actor_user_id=owner_user_id, actor_email=actor_email,
                identity_id=identity_id, connector_id=connector_id, proposal_id=proposal_id,
                outcome="read_captured", detail={"artifact": bool(artifact)},
            )
            return {
                "proposal_id": proposal_id,
                "status": "accepted",
                "accepted": True,
                "kind": "read",
                "continuation": ({"artifact_id": artifact["id"]} if artifact else None),
                "message": "Search complete.",
            }

        # ── WRITE tools: finalize + audit; never retry on unknown outcome ──────
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
        self,
        *,
        connector_key: str,
        action: str,
        params: Dict[str, Any],
        config: Dict[str, Any],
        grant: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validate + normalize params via the typed, server-owned action policy.

        The policy (not a manifest value or caller field) decides how the action
        is validated. Unknown actions fail closed.
        """
        policy = get_action_policy(connector_key, action)
        if policy is None:
            raise ActionError(
                f"Action {action!r} is not permitted for this connector", status_code=403
            )
        try:
            return policy.validate(params or {}, config or {}, grant)
        except ActionPolicyError as exc:
            raise ActionError(str(exc)) from exc

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
        await self.grants.store_refreshed_tokens(
            grant_id,
            access_token=token.access_token,
            access_expires_at=expires_at,
            refresh_token=token.refresh_token,
        )
        return token.access_token

    @staticmethod
    def _json(value: Any) -> Dict[str, Any]:
        """Coerce a JSONB column (dict or text) into a plain dict."""
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except Exception:  # noqa: BLE001
                return {}
        return dict(value) if isinstance(value, dict) else {}

    async def _load_proposal(self, proposal_id: str) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM connector_action_proposals WHERE id=$1", proposal_id
            )

    async def _persist_approval(
        self, proposal_id: str, *, params: Dict[str, Any], approval_hash: str
    ) -> None:
        """Persist server-approved params + hash and mark the proposal 'confirmed'.

        Preview writes the exact payload the user is about to authorize; execute
        then runs ONLY these stored params. Allowed while pending or confirmed so a
        user can re-preview after editing.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE connector_action_proposals
                   SET params=$2::jsonb,
                       confirmation_hash=$3,
                       status='confirmed',
                       row_version=row_version+1
                 WHERE id=$1 AND status IN ('pending','confirmed')
                """,
                proposal_id, json.dumps(params), approval_hash,
            )

    async def _rate_ok(self, key: str, *, limit: int, window: int) -> bool:
        if self.rate is None:
            return True
        return await self.rate.allow(key, limit=limit, window_seconds=window)

    async def _claim_for_execution(
        self, *, proposal_id: str, audit: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Atomically transition confirmed -> attempted (single execution) AND write
        the durable 'attempted' audit in the SAME transaction.

        Params + confirmation_hash were persisted at preview; the claim only flips
        state and consumes the one-time nonce, so execute can never run twice. If
        the audit insert fails, the whole transition rolls back (guaranteeing an
        attempted execution is always auditable).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE connector_action_proposals
                       SET status='attempted',
                           nonce_used_at=NOW(),
                           attempted_at=NOW(),
                           row_version=row_version+1
                     WHERE id=$1 AND status='confirmed' AND nonce_used_at IS NULL
                    RETURNING id
                    """,
                    proposal_id,
                )
                if row is None:
                    return False
                if audit:
                    await AuditService.log_in_tx(conn, **audit)
        return True

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
