"""In-process TTL + LRU cache of computed memory answers.

When a follow-up is answered from already-retrieved data (the ``from_memory``
route), the result is deterministic for a given ``(session, question)`` within a
short window. Caching it avoids repeating the LLM call for identical repeated
follow-ups (e.g. a user re-clicking a suggestion).

Strictly an optimization: a miss simply recomputes. In-process only, so it is
safe across restarts and multiple replicas (a miss just recomputes on the other
replica).
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Optional

_SEP = "\x00"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "")))
    except (TypeError, ValueError):
        return default


class AnswerCache:
    """Thread-safe TTL + LRU cache mapping a string key to an answer string."""

    def __init__(self, max_entries: int = 512, ttl_seconds: int = 600):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(session_id: object, source_key: object, question: object) -> str:
        sid = str(session_id or "").strip()
        src = str(source_key or "").strip()
        q = " ".join(str(question or "").strip().lower().split())
        if not sid or not q:
            return ""
        return f"{sid}{_SEP}{src}{_SEP}{q}"

    def get(self, key: str) -> Optional[str]:
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            inserted, value = entry
            if now - inserted > self._ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    def put(self, key: str, value: str) -> None:
        if not key or not value:
            return
        now = time.monotonic()
        with self._lock:
            # Drop expired entries opportunistically.
            expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
            for k in expired:
                self._store.pop(k, None)
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# Module-level singleton shared by the memory-answer node.
answer_cache = AnswerCache(
    max_entries=_env_int("JEEN_ANSWER_CACHE_MAX_ENTRIES", 512),
    ttl_seconds=_env_int("JEEN_ANSWER_CACHE_TTL_SECONDS", 600),
)
