"""Durable, encrypted result snapshots used as the export authorization source.

A snapshot is created at query completion from the SERVER-produced result set
(never browser-submitted rows). It binds the result to the owner, source query,
connection, a policy version, an expiry, and a payload hash, and stores the rows
envelope-encrypted. Outbound actions render their payload from a snapshot, so the
model/browser can never substitute different data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from src.security import crypto

logger = logging.getLogger(__name__)

POLICY_VERSION = "snapshot-v1"


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SnapshotService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create_snapshot(
        self,
        *,
        owner_user_id: str,
        identity_id: Optional[str],
        connection: str,
        query_id: Optional[str],
        sql: Optional[str],
        results: Dict[str, Any],
        classification: str = "internal",
        ttl_seconds: int = 3600,
    ) -> Optional[Dict[str, Any]]:
        """Create a snapshot from a server-held result. Returns metadata or None."""
        if not crypto.crypto_available():
            logger.warning("snapshot: APP_ENCRYPTION_KEY not configured — skipping snapshot")
            return None
        columns = results.get("columns") or []
        rows = results.get("rows") or results.get("data") or []
        payload = {"columns": columns, "rows": rows}
        canonical = _canonical(payload)
        payload_hash = _hash(canonical)
        source_query_hash = _hash(f"{connection}\x00{sql or ''}")

        snapshot_id = uuid.uuid4()
        blob = crypto.encrypt(canonical, aad=f"snapshot:{snapshot_id}")
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_result_snapshots
                    (id, owner_user_id, identity_id, connection, query_id,
                     source_query_hash, policy_version, row_count, columns, classification,
                     payload_hash, algo, kek_id, ciphertext, nonce, wrapped_dek, dek_nonce, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                """,
                snapshot_id, owner_user_id, identity_id, connection, query_id,
                source_query_hash, POLICY_VERSION, len(rows), json.dumps(columns),
                classification, payload_hash, blob.algo, blob.kek_id, blob.ciphertext,
                blob.nonce, blob.wrapped_dek, blob.dek_nonce, expires,
            )
        return {
            "id": str(snapshot_id),
            "row_count": len(rows),
            "columns": columns,
            "payload_hash": payload_hash,
            "expires_at": expires.isoformat(),
        }

    async def get_meta(self, snapshot_id: str, *, owner_user_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, owner_user_id, connection, query_id, row_count, columns, "
                "classification, payload_hash, policy_version, created_at, expires_at "
                "FROM connector_result_snapshots WHERE id=$1",
                snapshot_id,
            )
        if not row or row["owner_user_id"] != owner_user_id:
            return None
        if row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc):
            return None
        columns = row["columns"]
        if isinstance(columns, str):
            columns = json.loads(columns)
        return {
            "id": str(row["id"]),
            "connection": row["connection"],
            "query_id": row["query_id"],
            "row_count": row["row_count"],
            "columns": columns,
            "classification": row["classification"],
            "payload_hash": row["payload_hash"],
            "policy_version": row["policy_version"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        }

    async def get_payload(self, snapshot_id: str, *, owner_user_id: str) -> Optional[Dict[str, Any]]:
        """Decrypt + integrity-check the snapshot rows (server-side render only)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM connector_result_snapshots WHERE id=$1", snapshot_id
            )
        if not row or row["owner_user_id"] != owner_user_id:
            return None
        if row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc):
            return None
        canonical = crypto.decrypt(
            crypto.EncryptedBlob.from_row(dict(row)), aad=f"snapshot:{row['id']}"
        )
        if _hash(canonical) != row["payload_hash"]:
            raise crypto.CryptoError("Snapshot payload hash mismatch (tampering).")
        payload = json.loads(canonical)
        return {"columns": payload.get("columns", []), "rows": payload.get("rows", [])}

    async def cleanup_expired(self) -> int:
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM connector_result_snapshots WHERE expires_at <= NOW()"
            )
        try:
            return int(res.split()[-1])
        except Exception:  # noqa: BLE001
            return 0
