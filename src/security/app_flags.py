"""Global admin master switches backed by ``app_settings``.

The connector / integration platform ships **off**. An admin must flip
``connectors_enabled`` to true before any connector, OAuth, grant, or action
route does work. Enforcement is server-side: when off, the API refuses every
connector route (404/403), the UI hides all connector surfaces, and existing
grants are inert.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

CONNECTORS_ENABLED_KEY = "connectors_enabled"

_CACHE_TTL_SECONDS = 15.0
_cached: Optional[bool] = None
_expires_at: float = 0.0


def _coerce_bool(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on", "t")


def invalidate_cache() -> None:
    global _cached, _expires_at
    _cached = None
    _expires_at = 0.0


async def get_connectors_enabled(*, use_cache: bool = True) -> bool:
    """Return whether the connector platform is globally enabled (default False)."""
    global _cached, _expires_at
    now = time.monotonic()
    if use_cache and _cached is not None and now < _expires_at:
        return _cached
    try:
        from src.metadata import get_metadata_pool

        pool = await get_metadata_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM app_settings WHERE key = $1",
                CONNECTORS_ENABLED_KEY,
            )
        value = _coerce_bool(row["value"] if row else None)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("app_flags: connectors_enabled lookup failed (%s); treating as OFF", exc)
        return False
    _cached = value
    _expires_at = now + _CACHE_TTL_SECONDS
    return value


async def set_connectors_enabled(enabled: bool) -> bool:
    """Persist the master switch and invalidate the cache. Returns the new value."""
    from src.metadata import get_metadata_pool

    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
                VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
            CONNECTORS_ENABLED_KEY,
            "true" if enabled else "false",
        )
    invalidate_cache()
    logger.info("app_flags: connectors_enabled = %s", enabled)
    return enabled


def get_connectors_enabled_sync() -> bool:
    """Synchronous read for the Flask UI layer (psycopg). Fails closed."""
    try:
        from src.auth_db import _connect  # local import; sync psycopg helper

        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                (CONNECTORS_ENABLED_KEY,),
            ).fetchone()
        return _coerce_bool(row[0] if row else None)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("app_flags(sync): connectors_enabled lookup failed (%s)", exc)
        return False
