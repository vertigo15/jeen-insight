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

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncpg

logger = logging.getLogger(__name__)

# ── Cache key constants ───────────────────────────────────────────────────────

NO_CACHE_TTL      = 0
SOURCE_GLOBAL     = "__global__"   # source_key for the connection list

KEY_CONNECTIONS   = "connections"
# The full catalog bundle (every section below plus sources) as ONE entry, so a
# reader never combines sections from two different fetches and a bundle is a
# single L1 lookup or L2 row.
KEY_CATALOG       = "catalog"
KEY_TABLES        = "tables"
KEY_COLUMNS       = "columns"
KEY_RELATIONSHIPS = "relationships"
KEY_BUSINESS_TERMS  = "business_terms"
KEY_KNOWLEDGE_PAIRS = "knowledge_pairs"
# Optional catalog evidence used to select and validate query filters. These
# remain separate from ``KEY_COLUMNS`` so an older MCP catalog cache continues
# to serve the core schema while the richer prompt is refreshed.
KEY_COLUMN_STATISTICS = "column_statistics"
KEY_COLUMN_SAMPLES    = "column_samples"

# Structured autocomplete datasets (separate from the prompt bundle sections).
KEY_TABLES_RICH        = "tables_rich"
KEY_KNOWLEDGE_QUESTIONS = "knowledge_questions"
# Columns are cached per-scope: f"{KEY_COLUMNS_STRUCT}:{table_or_all}".
KEY_COLUMNS_STRUCT      = "columns_struct"

ALL_CATALOG_KEYS: List[str] = [
    KEY_TABLES, KEY_COLUMNS, KEY_RELATIONSHIPS,
    KEY_BUSINESS_TERMS, KEY_KNOWLEDGE_PAIRS,
]

# Per-section catalog rows written before the bundle became one KEY_CATALOG
# entry (plus per-source KEY_CONNECTIONS, which held its sources text). Nothing
# reads them any more; they expire on their own.
_LEGACY_SECTION_KEYS = frozenset({
    KEY_TABLES, KEY_COLUMNS, KEY_RELATIONSHIPS, KEY_BUSINESS_TERMS,
    KEY_KNOWLEDGE_PAIRS, KEY_COLUMN_STATISTICS, KEY_COLUMN_SAMPLES,
})


def _is_legacy_row(source_key: str, cache_key: str) -> bool:
    return cache_key in _LEGACY_SECTION_KEYS or (cache_key == KEY_CONNECTIONS and source_key != SOURCE_GLOBAL)


# ── Result wrapper ────────────────────────────────────────────────────────────

@dataclass
class CacheResult:
    payload: Any                       # the cached data
    source: str                        # 'l1' | 'l2' | 'l2_stale'
    fetched_at: Optional[datetime] = None
    expires_at: Optional[datetime]  = None
    is_stale: bool = False             # past its TTL or invalidated
    # Explicitly invalidated ("refresh catalog", TTL change) rather than merely
    # past its TTL: an expired copy may be served while it refreshes, an
    # invalidated one may not.
    invalidated: bool = False
    # The source's invalidation generation this payload was produced under
    # (see McpCacheService.generation). L1 ignores an entry from an older one.
    generation: Optional[Tuple[int, int]] = None


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
        # Invalidation counters: (config_id, source_key), or (config_id, None)
        # for "every source". In-process only — like L1 itself.
        self._generations: Dict[Tuple[int, Optional[str]], int] = {}
        # Per config: writes and invalidations take turns, so an upsert can
        # never commit after an invalidation has marked its rows stale.
        self._locks: Dict[int, asyncio.Lock] = {}
        # Invalidations started but whose UPDATE has not finished, keyed like
        # _generations. Until it finishes, an L2 row may still look fresh.
        self._pending: Dict[Tuple[int, Optional[str]], int] = {}
        # Scopes whose invalidation UPDATE failed or was cancelled: their L2
        # rows may still say fresh, so they are not trusted until an
        # invalidation of the scope succeeds. L1 written since is still used.
        self._unmarked: Set[Tuple[int, Optional[str]]] = set()

    def generation(self, mcp_server_id: int, source_key: str) -> Tuple[int, int]:
        """Changes whenever this source is invalidated.

        A fetch captures it before calling the provider and passes it to
        :meth:`set`, so data fetched before an invalidation is never stored
        as fresh after it.
        """
        return (
            self._generations.get((mcp_server_id, None), 0),
            self._generations.get((mcp_server_id, source_key), 0),
        )

    def _is_current(self, mcp_server_id: int, source_key: str, result: CacheResult) -> bool:
        return result.generation is None or result.generation == self.generation(mcp_server_id, source_key)

    def _l2_untrusted(self, mcp_server_id: int, source_key: str) -> bool:
        """An invalidation of this source is under way or failed to mark its L2 rows."""
        scopes = ((mcp_server_id, None), (mcp_server_id, source_key))
        return any(self._pending.get(scope) or scope in self._unmarked for scope in scopes)

    def _lock(self, mcp_server_id: int) -> asyncio.Lock:
        lock = self._locks.get(mcp_server_id)
        if lock is None:
            lock = self._locks[mcp_server_id] = asyncio.Lock()
        return lock

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
            if time.monotonic() < mono_exp and self._is_current(mcp_server_id, source_key, result):
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
        *,
        generation: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Write payload to L1 and L2; return whether it was stored.

        No-op when ttl_seconds == 0. With ``generation`` (from
        :meth:`generation`, taken before the fetch), nothing is stored if the
        source was invalidated since. An invalidation that starts during the
        write waits for it and then marks it stale; its L1 entry is ignored
        from the moment the generation changes.
        """
        if ttl_seconds == NO_CACHE_TTL:
            return False
        async with self._lock(mcp_server_id):
            current = self.generation(mcp_server_id, source_key)
            if generation is not None and generation != current:
                return False

            now = datetime.now(tz=timezone.utc)
            result = CacheResult(
                payload=payload,
                source="l1",
                fetched_at=now,
                is_stale=False,
                generation=generation if generation is not None else current,
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
        return True

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
        # Bumped first, before any await: from here on L1 ignores the old
        # entries and a fetch that started earlier cannot store its result.
        generation_keys = (
            [(mcp_server_id, None)] if source_key is None
            else [(mcp_server_id, source_key), (mcp_server_id, SOURCE_GLOBAL)]
        )
        for generation_key in generation_keys:
            self._generations[generation_key] = self._generations.get(generation_key, 0) + 1
            self._pending[generation_key] = self._pending.get(generation_key, 0) + 1

        try:
            async with self._lock(mcp_server_id):
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
        except BaseException:
            self._unmarked.update(generation_keys)
            raise
        else:
            if source_key is None:
                self._unmarked = {scope for scope in self._unmarked if scope[0] != mcp_server_id}
            else:
                self._unmarked.difference_update(generation_keys)
        finally:
            for generation_key in generation_keys:
                remaining = self._pending.get(generation_key, 0) - 1
                if remaining > 0:
                    self._pending[generation_key] = remaining
                else:
                    self._pending.pop(generation_key, None)
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
        generations_before = dict(self._generations)
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
            if _is_legacy_row(row["source_key"], row["cache_key"]):
                continue
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
                # As of the read: an invalidation during it makes the entry ignored.
                generation=(
                    generations_before.get((mcp_server_id, None), 0),
                    generations_before.get((mcp_server_id, row["source_key"]), 0),
                ),
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
        Uses the full-catalog entry as the representative one.
        """
        l1_key = (mcp_server_id, source_key, KEY_CATALOG)
        l1_hit = self._l1.get(l1_key)
        if l1_hit:
            mono_exp, result = l1_hit
            if time.monotonic() < mono_exp and self._is_current(mcp_server_id, source_key, result):
                return {
                    "cache_hit": True,
                    "source": "l1",
                    "fetched_at": result.fetched_at.isoformat() if result.fetched_at else None,
                    "expires_at": None,
                    "is_stale": False,
                }

        generation = self.generation(mcp_server_id, source_key)
        untrusted_before = self._l2_untrusted(mcp_server_id, source_key)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT fetched_at, expires_at, is_stale
                FROM insights_mcp_cache
                WHERE mcp_server_id = $1
                  AND source_key    = $2
                  AND cache_key     = $3
                LIMIT 1
                """,
                mcp_server_id, source_key, KEY_CATALOG,
            )
        if not row:
            return {
                "cache_hit": False, "source": None,
                "fetched_at": None, "expires_at": None, "is_stale": False,
            }

        now        = datetime.now(tz=timezone.utc)
        expires_at = _ensure_tz(row["expires_at"])
        stale      = (
            bool(row["is_stale"])
            or untrusted_before
            or self._l2_untrusted(mcp_server_id, source_key)
            or self.generation(mcp_server_id, source_key) != generation
        )
        is_fresh   = (not stale) and expires_at and now < expires_at

        return {
            "cache_hit":  bool(is_fresh),
            "source":     "l2",
            "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "is_stale":   stale,
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
        generation = self.generation(mcp_server_id, source_key)
        untrusted_before = self._l2_untrusted(mcp_server_id, source_key)
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
        # An invalidation that started before this read finished (or failed)
        # may not have marked the row when the read saw it: treat it as invalidated.
        invalidated = (
            bool(row["is_stale"])
            or untrusted_before
            or self._l2_untrusted(mcp_server_id, source_key)
            or self.generation(mcp_server_id, source_key) != generation
        )
        is_stale   = invalidated or (expires_at is not None and now >= expires_at)
        source     = "l2_stale" if is_stale else "l2"

        result = CacheResult(
            payload=payload,
            source=source,
            fetched_at=row["fetched_at"],
            expires_at=expires_at,
            is_stale=is_stale,
            invalidated=invalidated,
            generation=generation,
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
        # Always JSON-encode. A raw string payload (e.g. the pre-formatted catalog
        # markdown bundle) is NOT valid JSON on its own, so passing it straight to
        # $4::jsonb fails with "invalid input syntax for type json". json.dumps
        # wraps it as a proper JSON string literal; _decode_payload reverses it.
        payload_str = json.dumps(payload)
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
