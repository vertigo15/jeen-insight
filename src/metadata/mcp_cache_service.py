"""Two-layer MCP response cache.

L1 — in-memory dict keyed by (mcp_server_id, source_key, cache_key).
     Sub-millisecond lookup; lost on restart.

L2 — insights_mcp_cache table in Postgres.
     Survives restarts; serves stale data as a fallback when the MCP
     server is unreachable.

When cache_ttl_seconds == 0 (UI option "No cache"), both layers are
bypassed entirely — every query goes directly to the MCP server.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)

# ── Cache key constants ───────────────────────────────────────────────────────

NO_CACHE_TTL      = 0
SOURCE_GLOBAL     = "__global__"   # source_key for the connection list

KEY_CONNECTIONS   = "connections"
KEY_TABLES        = "tables"
KEY_COLUMNS       = "columns"
KEY_RELATIONSHIPS = "relationships"
KEY_BUSINESS_TERMS  = "business_terms"
KEY_KNOWLEDGE_PAIRS = "knowledge_pairs"

ALL_CATALOG_KEYS: List[str] = [
    KEY_TABLES, KEY_COLUMNS, KEY_RELATIONSHIPS,
    KEY_BUSINESS_TERMS, KEY_KNOWLEDGE_PAIRS,
]


# ── Result wrapper ────────────────────────────────────────────────────────────

@dataclass
class CacheResult:
    payload: Any                       # the cached data
    source: str                        # 'l1' | 'l2' | 'l2_stale'
    fetched_at: Optional[datetime] = None
    expires_at: Optional[datetime]  = None
    is_stale: bool = False


# ── Service ───────────────────────────────────────────────────────────────────

class McpCacheService:
    """
    Two-layer cache for MCP catalog responses.

    Usage
    -----
    result = await cache.get(config_id, source_key, KEY_TABLES, ttl)
    if result is None or result.is_stale:
        data = await mcp_server.call_tool(...)
        await cache.set(config_id, source_key, KEY_TABLES, data, ttl)
    else:
        data = result.payload
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        # L1: (config_id, source_key, cache_key) → (mono_expires_at, CacheResult)
        self._l1: Dict[Tuple[int, str, str], Tuple[float, CacheResult]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(
        self,
        mcp_server_id: int,
        source_key: str,
        cache_key: str,
        ttl_seconds: int,
    ) -> Optional[CacheResult]:
        """
        Return a CacheResult if any entry (fresh or stale) exists.
        Returns None only when no entry has ever been stored, or TTL == 0.

        The caller should check result.is_stale to decide whether to refresh.
        """
        if ttl_seconds == NO_CACHE_TTL:
            return None

        # L1 fast path.
        l1_key = (mcp_server_id, source_key, cache_key)
        l1_hit = self._l1.get(l1_key)
        if l1_hit:
            mono_exp, result = l1_hit
            if time.monotonic() < mono_exp:
                return result   # fresh L1 hit

        # L2 DB path.
        return await self._get_from_db(mcp_server_id, source_key, cache_key)

    async def set(
        self,
        mcp_server_id: int,
        source_key: str,
        cache_key: str,
        payload: Any,
        ttl_seconds: int,
    ) -> None:
        """Write payload to L1 and L2. No-op when ttl_seconds == 0."""
        if ttl_seconds == NO_CACHE_TTL:
            return

        now = datetime.now(tz=timezone.utc)
        result = CacheResult(
            payload=payload,
            source="l1",
            fetched_at=now,
            is_stale=False,
        )

        # L1 write.
        l1_key = (mcp_server_id, source_key, cache_key)
        self._l1[l1_key] = (time.monotonic() + ttl_seconds, result)

        # L2 write (best-effort; never block the caller on DB latency).
        try:
            await self._upsert_db(
                mcp_server_id, source_key, cache_key, payload, ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mcp_cache: L2 write failed for (%s, %s): %s — L1 still valid",
                source_key, cache_key, exc,
            )

    async def invalidate(
        self,
        mcp_server_id: int,
        source_key: Optional[str] = None,
    ) -> None:
        """
        Mark cache entries stale.

        source_key=None  → invalidate all entries for this config.
        source_key=value → invalidate that source + the global connection list.
        """
        # Evict from L1.
        drop = [
            k for k in self._l1
            if k[0] == mcp_server_id and (
                source_key is None
                or k[1] == source_key
                or k[1] == SOURCE_GLOBAL
            )
        ]
        for k in drop:
            self._l1.pop(k, None)

        # Mark L2 stale.
        async with self.pool.acquire() as conn:
            if source_key is None:
                await conn.execute(
                    "UPDATE insights_mcp_cache SET is_stale = true "
                    "WHERE mcp_server_id = $1",
                    mcp_server_id,
                )
            else:
                await conn.execute(
                    "UPDATE insights_mcp_cache SET is_stale = true "
                    "WHERE mcp_server_id = $1 AND source_key = ANY($2::text[])",
                    mcp_server_id,
                    [source_key, SOURCE_GLOBAL],
                )
        logger.info(
            "mcp_cache: invalidated config_id=%d source=%s",
            mcp_server_id, source_key or "*",
        )

    async def warm_from_db(self, mcp_server_id: int) -> int:
        """
        Load all non-stale L2 entries into L1 on startup.
        Returns the number of entries warmed.
        """
        now = datetime.now(tz=timezone.utc)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source_key, cache_key, payload, fetched_at, expires_at
                FROM insights_mcp_cache
                WHERE mcp_server_id = $1
                  AND is_stale = false
                  AND expires_at > NOW()
                """,
                mcp_server_id,
            )
        warmed = 0
        for row in rows:
            expires_at = _ensure_tz(row["expires_at"])
            remaining = (expires_at - now).total_seconds()
            if remaining <= 0:
                continue
            payload = _decode_payload(row["payload"])
            result = CacheResult(
                payload=payload,
                source="l2",
                fetched_at=row["fetched_at"],
                expires_at=expires_at,
                is_stale=False,
            )
            l1_key = (mcp_server_id, row["source_key"], row["cache_key"])
            self._l1[l1_key] = (time.monotonic() + remaining, result)
            warmed += 1
        if warmed:
            logger.info("mcp_cache: warmed %d L1 entries from DB", warmed)
        return warmed

    async def get_status(
        self, mcp_server_id: int, source_key: str
    ) -> Dict[str, Any]:
        """
        Return cache status for the UI status chip.
        Uses the 'tables' cache_key as the representative entry.
        """
        l1_key = (mcp_server_id, source_key, KEY_TABLES)
        l1_hit = self._l1.get(l1_key)
        if l1_hit:
            mono_exp, result = l1_hit
            if time.monotonic() < mono_exp:
                return {
                    "cache_hit": True,
                    "source": "l1",
                    "fetched_at": result.fetched_at.isoformat() if result.fetched_at else None,
                    "expires_at": None,
                    "is_stale": False,
                }

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT fetched_at, expires_at, is_stale
                FROM insights_mcp_cache
                WHERE mcp_server_id = $1
                  AND source_key    = $2
                  AND cache_key     = 'tables'
                LIMIT 1
                """,
                mcp_server_id, source_key,
            )
        if not row:
            return {
                "cache_hit": False, "source": None,
                "fetched_at": None, "expires_at": None, "is_stale": False,
            }

        now        = datetime.now(tz=timezone.utc)
        expires_at = _ensure_tz(row["expires_at"])
        is_fresh   = (not row["is_stale"]) and expires_at and now < expires_at

        return {
            "cache_hit":  bool(is_fresh),
            "source":     "l2",
            "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "is_stale":   bool(row["is_stale"]),
        }

    async def cleanup_expired(self, mcp_server_id: int) -> int:
        """Delete rows that have been stale for more than 7 days."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM insights_mcp_cache
                WHERE mcp_server_id = $1
                  AND is_stale      = true
                  AND fetched_at    < NOW() - INTERVAL '7 days'
                """,
                mcp_server_id,
            )
        count = int(result.split()[-1])
        if count:
            logger.info("mcp_cache: pruned %d expired rows for config_id=%d", count, mcp_server_id)
        return count

    # ── Internal DB helpers ───────────────────────────────────────────────────

    async def _get_from_db(
        self,
        mcp_server_id: int,
        source_key: str,
        cache_key: str,
    ) -> Optional[CacheResult]:
        now = datetime.now(tz=timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT payload, fetched_at, expires_at, is_stale
                FROM insights_mcp_cache
                WHERE mcp_server_id = $1
                  AND source_key    = $2
                  AND cache_key     = $3
                LIMIT 1
                """,
                mcp_server_id, source_key, cache_key,
            )
        if not row:
            return None

        payload    = _decode_payload(row["payload"])
        expires_at = _ensure_tz(row["expires_at"])
        is_stale   = bool(row["is_stale"]) or (expires_at is not None and now >= expires_at)
        source     = "l2_stale" if is_stale else "l2"

        result = CacheResult(
            payload=payload,
            source=source,
            fetched_at=row["fetched_at"],
            expires_at=expires_at,
            is_stale=is_stale,
        )

        # Warm L1 for fresh L2 hits so next request is instant.
        if not is_stale and expires_at:
            remaining = (expires_at - now).total_seconds()
            if remaining > 0:
                l1_key = (mcp_server_id, source_key, cache_key)
                self._l1[l1_key] = (time.monotonic() + remaining, result)

        return result

    async def _upsert_db(
        self,
        mcp_server_id: int,
        source_key: str,
        cache_key: str,
        payload: Any,
        ttl_seconds: int,
    ) -> None:
        payload_str = json.dumps(payload) if not isinstance(payload, str) else payload
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO insights_mcp_cache
                    (mcp_server_id, source_key, cache_key,
                     payload, fetched_at, expires_at, is_stale)
                VALUES
                    ($1, $2, $3, $4::jsonb, NOW(),
                     NOW() + ($5 * INTERVAL '1 second'), false)
                ON CONFLICT ON CONSTRAINT uq_mcp_cache_entry
                DO UPDATE SET
                    payload    = EXCLUDED.payload,
                    fetched_at = EXCLUDED.fetched_at,
                    expires_at = EXCLUDED.expires_at,
                    is_stale   = false
                """,
                mcp_server_id, source_key, cache_key, payload_str, ttl_seconds,
            )


# ── Module helpers ────────────────────────────────────────────────────────────

def _ensure_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """Return dt with UTC timezone attached if it is naive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _decode_payload(raw: Any) -> Any:
    """asyncpg returns JSONB as a dict/list; fall back to JSON parse for strings."""
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw
