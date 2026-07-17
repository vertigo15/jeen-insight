"""Append-only audit log for the connector platform.

The DB enforces immutability (a trigger blocks UPDATE/DELETE — see migration
012). This service only ever INSERTs event-type rows. It NEVER records tokens,
result rows, message bodies, or full recipient addresses — recipient PII is
redacted to domain + a KEYED (HMAC) hash before it reaches the log.

Operational hardening (recommended): run the application with a DB role that has
INSERT (and SELECT) on ``connector_audit`` but NOT UPDATE/DELETE nor table
ownership/DDL, so the append-only trigger cannot be dropped by the app's own
credentials. The trigger is a backstop; role separation is the real control.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)


def _recipient_hmac_key() -> bytes:
    """Server-side key for recipient hashing (never leaves the server).

    Prefers APP_ENCRYPTION_KEY, then INTERNAL_API_SECRET / FLASK_SECRET_KEY. The
    first entry is used when a rotation list ("<kid>:<secret>,…") is configured.
    """
    raw = (
        os.getenv("APP_ENCRYPTION_KEY")
        or os.getenv("INTERNAL_API_SECRET")
        or os.getenv("FLASK_SECRET_KEY")
        or os.getenv("AUTH_SECRET")
        or ""
    ).strip()
    if not raw:
        return b""
    first = raw.split(",")[0].strip()
    if ":" in first:
        first = first.split(":", 1)[1]
    return first.encode("utf-8")


def redact_recipient(addr: str) -> Dict[str, str]:
    """Redact a recipient to a domain + KEYED hash (no local part).

    Uses HMAC-SHA256 with a server-side key so the correlation hash cannot be
    brute-forced back to the address by enumerating common emails. When no key is
    configured we fail safe and drop the hash entirely (domain only).
    """
    addr = (addr or "").strip().lower()
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    key = _recipient_hmac_key()
    if not addr or not key:
        return {"domain": domain}
    digest = hmac.new(key, addr.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return {"domain": domain, "hash": digest}


def redact_recipients(addrs: List[str]) -> List[Dict[str, str]]:
    return [redact_recipient(a) for a in (addrs or [])]


class AuditService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def log(
        self,
        *,
        event_type: str,
        actor_user_id: Optional[str] = None,
        actor_email: Optional[str] = None,
        identity_id: Optional[str] = None,
        connector_id: Optional[str] = None,
        grant_id: Optional[str] = None,
        proposal_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        outcome: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one audit event. Best-effort: never raises into the caller."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO connector_audit
                        (event_type, actor_user_id, actor_email, identity_id,
                         connector_id, grant_id, proposal_id, snapshot_id,
                         outcome, detail)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                    """,
                    event_type, actor_user_id, actor_email, identity_id,
                    connector_id, grant_id, proposal_id, snapshot_id,
                    outcome, json.dumps(detail or {}),
                )
        except Exception:  # noqa: BLE001
            logger.exception("audit: failed to write event %s", event_type)

    async def list_recent(
        self, *, limit: int = 200, actor_user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        clause = ""
        args: List[Any] = [limit]
        if actor_user_id:
            clause = "WHERE actor_user_id = $2"
            args.append(actor_user_id)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, event_time, event_type, actor_user_id, actor_email,
                       identity_id, connector_id, grant_id, proposal_id,
                       snapshot_id, outcome, detail
                  FROM connector_audit
                  {clause}
                 ORDER BY event_time DESC
                 LIMIT $1
                """,
                *args,
            )
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["event_time"] = r["event_time"].isoformat() if r["event_time"] else None
            for k in ("id", "identity_id", "connector_id", "grant_id", "proposal_id", "snapshot_id"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            detail = r["detail"]
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except Exception:  # noqa: BLE001
                    detail = {}
            d["detail"] = detail or {}
            out.append(d)
        return out
