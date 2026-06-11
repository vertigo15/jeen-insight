"""Live-editable runtime guardrails backed by ``app_settings``.

These are global knobs that admins can tune from the Settings UI without a
redeploy. Each value falls back to its ``src/config.py`` env default when no
row exists in ``app_settings``.

Keys (stored in ``app_settings``):
  - ``db_statement_timeout_ms``     int   per-statement Postgres timeout
  - ``max_result_rows``             int   hard ceiling on returned rows
  - ``conversation_context_turns``  int   short-term memory window size

Reads are served from a short-lived in-process cache so the hot query path
doesn't hit the DB on every request; ``set_runtime_setting`` invalidates it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

from src.config import settings

logger = logging.getLogger(__name__)

# Allowed keys + (min, max) clamp bounds. Defaults come from src/config.py.
_BOUNDS: Dict[str, tuple[int, int]] = {
    "db_statement_timeout_ms": (0, 600_000),      # 0 = no timeout, up to 10 min
    "max_result_rows": (1, 1_000_000),
    "conversation_context_turns": (0, 50),
}

_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class RuntimeSettings:
    db_statement_timeout_ms: int
    max_result_rows: int
    conversation_context_turns: int


def _defaults() -> RuntimeSettings:
    return RuntimeSettings(
        db_statement_timeout_ms=settings.DB_STATEMENT_TIMEOUT_MS,
        max_result_rows=settings.MAX_RESULT_ROWS,
        conversation_context_turns=settings.CONVERSATION_CONTEXT_TURNS,
    )


# Module-level cache: (value, expires_at).
_cached: Optional[RuntimeSettings] = None
_expires_at: float = 0.0


def clamp(key: str, value: int) -> int:
    lo, hi = _BOUNDS[key]
    return max(lo, min(hi, int(value)))


def bounds() -> Dict[str, Dict[str, int]]:
    """Return clamp bounds for the UI (min/max per key)."""
    return {k: {"min": lo, "max": hi} for k, (lo, hi) in _BOUNDS.items()}


def invalidate_cache() -> None:
    global _cached, _expires_at
    _cached = None
    _expires_at = 0.0


async def get_runtime_settings(*, use_cache: bool = True) -> RuntimeSettings:
    """Return the effective runtime settings (DB overrides over env defaults)."""
    global _cached, _expires_at

    now = time.monotonic()
    if use_cache and _cached is not None and now < _expires_at:
        return _cached

    defaults = _defaults()
    values = {
        "db_statement_timeout_ms": defaults.db_statement_timeout_ms,
        "max_result_rows": defaults.max_result_rows,
        "conversation_context_turns": defaults.conversation_context_turns,
    }

    try:
        from src.metadata import get_metadata_pool

        pool = await get_metadata_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])",
                list(_BOUNDS.keys()),
            )
        for r in rows:
            key = r["key"]
            raw = r["value"]
            if key in values and raw is not None:
                try:
                    values[key] = clamp(key, int(raw))
                except (TypeError, ValueError):
                    logger.warning(
                        "runtime_settings: ignoring non-int value for %s: %r",
                        key, raw,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runtime_settings: falling back to env defaults (%s)", exc
        )
        return defaults

    result = RuntimeSettings(**values)
    _cached = result
    _expires_at = now + _CACHE_TTL_SECONDS
    return result


async def set_runtime_setting(key: str, value: int) -> int:
    """Upsert a single runtime setting (clamped) and invalidate the cache.

    Returns the clamped value that was stored.
    """
    if key not in _BOUNDS:
        raise KeyError(f"Unknown runtime setting: {key}")
    clamped = clamp(key, value)

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
            key,
            str(clamped),
        )
    invalidate_cache()
    logger.info("runtime_settings: %s = %d", key, clamped)
    return clamped
