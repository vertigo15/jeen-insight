"""Admin connector registry: connectors, immutable versions, client secrets, gating.

v1 stores only fixed native-provider connectors derived from the static catalog
(``src/connectors/catalog.py``). There is no support for arbitrary remote URLs or
admin-supplied manifests. Each config change creates a NEW immutable
``connector_versions`` row so grants/proposals always reference the exact
manifest+config they were created against.

Connector-level client secrets (the OAuth client secret) are envelope-encrypted
and NEVER returned by any API. The plaintext is only decrypted server-side for
the OAuth token exchange.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import asyncpg

from src.connectors.catalog import get_catalog_entry
from src.security import crypto

logger = logging.getLogger(__name__)


class RegistryError(RuntimeError):
    pass


class ConnectorRegistryService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Connectors ─────────────────────────────────────────────────────────

    async def create_connector(
        self, *, catalog_key: str, display_name: Optional[str], created_by: Optional[str]
    ) -> Dict[str, Any]:
        entry = get_catalog_entry(catalog_key)
        if entry is None or entry.coming_soon:
            raise RegistryError(f"Unknown or unavailable catalog connector: {catalog_key!r}")

        manifest = entry.build_manifest()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT id FROM connectors WHERE key = $1", entry.key
                )
                if existing:
                    raise RegistryError(f"Connector {entry.key!r} already exists")
                connector_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO connectors (id, key, provider, display_name, is_enabled, created_by)
                    VALUES ($1,$2,$3,$4,FALSE,$5)
                    """,
                    connector_id, entry.key, entry.provider,
                    display_name or entry.display_name, created_by,
                )
                version_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO connector_versions (id, connector_id, version, manifest, config, created_by)
                    VALUES ($1,$2,1,$3::jsonb,$4::jsonb,$5)
                    """,
                    version_id, connector_id, json.dumps(manifest),
                    json.dumps(manifest.get("config", {})), created_by,
                )
                await conn.execute(
                    "UPDATE connectors SET current_version_id = $2 WHERE id = $1",
                    connector_id, version_id,
                )
        logger.info("registry: created connector %s (%s)", entry.key, connector_id)
        return await self.get_connector(str(connector_id))  # type: ignore[return-value]

    async def get_connector(self, connector_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM connectors WHERE id = $1", connector_id)
            if not row:
                return None
            return await self._connector_dict(conn, row)

    async def get_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM connectors WHERE key = $1", key)
            if not row:
                return None
            return await self._connector_dict(conn, row)

    async def list_connectors(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM connectors ORDER BY created_at")
            return [await self._connector_dict(conn, r) for r in rows]

    async def set_enabled(self, connector_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE connectors SET is_enabled=$2, updated_at=NOW() WHERE id=$1",
                connector_id, enabled,
            )
        return await self.get_connector(connector_id)

    async def set_config(
        self, connector_id: str, config: Dict[str, Any], *, created_by: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Create a new immutable version with updated config; point current at it."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, key FROM connectors WHERE id=$1", connector_id)
            if not row:
                return None
            entry = get_catalog_entry(row["key"])
            if entry is None:
                raise RegistryError(f"Connector {row['key']!r} has no catalog definition")
            manifest = entry.build_manifest(config)
            async with conn.transaction():
                nextver = await conn.fetchval(
                    "SELECT COALESCE(MAX(version),0)+1 FROM connector_versions WHERE connector_id=$1",
                    connector_id,
                )
                version_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO connector_versions (id, connector_id, version, manifest, config, created_by)
                    VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6)
                    """,
                    version_id, connector_id, nextver, json.dumps(manifest),
                    json.dumps(manifest.get("config", {})), created_by,
                )
                await conn.execute(
                    "UPDATE connectors SET current_version_id=$2, updated_at=NOW() WHERE id=$1",
                    connector_id, version_id,
                )
        return await self.get_connector(connector_id)

    async def get_current_version(self, connector_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT v.* FROM connector_versions v
                  JOIN connectors c ON c.current_version_id = v.id
                 WHERE c.id = $1
                """,
                connector_id,
            )
        return self._version_dict(row) if row else None

    async def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM connector_versions WHERE id=$1", version_id)
        return self._version_dict(row) if row else None

    async def delete_connector(self, connector_id: str) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute("DELETE FROM connectors WHERE id=$1", connector_id)
        return res != "DELETE 0"

    # ── Client secret (encrypted; never returned) ──────────────────────────

    async def set_client_secret(
        self, connector_id: str, secret_plaintext: str, *, created_by: Optional[str]
    ) -> None:
        if not crypto.crypto_available():
            raise RegistryError(
                "APP_ENCRYPTION_KEY is not configured — cannot store a connector client secret."
            )
        secret_id = uuid.uuid4()
        blob = crypto.encrypt(secret_plaintext, aad=f"connector_client_secret:{secret_id}")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                nextver = await conn.fetchval(
                    "SELECT COALESCE(MAX(version),0)+1 FROM connector_client_secrets "
                    "WHERE connector_id=$1 AND purpose='oauth_client_secret'",
                    connector_id,
                )
                await conn.execute(
                    "UPDATE connector_client_secrets SET is_active=FALSE "
                    "WHERE connector_id=$1 AND purpose='oauth_client_secret'",
                    connector_id,
                )
                await conn.execute(
                    """
                    INSERT INTO connector_client_secrets
                        (id, connector_id, purpose, version, is_active,
                         algo, kek_id, ciphertext, nonce, wrapped_dek, dek_nonce, created_by)
                    VALUES ($1,$2,'oauth_client_secret',$3,TRUE,$4,$5,$6,$7,$8,$9,$10)
                    """,
                    secret_id, connector_id, nextver,
                    blob.algo, blob.kek_id, blob.ciphertext, blob.nonce,
                    blob.wrapped_dek, blob.dek_nonce, created_by,
                )
        logger.info("registry: stored client secret v%s for connector %s", nextver, connector_id)

    async def get_client_secret(self, connector_id: str) -> Optional[str]:
        """Decrypt the active client secret (server-side only)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, algo, kek_id, ciphertext, nonce, wrapped_dek, dek_nonce "
                "FROM connector_client_secrets "
                "WHERE connector_id=$1 AND purpose='oauth_client_secret' AND is_active=TRUE "
                "ORDER BY version DESC LIMIT 1",
                connector_id,
            )
        if not row:
            return None
        blob = crypto.EncryptedBlob.from_row(dict(row))
        return crypto.decrypt(blob, aad=f"connector_client_secret:{row['id']}")

    async def has_client_secret(self, connector_id: str) -> bool:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT 1 FROM connector_client_secrets "
                "WHERE connector_id=$1 AND purpose='oauth_client_secret' AND is_active=TRUE LIMIT 1",
                connector_id,
            )
        return bool(val)

    # ── group -> connector gating ──────────────────────────────────────────

    async def add_group_grant(
        self, *, connector_id: str, tenant_id: str, group_object_id: str, created_by: Optional[str]
    ) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO connector_group_grants (connector_id, tenant_id, group_object_id, created_by)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (connector_id, group_object_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id
                RETURNING id, connector_id, tenant_id, group_object_id, created_at, created_by
                """,
                connector_id, tenant_id, group_object_id, created_by,
            )
        return {**dict(row), "id": str(row["id"]), "connector_id": str(row["connector_id"]),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None}

    async def remove_group_grant(self, connector_id: str, group_object_id: str) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM connector_group_grants WHERE connector_id=$1 AND group_object_id=$2",
                connector_id, group_object_id,
            )
        return res != "DELETE 0"

    async def list_group_grant_ids(self, connector_id: str) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_object_id FROM connector_group_grants WHERE connector_id=$1",
                connector_id,
            )
        return [r["group_object_id"] for r in rows]

    # ── mappers ────────────────────────────────────────────────────────────

    async def _connector_dict(self, conn, row: asyncpg.Record) -> Dict[str, Any]:
        entry = get_catalog_entry(row["key"])
        grants = await conn.fetch(
            "SELECT group_object_id FROM connector_group_grants WHERE connector_id=$1",
            row["id"],
        )
        secret = await conn.fetchval(
            "SELECT 1 FROM connector_client_secrets "
            "WHERE connector_id=$1 AND purpose='oauth_client_secret' AND is_active=TRUE LIMIT 1",
            row["id"],
        )
        version = None
        if row["current_version_id"]:
            vrow = await conn.fetchrow(
                "SELECT * FROM connector_versions WHERE id=$1", row["current_version_id"]
            )
            version = self._version_dict(vrow)
        return {
            "id": str(row["id"]),
            "key": row["key"],
            "provider": row["provider"],
            "display_name": row["display_name"],
            "is_enabled": row["is_enabled"],
            "category": entry.category if entry else None,
            "has_client_secret": bool(secret),
            "group_grants": [g["group_object_id"] for g in grants],
            "current_version": version,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    @staticmethod
    def _version_dict(row: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        manifest = row["manifest"]
        config = row["config"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        if isinstance(config, str):
            config = json.loads(config)
        return {
            "id": str(row["id"]),
            "connector_id": str(row["connector_id"]),
            "version": row["version"],
            "manifest": manifest,
            "config": config,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
