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
# Independent switch: whether the AGENT may autonomously call connector tools.
# Distinct from CONNECTORS_ENABLED_KEY (manual user actions). Agent tool-calling
# requires BOTH to be true; turning this off leaves the manual flow intact.
AGENT_TOOLS_ENABLED_KEY = "agent_tools_enabled"

_CACHE_TTL_SECONDS = 15.0
_cached: Optional[bool] = None
_expires_at: float = 0.0

_agent_cached: Optional[bool] = None
_agent_expires_at: float = 0.0


def _coerce_bool(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on", "t")


def invalidate_cache() -> None:
    global _cached, _expires_at, _agent_cached, _agent_expires_at
    _cached = None
    _expires_at = 0.0
    _agent_cached = None
    _agent_expires_at = 0.0


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


# ── Independent agent-tools switch ──────────────────────────────────────────

async def get_agent_tools_enabled(*, use_cache: bool = True) -> bool:
    """Return whether the AGENT may autonomously call connector tools.

    Independent of the connectors master switch. Callers that gate a live action
    (the action gate) should pass ``use_cache=False`` so a disable takes effect
    promptly rather than lingering for the cache TTL. Fails closed (default off).
    """
    global _agent_cached, _agent_expires_at
    now = time.monotonic()
    if use_cache and _agent_cached is not None and now < _agent_expires_at:
        return _agent_cached
    try:
        from src.metadata import get_metadata_pool

        pool = await get_metadata_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM app_settings WHERE key = $1",
                AGENT_TOOLS_ENABLED_KEY,
            )
        value = _coerce_bool(row["value"] if row else None)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("app_flags: agent_tools_enabled lookup failed (%s); treating as OFF", exc)
        return False
    _agent_cached = value
    _agent_expires_at = now + _CACHE_TTL_SECONDS
    return value


async def set_agent_tools_enabled(enabled: bool) -> bool:
    """Persist the agent-tools switch and invalidate the cache."""
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
            AGENT_TOOLS_ENABLED_KEY,
            "true" if enabled else "false",
        )
    invalidate_cache()
    logger.info("app_flags: agent_tools_enabled = %s", enabled)
    return enabled


def get_agent_tools_enabled_sync() -> bool:
    """Synchronous read for the Flask UI layer (psycopg). Fails closed."""
    try:
        from src.auth_db import _connect  # local import; sync psycopg helper

        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                (AGENT_TOOLS_ENABLED_KEY,),
            ).fetchone()
        return _coerce_bool(row[0] if row else None)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("app_flags(sync): agent_tools_enabled lookup failed (%s)", exc)
        return False
