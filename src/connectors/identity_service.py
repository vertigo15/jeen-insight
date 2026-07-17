"""Canonical Entra identities, group membership cache, and entitlement resolution.

Design (plan Phase 1):
  - Identity is canonical + immutable, bound to ``(tenant_id, object_id)`` — never
    a mutable email. It links to at most one local ``auth_users`` row.
  - Group memberships are READ-ONLY / cache-derived from Entra (Graph or token
    claims). They are never manually mutated here.
  - Authorization is derived with explicit precedence:
        deny (local exception) > group role > local allow exception > base role
  - Membership that is stale or incomplete (overage/truncation) fails CLOSED for
    any group-derived allow; only explicit local allow exceptions bypass it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)

# Default max age for cached group membership. Overridable via
# CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS. Membership older than this (or
# incomplete) fails closed for group-based allows; local allow exceptions bypass.
MEMBERSHIP_MAX_AGE_SECONDS = 3600


def _membership_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS") or MEMBERSHIP_MAX_AGE_SECONDS))
    except (TypeError, ValueError):
        return MEMBERSHIP_MAX_AGE_SECONDS


_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


def _rank(role: str) -> int:
    return _ROLE_RANK.get(role, 0)


def _higher(a: str, b: str) -> str:
    return a if _rank(a) >= _rank(b) else b


class IdentityService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Identity lifecycle ─────────────────────────────────────────────────

    async def upsert_identity(
        self,
        *,
        tenant_id: str,
        object_id: str,
        upn: Optional[str] = None,
        display_name: Optional[str] = None,
        auth_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create or update the canonical identity for (tenant_id, object_id).

        Account linking rule: an identity links to at most one ``auth_users`` row
        (enforced by a UNIQUE constraint). We only set ``auth_user_id`` when it is
        currently NULL, so an existing safe link is never silently repointed.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO connector_identities
                    (tenant_id, object_id, upn, display_name, auth_user_id)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (tenant_id, object_id) DO UPDATE
                    SET upn = COALESCE(EXCLUDED.upn, connector_identities.upn),
                        display_name = COALESCE(EXCLUDED.display_name, connector_identities.display_name),
                        auth_user_id = COALESCE(connector_identities.auth_user_id, EXCLUDED.auth_user_id),
                        updated_at = NOW()
                RETURNING *
                """,
                tenant_id, object_id, upn, display_name, auth_user_id,
            )
        return self._identity_row(row)

    async def get_identity(self, identity_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM connector_identities WHERE id = $1", identity_id
            )
        return self._identity_row(row) if row else None

    async def get_by_auth_user(self, auth_user_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM connector_identities WHERE auth_user_id = $1",
                auth_user_id,
            )
        return self._identity_row(row) if row else None

    async def get_by_tenant_object(
        self, tenant_id: str, object_id: str
    ) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM connector_identities WHERE tenant_id=$1 AND object_id=$2",
                tenant_id, object_id,
            )
        return self._identity_row(row) if row else None

    # ── Group membership cache (read-only from Entra) ──────────────────────

    async def sync_memberships(
        self,
        identity_id: str,
        groups: List[Dict[str, str]],
        *,
        complete: bool = True,
        source: str = "graph",
        synced_at: Optional[datetime] = None,
    ) -> None:
        """Replace the cached membership for an identity.

        ``groups`` is a list of ``{"object_id": ..., "display_name": ...}``.
        Marks the sync freshness + completeness for fail-closed authz.

        ``synced_at`` is the *authoritative as-of* time for this data. For token-
        sourced claims pass the interactive login time so the freshness window is
        measured from login (not from this write) — otherwise re-stamping on every
        request would keep stale claims perpetually "fresh" and group removals
        would never take effect. Defaults to NOW() (correct for live Graph reads).
        """
        stamp = synced_at or datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                ident = await conn.fetchrow(
                    "SELECT tenant_id FROM connector_identities WHERE id = $1",
                    identity_id,
                )
                if not ident:
                    raise ValueError(f"Unknown identity {identity_id}")
                tenant_id = ident["tenant_id"]

                await conn.execute(
                    "DELETE FROM connector_identity_groups WHERE identity_id = $1",
                    identity_id,
                )
                for g in groups:
                    oid = (g.get("object_id") or "").strip()
                    if not oid:
                        continue
                    grp = await conn.fetchrow(
                        """
                        INSERT INTO connector_group_dir (tenant_id, object_id, display_name)
                        VALUES ($1,$2,$3)
                        ON CONFLICT (tenant_id, object_id) DO UPDATE
                            SET display_name = COALESCE(EXCLUDED.display_name, connector_group_dir.display_name),
                                updated_at = NOW()
                        RETURNING id
                        """,
                        tenant_id, oid, g.get("display_name"),
                    )
                    await conn.execute(
                        """
                        INSERT INTO connector_identity_groups (identity_id, group_id)
                        VALUES ($1,$2) ON CONFLICT DO NOTHING
                        """,
                        identity_id, grp["id"],
                    )
                await conn.execute(
                    """
                    INSERT INTO connector_membership_sync (identity_id, synced_at, source, complete)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (identity_id) DO UPDATE
                        SET synced_at = EXCLUDED.synced_at,
                            source = EXCLUDED.source,
                            complete = EXCLUDED.complete
                    """,
                    identity_id, stamp, source, complete,
                )

    async def get_membership(self, identity_id: str) -> Dict[str, Any]:
        """Return group object ids + freshness for an identity."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.object_id, d.display_name
                  FROM connector_identity_groups ig
                  JOIN connector_group_dir d ON d.id = ig.group_id
                 WHERE ig.identity_id = $1
                """,
                identity_id,
            )
            sync = await conn.fetchrow(
                "SELECT synced_at, source, complete FROM connector_membership_sync WHERE identity_id=$1",
                identity_id,
            )
        groups = [{"object_id": r["object_id"], "display_name": r["display_name"]} for r in rows]
        fresh = self._is_fresh(sync)
        return {
            "groups": groups,
            "group_ids": [g["object_id"] for g in groups],
            "synced_at": sync["synced_at"].isoformat() if sync and sync["synced_at"] else None,
            "source": sync["source"] if sync else None,
            "complete": bool(sync["complete"]) if sync else False,
            "fresh": fresh,
        }

    async def maybe_refresh_from_graph(self, identity: Dict[str, Any], graph) -> None:
        """Authoritatively refresh membership from Graph when the cache is stale.

        Best-effort: when app-only Graph is available and the last *authoritative*
        (graph-sourced) sync is older than the TTL, re-read transitive membership
        and overwrite the cache with source='graph', synced_at=NOW(). On any error
        (no creds, missing permission, transient failure) we leave the existing
        login-bounded token cache in place — which still ages out via the TTL, so
        we never fail *open*.
        """
        if graph is None or not getattr(graph, "available", lambda: False)():
            return
        object_id = identity.get("object_id")
        if not object_id:
            return
        async with self.pool.acquire() as conn:
            sync = await conn.fetchrow(
                "SELECT synced_at, source, complete FROM connector_membership_sync WHERE identity_id=$1",
                identity["id"],
            )
        # Skip if we already have fresh, authoritative (graph) data.
        if sync and sync["source"] == "graph" and self._is_fresh(sync):
            return
        try:
            group_ids, complete = await graph.member_group_ids(object_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("graph membership refresh skipped for %s: %s", identity["id"], exc)
            return
        await self.sync_memberships(
            identity["id"],
            [{"object_id": gid, "display_name": None} for gid in group_ids],
            complete=complete,
            source="graph",
        )

    def _is_fresh(self, sync: Optional[asyncpg.Record]) -> bool:
        if not sync or not sync["synced_at"]:
            return False
        if not sync["complete"]:
            return False
        age = datetime.now(timezone.utc) - sync["synced_at"]
        return age <= timedelta(seconds=_membership_ttl_seconds())

    # ── group -> role mapping ──────────────────────────────────────────────

    async def set_group_role(
        self, *, tenant_id: str, group_object_id: str, role: str, created_by: Optional[str]
    ) -> Dict[str, Any]:
        if role not in _ROLE_RANK:
            raise ValueError("role must be admin/editor/viewer")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO connector_group_roles (tenant_id, group_object_id, role, created_by)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (tenant_id, group_object_id) DO UPDATE
                    SET role = EXCLUDED.role
                RETURNING id, tenant_id, group_object_id, role, created_at, created_by
                """,
                tenant_id, group_object_id, role, created_by,
            )
        return {**dict(row), "id": str(row["id"]),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None}

    async def list_group_roles(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, tenant_id, group_object_id, role, created_at, created_by "
                "FROM connector_group_roles ORDER BY created_at DESC"
            )
        return [
            {**dict(r), "id": str(r["id"]),
             "created_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in rows
        ]

    async def delete_group_role(self, role_id: str) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute("DELETE FROM connector_group_roles WHERE id=$1", role_id)
        return res != "DELETE 0"

    # ── Local exceptions ───────────────────────────────────────────────────

    async def add_local_exception(
        self,
        *,
        identity_id: str,
        effect: str,
        scope: str,
        role: Optional[str] = None,
        connector_id: Optional[str] = None,
        reason: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        if effect not in ("allow", "deny"):
            raise ValueError("effect must be allow/deny")
        if scope not in ("role", "connector"):
            raise ValueError("scope must be role/connector")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO connector_local_exceptions
                    (identity_id, effect, scope, role, connector_id, reason, expires_at, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                RETURNING id
                """,
                identity_id, effect, scope, role, connector_id, reason, expires_at, created_by,
            )
        return {"id": str(row["id"])}

    async def _active_exceptions(self, conn, identity_id: str) -> List[asyncpg.Record]:
        return await conn.fetch(
            """
            SELECT effect, scope, role, connector_id
              FROM connector_local_exceptions
             WHERE identity_id = $1
               AND (expires_at IS NULL OR expires_at > NOW())
            """,
            identity_id,
        )

    # ── Entitlement resolution ─────────────────────────────────────────────

    async def effective_role(self, identity_id: str, *, base_role: str = "viewer") -> str:
        """Resolve the effective app role. deny > group role > local allow > base."""
        membership = await self.get_membership(identity_id)
        group_ids = membership["group_ids"]
        async with self.pool.acquire() as conn:
            exceptions = await self._active_exceptions(conn, identity_id)
            group_role = "viewer"
            if group_ids and membership["fresh"]:
                grow = await conn.fetch(
                    "SELECT role FROM connector_group_roles WHERE group_object_id = ANY($1::text[])",
                    group_ids,
                )
                for gr in grow:
                    group_role = _higher(group_role, gr["role"])

        # deny(role) caps to viewer.
        if any(e["effect"] == "deny" and e["scope"] == "role" for e in exceptions):
            return "viewer"
        local_allow = "viewer"
        for e in exceptions:
            if e["effect"] == "allow" and e["scope"] == "role" and e["role"]:
                local_allow = _higher(local_allow, e["role"])
        return _higher(_higher(base_role, group_role), local_allow)

    async def can_use_connector(
        self, identity_id: str, connector_id: str, *, group_grant_ids: List[str]
    ) -> Tuple[bool, str]:
        """Return (allowed, reason).

        ``group_grant_ids`` is the list of Entra group object ids that gate the
        connector (from the registry). Access requires membership in one of them
        with fresh/complete membership data, OR an explicit local allow exception.
        Any matching local deny wins.
        """
        membership = await self.get_membership(identity_id)
        async with self.pool.acquire() as conn:
            exceptions = await self._active_exceptions(conn, identity_id)

        for e in exceptions:
            if (
                e["effect"] == "deny"
                and e["scope"] == "connector"
                and str(e["connector_id"]) == str(connector_id)
            ):
                return False, "local_deny"

        for e in exceptions:
            if (
                e["effect"] == "allow"
                and e["scope"] == "connector"
                and str(e["connector_id"]) == str(connector_id)
            ):
                return True, "local_allow"

        if not group_grant_ids:
            return False, "no_group_grants"

        if not membership["fresh"]:
            # Fail closed on stale/incomplete membership for group-based allows.
            return False, "membership_stale"

        if set(group_grant_ids) & set(membership["group_ids"]):
            return True, "group_grant"
        return False, "not_in_granted_group"

    # ── row mappers ────────────────────────────────────────────────────────

    @staticmethod
    def _identity_row(row: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "tenant_id": row["tenant_id"],
            "object_id": row["object_id"],
            "upn": row["upn"],
            "display_name": row["display_name"],
            "auth_user_id": row["auth_user_id"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
