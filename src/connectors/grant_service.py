"""Per-user connector grants, encrypted token material, and OAuth sessions.

All secret material (PKCE verifiers, refresh/access tokens) is envelope-encrypted
with AAD bound to the row's immutable id (see ``src/security/crypto.py``). Secret
values are never returned by any API; only server-side flows decrypt them.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from src.security import crypto

logger = logging.getLogger(__name__)


class GrantError(RuntimeError):
    pass


def _require_crypto() -> None:
    if not crypto.crypto_available():
        raise GrantError(
            "APP_ENCRYPTION_KEY is not configured — cannot store per-user connector tokens."
        )


class GrantService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── OAuth sessions (PKCE) ──────────────────────────────────────────────

    async def create_oauth_session(
        self,
        *,
        state: str,
        identity_id: str,
        connector_id: str,
        connector_version_id: str,
        redirect_uri: str,
        code_verifier: str,
        oidc_nonce: str,
        ttl_seconds: int = 600,
    ) -> str:
        _require_crypto()
        session_id = uuid.uuid4()
        blob = crypto.encrypt(code_verifier, aad=f"oauth_session:{session_id}")
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_oauth_sessions
                    (id, state, identity_id, connector_id, connector_version_id, redirect_uri,
                     algo, kek_id, ciphertext, nonce, wrapped_dek, dek_nonce, oidc_nonce, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                session_id, state, identity_id, connector_id, connector_version_id, redirect_uri,
                blob.algo, blob.kek_id, blob.ciphertext, blob.nonce, blob.wrapped_dek,
                blob.dek_nonce, oidc_nonce, expires,
            )
        return str(session_id)

    async def consume_oauth_session(self, state: str) -> Optional[Dict[str, Any]]:
        """Single-use: mark consumed and return the session with decrypted verifier."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT * FROM connector_oauth_sessions
                     WHERE state = $1 AND consumed_at IS NULL AND expires_at > NOW()
                     FOR UPDATE
                    """,
                    state,
                )
                if not row:
                    return None
                await conn.execute(
                    "UPDATE connector_oauth_sessions SET consumed_at = NOW() WHERE id = $1",
                    row["id"],
                )
        verifier = crypto.decrypt(
            crypto.EncryptedBlob.from_row(dict(row)), aad=f"oauth_session:{row['id']}"
        )
        return {
            "id": str(row["id"]),
            "identity_id": str(row["identity_id"]),
            "connector_id": str(row["connector_id"]),
            "connector_version_id": str(row["connector_version_id"]),
            "redirect_uri": row["redirect_uri"],
            "code_verifier": verifier,
            "oidc_nonce": row["oidc_nonce"],
        }

    # ── Grants ─────────────────────────────────────────────────────────────

    async def upsert_grant(
        self,
        *,
        identity_id: str,
        connector_id: str,
        connector_version_id: str,
        external_account: Optional[str],
        scopes: Optional[str],
        refresh_token: Optional[str],
        access_token: Optional[str],
        access_expires_at: Optional[datetime],
    ) -> str:
        _require_crypto()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                grow = await conn.fetchrow(
                    """
                    INSERT INTO connector_user_grants
                        (id, identity_id, connector_id, connector_version_id,
                         status, external_account, scopes)
                    VALUES ($1,$2,$3,$4,'active',$5,$6)
                    ON CONFLICT (identity_id, connector_id) DO UPDATE
                        SET status='active',
                            connector_version_id = EXCLUDED.connector_version_id,
                            external_account = EXCLUDED.external_account,
                            scopes = EXCLUDED.scopes,
                            updated_at = NOW()
                    RETURNING id
                    """,
                    uuid.uuid4(), identity_id, connector_id, connector_version_id,
                    external_account, scopes,
                )
                grant_id = grow["id"]
                if refresh_token is not None:
                    await self._store_secret(conn, grant_id, "refresh_token", refresh_token, None)
                if access_token is not None:
                    await self._store_secret(
                        conn, grant_id, "access_token", access_token, access_expires_at
                    )
        return str(grant_id)

    async def _store_secret(
        self, conn, grant_id, kind: str, plaintext: str, expires_at: Optional[datetime]
    ) -> None:
        secret_id = uuid.uuid4()
        blob = crypto.encrypt(plaintext, aad=f"grant_secret:{secret_id}")
        await conn.execute(
            """
            INSERT INTO connector_grant_secrets
                (id, grant_id, kind, algo, kek_id, ciphertext, nonce, wrapped_dek, dek_nonce, expires_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (grant_id, kind) DO UPDATE
                SET id=EXCLUDED.id, algo=EXCLUDED.algo, kek_id=EXCLUDED.kek_id,
                    ciphertext=EXCLUDED.ciphertext, nonce=EXCLUDED.nonce,
                    wrapped_dek=EXCLUDED.wrapped_dek, dek_nonce=EXCLUDED.dek_nonce,
                    expires_at=EXCLUDED.expires_at, updated_at=NOW()
            """,
            secret_id, grant_id, kind, blob.algo, blob.kek_id, blob.ciphertext,
            blob.nonce, blob.wrapped_dek, blob.dek_nonce, expires_at,
        )

    async def store_access_token(
        self, grant_id: str, token: str, expires_at: Optional[datetime]
    ) -> None:
        _require_crypto()
        async with self.pool.acquire() as conn:
            await self._store_secret(conn, uuid.UUID(grant_id), "access_token", token, expires_at)

    async def _read_secret(self, grant_id: str, kind: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, algo, kek_id, ciphertext, nonce, wrapped_dek, dek_nonce, expires_at "
                "FROM connector_grant_secrets WHERE grant_id=$1 AND kind=$2",
                grant_id, kind,
            )
        if not row:
            return None
        value = crypto.decrypt(
            crypto.EncryptedBlob.from_row(dict(row)), aad=f"grant_secret:{row['id']}"
        )
        return {"value": value, "expires_at": row["expires_at"]}

    async def get_refresh_token(self, grant_id: str) -> Optional[str]:
        s = await self._read_secret(grant_id, "refresh_token")
        return s["value"] if s else None

    async def get_access_token(self, grant_id: str) -> Optional[Dict[str, Any]]:
        return await self._read_secret(grant_id, "access_token")

    async def get_grant(self, identity_id: str, connector_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM connector_user_grants WHERE identity_id=$1 AND connector_id=$2",
                identity_id, connector_id,
            )
        return self._grant_dict(row) if row else None

    async def get_grant_by_id(self, grant_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM connector_user_grants WHERE id=$1", grant_id)
        return self._grant_dict(row) if row else None

    async def list_grants(self, identity_id: str) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM connector_user_grants WHERE identity_id=$1 ORDER BY created_at DESC",
                identity_id,
            )
        return [self._grant_dict(r) for r in rows]

    async def revoke_grant(self, grant_id: str) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                res = await conn.execute(
                    "UPDATE connector_user_grants SET status='revoked', updated_at=NOW() WHERE id=$1",
                    grant_id,
                )
                # Destroy token material on revoke.
                await conn.execute(
                    "DELETE FROM connector_grant_secrets WHERE grant_id=$1", grant_id
                )
        return res != "UPDATE 0"

    async def touch_used(self, grant_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE connector_user_grants SET last_used_at=NOW() WHERE id=$1", grant_id
            )

    @staticmethod
    def _grant_dict(row: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "identity_id": str(row["identity_id"]),
            "connector_id": str(row["connector_id"]),
            "connector_version_id": str(row["connector_version_id"]),
            "status": row["status"],
            "external_account": row["external_account"],
            "scopes": row["scopes"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
        }
