"""Per-user concurrency governor for the expensive text-to-SQL path.

A single user (or a runaway client) firing many simultaneous queries can pin the
LLM and exhaust the DB connection pool, degrading the service for everyone. This
governor caps how many queries one user may run concurrently.

It is intentionally simple and in-process (per replica): a small manual counter
guarded by an ``asyncio.Condition``. ``max_per_user <= 0`` disables it entirely.
When saturated it either rejects immediately (``wait_timeout <= 0``) or waits up
to ``wait_timeout`` seconds for a slot before rejecting.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict

logger = logging.getLogger(__name__)


class ConcurrencyLimitExceeded(Exception):
    """Raised when a user exceeds their allowed concurrent-query budget."""


class UserConcurrencyLimiter:
    """Caps concurrent operations per user key (in-process)."""

    def __init__(self, max_per_user: int, wait_timeout: float = 0.0):
        self._max = int(max_per_user or 0)
        self._wait = float(wait_timeout or 0.0)
        self._active: Dict[str, int] = {}
        self._cond = asyncio.Condition()

    @property
    def enabled(self) -> bool:
        return self._max > 0

    def active(self, key: str) -> int:
        return self._active.get(str(key), 0)

    async def acquire(self, key: str) -> None:
        if not self.enabled:
            return
        k = str(key or "anonymous")
        async with self._cond:
            if self._active.get(k, 0) >= self._max:
                if self._wait > 0:
                    try:
                        await asyncio.wait_for(
                            self._cond.wait_for(lambda: self._active.get(k, 0) < self._max),
                            timeout=self._wait,
                        )
                    except asyncio.TimeoutError:
                        raise ConcurrencyLimitExceeded(
                            f"user {k!r} exceeded {self._max} concurrent queries"
                        )
                else:
                    raise ConcurrencyLimitExceeded(
                        f"user {k!r} exceeded {self._max} concurrent queries"
                    )
            self._active[k] = self._active.get(k, 0) + 1

    async def release(self, key: str) -> None:
        if not self.enabled:
            return
        k = str(key or "anonymous")
        async with self._cond:
            remaining = self._active.get(k, 0) - 1
            if remaining <= 0:
                self._active.pop(k, None)
            else:
                self._active[k] = remaining
            self._cond.notify_all()

    @asynccontextmanager
    async def slot(self, key: str):
        """Async context manager that holds a concurrency slot for *key*."""
        await self.acquire(key)
        try:
            yield
        finally:
            await self.release(key)


def _build_default_limiter() -> UserConcurrencyLimiter:
    try:
        from src.config import settings

        return UserConcurrencyLimiter(
            max_per_user=getattr(settings, "MAX_CONCURRENT_QUERIES_PER_USER", 0),
            wait_timeout=getattr(settings, "QUERY_QUEUE_WAIT_SECONDS", 0.0),
        )
    except Exception:  # noqa: BLE001 — never block startup over the governor
        logger.warning("concurrency governor disabled (config load failed)", exc_info=True)
        return UserConcurrencyLimiter(max_per_user=0)


# Module-level singleton used by the query route.
query_limiter = _build_default_limiter()
