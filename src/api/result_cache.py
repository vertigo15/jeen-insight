"""In-process LRU + TTL cache of query result sets.

Lets chart building, profiling, and insights reuse the rows a query already
produced — keyed by ``(user, connection, query_id)`` — instead of the browser
re-uploading them on every action.

This is strictly an **optimization**. Every consumer has a fallback (the client
can re-send the rows on a miss), so eviction, process restarts, or running more
than one replica never affect correctness — only how often we hit the cache.

Eviction (an entry leaves on the first of):
  * Per-user cap — each user keeps only their ``JEEN_RESULT_CACHE_PER_USER`` most
    recent queries (default 5). Running a 6th evicts that user's oldest, so one
    busy user can't push out everyone else's data (no noisy-neighbour).
  * TTL — ``JEEN_RESULT_CACHE_TTL_SECONDS`` (default 30 min) from insertion.
  * Global cap — ``JEEN_RESULT_CACHE_MAX_ENTRIES`` (default 256) as a hard memory
    ceiling across all users.
  * Process restart.

Notes / limits:
  * In-process only. With multiple API replicas a request can land on a replica
    that doesn't hold the entry → cache miss → client re-sends rows. That's the
    designed fallback. Move to Redis (or sticky sessions) for cross-replica hits.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SEP = "\x00"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "")))
    except (TypeError, ValueError):
        return default


class ResultCache:
    """Thread-safe per-user (LRU) + TTL store of ``{columns, rows}`` datasets.

    Each user gets their own small ring of recent queries (``per_user_max``);
    a global ``max_entries`` ceiling and a TTL bound total memory.
    """

    def __init__(self, max_entries: int = 256, ttl_seconds: int = 1800, per_user_max: int = 5):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._per_user = max(1, per_user_max)
        # key -> (inserted_monotonic, dataset)
        self._store: "OrderedDict[str, tuple[float, Dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _user_prefix(user_id: Any) -> str:
        return f"{str(user_id).strip()}{_SEP}"

    @classmethod
    def _key(cls, user_id: Any, connection: Any, query_id: Any) -> str:
        uid = str(user_id or "").strip()
        conn = str(connection or "").strip()
        qid = str(query_id or "").strip()
        if not uid or not conn or not qid:
            return ""
        return f"{cls._user_prefix(uid)}{conn}{_SEP}{qid}"

    def put(
        self,
        *,
        user_id: Any,
        connection: Any,
        query_id: Any,
        dataset: Optional[Dict[str, Any]],
    ) -> None:
        """Cache a result set. No-ops on missing query_id or empty data."""
        key = self._key(user_id, connection, query_id)
        if not key or not isinstance(dataset, dict):
            return
        rows = dataset.get("rows") or dataset.get("data")
        columns = dataset.get("columns")
        if not rows or not columns:
            return

        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            self._store[key] = (now, {"columns": list(columns), "rows": rows})
            self._store.move_to_end(key)
            # Keep only this user's most-recent N queries.
            self._evict_over_user_cap(user_id)
            # Global hard ceiling across all users.
            while len(self._store) > self._max:
                evicted_key, _ = self._store.popitem(last=False)
                logger.debug("result_cache: evicted %s (global capacity)", evicted_key)
        logger.debug(
            "result_cache: stored query_id=%s rows=%d (entries=%d)",
            query_id, len(rows), len(self._store),
        )

    def get(
        self, *, user_id: Any, connection: Any, query_id: Any
    ) -> Optional[Dict[str, Any]]:
        """Return the cached ``{columns, rows}`` or ``None`` on miss/expiry."""
        key = self._key(user_id, connection, query_id)
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            inserted, dataset = entry
            if now - inserted > self._ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return dataset

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            self._store.pop(k, None)

    def _evict_over_user_cap(self, user_id: Any) -> None:
        """Drop this user's least-recently-used entries beyond ``per_user_max``.
        ``_store`` is ordered LRU→MRU, so the user's keys are too — pop from the
        front until only ``per_user_max`` remain."""
        prefix = self._user_prefix(user_id)
        if not str(user_id or "").strip():
            return
        user_keys = [k for k in self._store if k.startswith(prefix)]
        for k in user_keys[: max(0, len(user_keys) - self._per_user)]:
            self._store.pop(k, None)
            logger.debug("result_cache: evicted %s (per-user cap)", k)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._store),
                "max_entries": self._max,
                "ttl_seconds": self._ttl,
                "per_user_max": self._per_user,
            }


# Module-level singleton shared across routes.
result_cache = ResultCache(
    max_entries=_env_int("JEEN_RESULT_CACHE_MAX_ENTRIES", 256),
    ttl_seconds=_env_int("JEEN_RESULT_CACHE_TTL_SECONDS", 1800),
    per_user_max=_env_int("JEEN_RESULT_CACHE_PER_USER", 5),
)
