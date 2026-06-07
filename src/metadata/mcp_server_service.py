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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)

# ── Column list ───────────────────────────────────────────────────────────────

_COLS = """
    id, is_active,
    server_name, endpoint, transport, auth_type, bearer_token,
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

# Required needs — server cannot be activated until these are mapped.
# NEED_LIST_SOURCES → list_connections  (connection list)
# NEED_LIST_TABLES  → get_catalog_prompt (all catalog data in one call)
REQUIRED_NEEDS = {NEED_LIST_SOURCES, NEED_LIST_TABLES}


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

def _row_to_server(row: Any) -> McpServer:
    return McpServer(
        id=row["id"],
        is_active=row["is_active"],
        server_name=row["server_name"],
        endpoint=row["endpoint"],
        transport=row["transport"],
        auth_type=row["auth_type"],
        bearer_token=row["bearer_token"],
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
        Return 'db' or 'mcp' for *source_key*.
        Falls back to global app_settings when no per-connection row exists.
        """
        if source_key:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT catalog_source FROM insights_catalog_config"
                    " WHERE source_key = $1",
                    source_key,
                )
            if row:
                return row["catalog_source"]
        # Global fallback.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM app_settings WHERE key = 'catalog_source'"
            )
        return (row["value"] if row else None) or "db"

    async def get_connection_config(self, source_key: str) -> dict:
        """Return {catalog_source, cache_ttl_seconds} for *source_key*."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT catalog_source, cache_ttl_seconds"
                " FROM insights_catalog_config WHERE source_key = $1",
                source_key,
            )
        if row:
            return {"catalog_source": row["catalog_source"],
                    "cache_ttl_seconds": row["cache_ttl_seconds"]}
        # Return defaults when no row exists yet.
        return {"catalog_source": "db", "cache_ttl_seconds": 900}

    async def set_catalog_source(
        self, source: str, source_key: Optional[str] = None
    ) -> None:
        """
        Set catalog source for *source_key* (per-connection).
        When source_key is None the global app_settings fallback is updated.
        """
        if source not in ("db", "mcp"):
            raise ValueError(f"catalog_source must be 'db' or 'mcp', got {source!r}")
        if source_key:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO insights_catalog_config
                        (source_key, catalog_source, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (source_key) DO UPDATE
                        SET catalog_source = EXCLUDED.catalog_source,
                            updated_at     = NOW()
                    """,
                    source_key, source,
                )
        else:
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
        logger.info("catalog_source → %s (source_key=%s)", source, source_key)

    async def set_connection_ttl(
        self, source_key: str, cache_ttl_seconds: int
    ) -> None:
        """Upsert the per-connection cache TTL."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO insights_catalog_config
                    (source_key, cache_ttl_seconds, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (source_key) DO UPDATE
                    SET cache_ttl_seconds = EXCLUDED.cache_ttl_seconds,
                        updated_at        = NOW()
                """,
                source_key, cache_ttl_seconds,
            )

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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO insights_mcp_servers
                    (server_name, endpoint, transport, auth_type,
                     bearer_token, cache_ttl_seconds)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING {_COLS}
                """,
                server_name, endpoint, transport, auth_type,
                bearer_token, cache_ttl_seconds,
            )
        logger.info("mcp_server: created id=%d (%s)", row["id"], server_name)
        return _row_to_server(row)

    async def update(self, server_id: int, **fields: Any) -> Optional[McpServer]:
        """Update connection/auth/cache fields. Saving resets health to NULL."""
        _allowed = {
            "server_name", "endpoint", "transport",
            "auth_type", "bearer_token", "cache_ttl_seconds",
        }
        clean = {k: v for k, v in fields.items() if k in _allowed}
        if not clean:
            return await self.get_by_id(server_id)

        # Any config change invalidates the stored health (must re-test).
        clean_with_health = {**clean}
        set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(clean_with_health)]
        set_parts += ["health = NULL", "last_checked_at = NULL", "updated_at = NOW()"]
        sql = (
            f"UPDATE insights_mcp_servers "
            f"SET {', '.join(set_parts)} "
            f"WHERE id = $1 RETURNING {_COLS}"
        )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, server_id, *clean_with_health.values())
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
        """Persist a failed test result without overwriting the full health blob."""
        import json as _json
        fail_health = {"status": "down", "error": message}
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
