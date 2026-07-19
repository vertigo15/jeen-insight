"""Distributed (DB-backed) fixed-window rate limiter + budgets for connectors.

The in-process limiter in ``src/api/concurrency.py`` only bounds concurrency on a
single replica. Action proposals/executions and daily cost budgets must hold
ACROSS replicas, so counters live in the DB (``connector_rate_counters``). This is
a fixed-window counter — coarse but sufficient to cap abuse and cost; keys are
namespaced per (tenant | user | connector | action) and per window length.

The decision math is a pure function (:func:`decide_fixed_window`) so it can be
unit-tested without a database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowDecision:
    allowed: bool
    new_count: int
    reset_window: bool


def decide_fixed_window(
    *,
    now: datetime,
    window_start: Optional[datetime],
    count: int,
    limit: int,
    window_seconds: int,
) -> WindowDecision:
    """Pure fixed-window decision.

    A non-positive ``limit`` disables the limit (always allowed). When the stored
    window has elapsed the counter resets to 1; otherwise it increments. ``allowed``
    is True while the (post-increment) count is within ``limit``.
    """
    if limit <= 0:
        return WindowDecision(allowed=True, new_count=1, reset_window=True)
    if window_start is None or (window_start + timedelta(seconds=window_seconds)) <= now:
        return WindowDecision(allowed=(1 <= limit), new_count=1, reset_window=True)
    new_count = count + 1
    return WindowDecision(allowed=(new_count <= limit), new_count=new_count, reset_window=False)


class RateLimiter:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Atomically consume one unit for ``key``; return whether it is allowed.

        Fails OPEN on a DB error (a limiter outage must not take down the feature)
        but logs — the security controls (policy, gate, confirm) are independent.
        """
        if limit <= 0:
            return True
        now = datetime.now(timezone.utc)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT window_start, count FROM connector_rate_counters "
                        "WHERE key=$1 FOR UPDATE",
                        key,
                    )
                    if row is None:
                        d = decide_fixed_window(
                            now=now, window_start=None, count=0,
                            limit=limit, window_seconds=window_seconds,
                        )
                        await conn.execute(
                            "INSERT INTO connector_rate_counters (key, window_start, count) "
                            "VALUES ($1,$2,$3) "
                            "ON CONFLICT (key) DO UPDATE SET count = connector_rate_counters.count + 1",
                            key, now, d.new_count,
                        )
                        return d.allowed
                    d = decide_fixed_window(
                        now=now, window_start=row["window_start"], count=row["count"],
                        limit=limit, window_seconds=window_seconds,
                    )
                    if d.reset_window:
                        await conn.execute(
                            "UPDATE connector_rate_counters SET window_start=$2, count=$3 WHERE key=$1",
                            key, now, d.new_count,
                        )
                    else:
                        await conn.execute(
                            "UPDATE connector_rate_counters SET count=$2 WHERE key=$1",
                            key, d.new_count,
                        )
                    return d.allowed
        except Exception:  # noqa: BLE001
            logger.exception("rate_limiter: DB error for key=%s (failing open)", key)
            return True
