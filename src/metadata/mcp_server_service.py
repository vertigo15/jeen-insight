"""MCP server service — CRUD for insights_mcp_servers.

One active row in insights_mcp_servers + app_settings.catalog_source = 'mcp'
switches the entire app to MCP catalog mode. All connections (source_keys)
are served by the single active MCP server.

Key differences from the previous mcp_config_service:
  - health is a single JSONB blob (not 6 individual tool_* columns)
  - transport includes 'stdio'
  - auth_type includes 'oauth'
  - catalog_source ('db' | 'mcp') is stored in app_settings, not here
  - McpServer.get_tool_for_need() resolves tool names from the health blob
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)

# ── Column list ───────────────────────────────────────────────────────────────

_COLS = """
    id, is_active,
    server_name, endpoint, transport, auth_type, bearer_token,
    token_algo, token_kek_id, token_ciphertext, token_nonce,
    token_wrapped_dek, token_dek_nonce,
    cache_ttl_seconds, health, last_checked_at,
    created_at, updated_at
"""

# ── Need keys (used in health.tools[*].need) ─────────────────────────────────
# These are the canonical need identifiers shared between the service layer
# and the UI.  Each MCP tool's 'need' field maps to one of these keys.

NEED_LIST_SOURCES      = "list_sources"
NEED_LIST_TABLES       = "list_tables"
NEED_DESCRIBE_TABLE    = "describe_table"
NEED_LIST_RELATIONSHIPS = "list_relationships"
NEED_BUSINESS_GLOSSARY = "business_glossary"
NEED_KNOWLEDGE_PAIRS   = "knowledge_pairs"
# Structured autocomplete datasets (optional). Each maps to a dedicated tool
# returning JSON rows, used by the `/`, `#`, and `@` UI features under MCP.
NEED_TABLES_RICH        = "tables_rich"
NEED_LIST_COLUMNS       = "list_columns"
NEED_KNOWLEDGE_QUESTIONS = "knowledge_questions"

# Required needs — server cannot be activated until these are mapped.
# NEED_LIST_SOURCES → list_connections  (connection list)
# NEED_LIST_TABLES  → get_catalog_prompt (all catalog data in one call)
REQUIRED_NEEDS = {NEED_LIST_SOURCES, NEED_LIST_TABLES}

# Canonical catalog-need definitions surfaced to the API/UI. Keeping the
# labels + required flags here gives the activation gate (REQUIRED_NEEDS),
# the API and the settings UI a single source of truth instead of three
# independent copies.
CATALOG_NEEDS = [
    {"key": NEED_LIST_SOURCES,       "label": "List connections",                 "required": True},
    {"key": NEED_LIST_TABLES,        "label": "Catalog prompt (tables, columns)", "required": True},
    {"key": NEED_LIST_RELATIONSHIPS, "label": "Relationships",                    "required": False},
    {"key": NEED_BUSINESS_GLOSSARY,  "label": "Business terms & glossary",        "required": False},
    {"key": NEED_TABLES_RICH,        "label": "Tables (rich: @ picker)",          "required": False},
    {"key": NEED_LIST_COLUMNS,       "label": "Columns (# autocomplete)",         "required": False},
    {"key": NEED_KNOWLEDGE_QUESTIONS,"label": "Knowledge questions (/ templates)","required": False},
]

# Human-readable labels for required needs, used in error messages.
REQUIRED_NEED_LABELS = {
    n["key"]: n["label"] for n in CATALOG_NEEDS if n["required"]
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class McpServer:
    """Mirrors a row in insights_mcp_servers."""

    id: int
    is_active: bool

    server_name: str
    endpoint: str
    transport: str      # 'stdio' | 'sse' | 'http'
    auth_type: str      # 'none'  | 'bearer' | 'oauth'
    bearer_token: Optional[str]
    cache_ttl_seconds: int

    # Full health-check result. None = never tested.
    health: Optional[Dict[str, Any]]
    last_checked_at: Optional[datetime]

    created_at: datetime
    updated_at: datetime

    # ── Derived helpers ───────────────────────────────────────────────────────

    def get_tool_for_need(self, need: str) -> Optional[str]:
        """Return the MCP tool name mapped to *need*, or None."""
        if not self.health:
            return None
        for t in self.health.get("tools", []):
            if isinstance(t, dict) and t.get("need") == need:
                return t.get("name")
        return None

    @property
    def is_ready(self) -> bool:
        """True if all required needs are mapped (server can be activated)."""
        return all(self.get_tool_for_need(n) for n in REQUIRED_NEEDS)

    @property
    def health_status(self) -> Optional[str]:
        """'healthy' | 'degraded' | 'down' | None (not checked)."""
        return (self.health or {}).get("status")

    def to_dict(self, *, include_token: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id":               self.id,
            "is_active":        self.is_active,
            "server_name":      self.server_name,
            "endpoint":         self.endpoint,
            "transport":        self.transport,
            "auth_type":        self.auth_type,
            "has_token":        bool(self.bearer_token),
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "health":           self.health,
            "last_checked_at":  self.last_checked_at.isoformat() if self.last_checked_at else None,
            "is_ready":         self.is_ready,
            "created_at":       self.created_at.isoformat() if self.created_at else None,
            "updated_at":       self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_token:
            d["bearer_token"] = self.bearer_token
        return d


# ── Row → dataclass ───────────────────────────────────────────────────────────

class McpTokenError(RuntimeError):
    """Raised when an MCP bearer token cannot be stored securely."""


def _dev_mode() -> bool:
    # Default TRUE (POC/portable). Set JEEN_DEV_MODE=false to harden.
    raw = os.getenv("JEEN_DEV_MODE")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on", "t")


def _require_kek_for_token(token: Optional[str]) -> None:
    """Gate MCP bearer-token persistence.

    Hardened mode (JEEN_DEV_MODE=false): refuse a token unless a KEK is
    configured, so it is always encrypted at rest.

    Dev/POC mode (default): allow it and store portable plaintext when no KEK is
    set — this is what lets the same shared DB be read by every copy of the app
    (local / regular Azure / defence stack) without provisioning a shared key.
    """
    if not token:
        return
    from src.security import crypto

    if crypto.crypto_available():
        return
    if _dev_mode():
        logger.warning(
            "mcp_server: storing bearer token as PLAINTEXT (no APP_ENCRYPTION_KEY). "
            "Portable across stacks and fine for a POC; set JEEN_DEV_MODE=false and "
            "APP_ENCRYPTION_KEY to encrypt it at rest."
        )
        return
    raise McpTokenError(
        "APP_ENCRYPTION_KEY must be configured before storing an MCP bearer token "
        "— refusing to persist it in plaintext (JEEN_DEV_MODE=false)."
    )


def _decrypt_token(row: Any) -> Optional[str]:
    """Return the effective (decrypted) bearer token for internal use.

    New writes are always envelope-encrypted (never plaintext). The legacy
    plaintext column is still *read* so pre-encryption rows keep working until the
    one-time backfill migrates them, but it is never written again.
    """
    if row["token_ciphertext"]:
        try:
            from src.security import crypto

            blob = crypto.EncryptedBlob(
                algo=row["token_algo"],
                kek_id=row["token_kek_id"],
                ciphertext=row["token_ciphertext"],
                nonce=row["token_nonce"],
                wrapped_dek=row["token_wrapped_dek"],
                dek_nonce=row["token_dek_nonce"],
            )
            return crypto.decrypt(blob, aad=f"mcp_server:{row['id']}:bearer")
        except Exception as exc:  # noqa: BLE001
            # KEK missing/mismatched (e.g. this deployment does not hold the key
            # that encrypted the token, or the shared DB was encrypted by another
            # env). Rather than fail the entire catalog closed — which shows up as
            # a silent, hard-to-diagnose empty sidebar — degrade to the plaintext
            # column when one is still present. This is exactly the failure mode
            # that took the catalog down when the shared token was re-encrypted
            # with a key the deployed app didn't have.
            # Log the failure class and the (non-secret) key id, never the raw
            # exception text or any token material.
            err = type(exc).__name__
            kek_id = row["token_kek_id"]
            if row["bearer_token"]:
                logger.error(
                    "mcp_server id=%s: cannot decrypt bearer token (err=%s, kek_id=%s) — "
                    "falling back to the plaintext column. Set this deployment's "
                    "APP_ENCRYPTION_KEY to the key that encrypted it to restore "
                    "encrypted-at-rest.",
                    row["id"], err, kek_id,
                )
                return row["bearer_token"]
            logger.error(
                "mcp_server id=%s: cannot decrypt bearer token (err=%s, kek_id=%s) and no "
                "plaintext fallback exists — catalog auth will fail. Fix APP_ENCRYPTION_KEY.",
                row["id"], err, kek_id,
            )
            return None
    if row["bearer_token"]:
        logger.warning(
            "mcp_server id=%s still has a legacy plaintext bearer token; run the "
            "encryption backfill to migrate it.", row["id"],
        )
    return row["bearer_token"]


def _row_to_server(row: Any) -> McpServer:
    return McpServer(
        id=row["id"],
        is_active=row["is_active"],
        server_name=row["server_name"],
        endpoint=row["endpoint"],
        transport=row["transport"],
        auth_type=row["auth_type"],
        bearer_token=_decrypt_token(row),
        cache_ttl_seconds=row["cache_ttl_seconds"],
        health=_decode_health(row["health"]),
        last_checked_at=row["last_checked_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _decode_health(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode the JSONB health column regardless of whether asyncpg returns
    it as a Python dict/list (native codec) or as a JSON string."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json as _json
        try:
            decoded = _json.loads(raw)
            return decoded if isinstance(decoded, dict) else None
        except Exception:
            return None
    # asyncpg Record or other mapping type
    try:
        return dict(raw)
    except Exception:
        return None


# ── Service ───────────────────────────────────────────────────────────────────

class McpServerService:
    """CRUD for insights_mcp_servers + catalog_source toggle in app_settings."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Catalog source (per-connection) ───────────────────────────────────────────

    async def get_catalog_source(self, source_key: Optional[str] = None) -> str:
        """
        Return the **global** catalog source, ``'db'`` or ``'mcp'``.

        The application uses a single source at a time for every connection.
        ``source_key`` is accepted for backward compatibility but ignored.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM app_settings WHERE key = 'catalog_source'"
            )
        return (row["value"] if row else None) or "db"

    async def set_catalog_source(
        self, source: str, source_key: Optional[str] = None
    ) -> None:
        """
        Set the **global** catalog source (``'db'`` | ``'mcp'``).

        ``source_key`` is accepted for backward compatibility but ignored —
        the source is one global setting for the whole application.
        """
        if source not in ("db", "mcp"):
            raise ValueError(f"catalog_source must be 'db' or 'mcp', got {source!r}")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                    VALUES ('catalog_source', $1, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
                source,
            )
        logger.info("catalog_source → %s (global)", source)

    # ── Server CRUD ───────────────────────────────────────────────────────────

    async def get_active(self) -> Optional[McpServer]:
        """Return the active server, or None (app is in DB mode)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM insights_mcp_servers "
                "WHERE is_active = true LIMIT 1"
            )
        return _row_to_server(row) if row else None

    async def get_by_id(self, server_id: int) -> Optional[McpServer]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLS} FROM insights_mcp_servers WHERE id = $1",
                server_id,
            )
        return _row_to_server(row) if row else None

    async def list_all(self) -> List[McpServer]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLS} FROM insights_mcp_servers ORDER BY id DESC"
            )
        return [_row_to_server(r) for r in rows]

    async def create(
        self,
        *,
        server_name: str,
        endpoint: str,
        transport: str = "http",
        auth_type: str = "none",
        bearer_token: Optional[str] = None,
        cache_ttl_seconds: int = 900,
    ) -> McpServer:
        from src.security import crypto

        # Fail closed (hardened mode) before writing anything if we can't encrypt a
        # supplied token; in dev/POC mode this just warns and we store plaintext.
        _require_kek_for_token(bearer_token)
        will_encrypt = bool(bearer_token) and crypto.crypto_available()
        # When encrypting, insert NULL then write the ciphertext columns. Otherwise
        # store the (portable) plaintext token directly.
        plaintext_to_store = None if will_encrypt else bearer_token
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO insights_mcp_servers
                        (server_name, endpoint, transport, auth_type,
                         bearer_token, cache_ttl_seconds)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING {_COLS}
                    """,
                    server_name, endpoint, transport, auth_type,
                    plaintext_to_store, cache_ttl_seconds,
                )
                if will_encrypt:
                    await self._store_encrypted_token(conn, row["id"], bearer_token)
                    row = await conn.fetchrow(
                        f"SELECT {_COLS} FROM insights_mcp_servers WHERE id = $1", row["id"]
                    )
        logger.info("mcp_server: created id=%d (%s)", row["id"], server_name)
        return _row_to_server(row)

    async def _fetch_row(self, server_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"SELECT {_COLS} FROM insights_mcp_servers WHERE id = $1", server_id
            )

    async def _store_encrypted_token(self, conn, server_id: int, plaintext: str) -> None:
        """Write a bearer token to the encrypted columns (id-bound AAD), never plaintext.

        Requires a configured KEK; callers must gate with ``_require_kek_for_token``.
        """
        from src.security import crypto

        blob = crypto.encrypt(plaintext, aad=f"mcp_server:{server_id}:bearer")
        await conn.execute(
            """
            UPDATE insights_mcp_servers
               SET token_algo=$2, token_kek_id=$3, token_ciphertext=$4,
                   token_nonce=$5, token_wrapped_dek=$6, token_dek_nonce=$7,
                   bearer_token=NULL
             WHERE id=$1
            """,
            server_id, blob.algo, blob.kek_id, blob.ciphertext,
            blob.nonce, blob.wrapped_dek, blob.dek_nonce,
        )

    async def update(self, server_id: int, **fields: Any) -> Optional[McpServer]:
        """Update connection/auth/cache fields. Saving resets health to NULL."""
        _allowed = {
            "server_name", "endpoint", "transport",
            "auth_type", "bearer_token", "cache_ttl_seconds",
        }
        clean = {k: v for k, v in fields.items() if k in _allowed}
        if not clean:
            return await self.get_by_id(server_id)

        from src.security import crypto

        # Handle the bearer token out-of-band. Fail closed (hardened mode) if a
        # token is supplied without a configured KEK; in dev/POC mode store it as
        # portable plaintext instead.
        token_changed = "bearer_token" in clean
        new_token = clean.pop("bearer_token", None) if token_changed else None
        _require_kek_for_token(new_token)
        will_encrypt = token_changed and bool(new_token) and crypto.crypto_available()

        set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(clean)]
        set_parts += ["health = NULL", "last_checked_at = NULL", "updated_at = NOW()"]
        extra_params: List[Any] = []
        # A token change always clears any prior ciphertext columns.
        if token_changed:
            set_parts += [
                "token_algo = NULL", "token_kek_id = NULL", "token_ciphertext = NULL",
                "token_nonce = NULL", "token_wrapped_dek = NULL", "token_dek_nonce = NULL",
            ]
            if will_encrypt:
                # Plaintext cleared here; ciphertext written after the row exists.
                set_parts.append("bearer_token = NULL")
            else:
                # Store portable plaintext directly (or NULL to clear the token).
                idx = 2 + len(clean) + len(extra_params)
                set_parts.append(f"bearer_token = ${idx}")
                extra_params.append(new_token)
        sql = (
            f"UPDATE insights_mcp_servers "
            f"SET {', '.join(set_parts)} "
            f"WHERE id = $1 RETURNING {_COLS}"
        )
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(sql, server_id, *clean.values(), *extra_params)
                if row and will_encrypt:
                    await self._store_encrypted_token(conn, server_id, new_token)
                    row = await conn.fetchrow(
                        f"SELECT {_COLS} FROM insights_mcp_servers WHERE id = $1", server_id
                    )
        return _row_to_server(row) if row else None

    async def set_server_ttl(
        self, server_id: int, cache_ttl_seconds: int
    ) -> Optional[McpServer]:
        """Update only a server's cache TTL.

        Unlike :meth:`update`, this deliberately does **not** clear the stored
        health blob — changing the cache window is not a connection/auth change
        and must not force a re-test.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE insights_mcp_servers
                   SET cache_ttl_seconds = $2, updated_at = NOW()
                 WHERE id = $1
                RETURNING {_COLS}
                """,
                server_id, cache_ttl_seconds,
            )
        return _row_to_server(row) if row else None

    async def activate(self, server_id: int) -> Optional[McpServer]:
        """Set is_active = true and switch catalog_source to 'mcp'."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE insights_mcp_servers "
                    "SET is_active = false, updated_at = NOW() "
                    "WHERE is_active = true AND id <> $1",
                    server_id,
                )
                row = await conn.fetchrow(
                    f"""
                    UPDATE insights_mcp_servers
                       SET is_active = true, updated_at = NOW()
                     WHERE id = $1
                    RETURNING {_COLS}
                    """,
                    server_id,
                )
                await conn.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                        VALUES ('catalog_source', 'mcp', NOW())
                    ON CONFLICT (key) DO UPDATE
                        SET value = 'mcp', updated_at = NOW()
                    """,
                )
        if row:
            logger.info("mcp_server: activated id=%d — catalog_source=mcp", server_id)
        return _row_to_server(row) if row else None

    async def deactivate(self, server_id: int) -> Optional[McpServer]:
        """Set is_active = false and revert catalog_source to 'db'."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE insights_mcp_servers
                       SET is_active = false, updated_at = NOW()
                     WHERE id = $1
                    RETURNING {_COLS}
                    """,
                    server_id,
                )
                await conn.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                        VALUES ('catalog_source', 'db', NOW())
                    ON CONFLICT (key) DO UPDATE
                        SET value = 'db', updated_at = NOW()
                    """,
                )
        if row:
            logger.info("mcp_server: deactivated id=%d — catalog_source=db", server_id)
        return _row_to_server(row) if row else None

    async def delete(self, server_id: int) -> bool:
        """Delete server (cascades to insights_mcp_cache)."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM insights_mcp_servers WHERE id = $1", server_id
            )
        deleted = result != "DELETE 0"
        if deleted:
            logger.info("mcp_server: deleted id=%d", server_id)
        return deleted

    # ── Health ────────────────────────────────────────────────────────────────

    async def save_health(
        self,
        server_id: int,
        health: Dict[str, Any],
    ) -> Optional[McpServer]:
        """Persist the full health-check result blob."""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE insights_mcp_servers
                   SET health          = $2::jsonb,
                       last_checked_at = NOW(),
                       updated_at      = NOW()
                 WHERE id = $1
                RETURNING {_COLS}
                """,
                server_id,
                _json.dumps(health),
            )
        if row:
            logger.info(
                "mcp_server: health saved id=%d status=%s",
                server_id, health.get("status"),
            )
        return _row_to_server(row) if row else None

    async def clear_health(self, server_id: int) -> None:
        """Clear stored health (called when server config changes)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE insights_mcp_servers "
                "SET health = NULL, last_checked_at = NULL, updated_at = NOW() "
                "WHERE id = $1",
                server_id,
            )

    async def save_test_result(
        self, server_id: int, *, ok: bool, message: str
    ) -> None:
        """Persist a failed health-check result (status=down + error message)."""
        import json as _json
        from datetime import datetime, timezone

        fail_health = {
            "status":     "down",
            "error":      message,
            "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE insights_mcp_servers
                   SET health          = $2::jsonb,
                       last_checked_at = NOW(),
                       updated_at      = NOW()
                 WHERE id = $1
                """,
                server_id,
                _json.dumps(fail_health),
            )
