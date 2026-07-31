"""Live-editable runtime guardrails backed by ``app_settings``.

These are global knobs that admins can tune from the Settings UI without a
redeploy. Each value falls back to its ``src/config.py`` env default when no
row exists in ``app_settings``.

Keys (stored in ``app_settings``):
  - ``db_statement_timeout_ms``          int    per-statement Postgres timeout
  - ``max_result_rows``                  int    hard ceiling on returned rows
  - ``conversation_context_turns``       int    short-term memory window size
  - ``dax_entity_resolution_enabled``    bool   text-to-DAX entity resolution
  - ``dax_entity_max_domain_values``     int    distinct values probed per column
  - ``dax_entity_match_threshold``       float  fuzzy-match score cutoff (0-100)
  - ``dax_entity_cross_column_enabled``  bool   search sibling columns on a miss

Reads are served from a short-lived in-process cache so the hot query path
doesn't hit the DB on every request; ``set_runtime_setting`` invalidates it.

The ``dax_entity_*`` keys exist so entity resolution can be tuned — or switched
off — without a redeploy. They are read once per question and passed through
graph state, so a change never takes effect midway through a retry loop. Unlike
``src/security/app_flags.py``, an unreadable database falls back to the
``src/config.py`` env default rather than off: these govern an already-live
query path, so a transient DB blip must not silently change query behaviour.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.config import settings

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class _Spec:
    """How one key is parsed and constrained. ``lo``/``hi`` are unused for bools."""

    kind: str  # "int" | "float" | "bool"
    lo: float = 0.0
    hi: float = 0.0


# Allowed keys + parse/clamp rules. Defaults come from src/config.py.
_SPECS: Dict[str, _Spec] = {
    "db_statement_timeout_ms": _Spec("int", 0, 600_000),   # 0 = no timeout, up to 10 min
    "max_result_rows": _Spec("int", 1, 1_000_000),
    "conversation_context_turns": _Spec("int", 0, 50),
    "dax_entity_resolution_enabled": _Spec("bool"),
    "dax_entity_max_domain_values": _Spec("int", 1, 100_000),
    "dax_entity_match_threshold": _Spec("float", 0.0, 100.0),
    "dax_entity_cross_column_enabled": _Spec("bool"),
}

_TRUTHY = ("1", "true", "yes", "on", "t")

_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class RuntimeSettings:
    db_statement_timeout_ms: int
    max_result_rows: int
    conversation_context_turns: int
    dax_entity_resolution_enabled: bool
    dax_entity_max_domain_values: int
    dax_entity_match_threshold: float
    dax_entity_cross_column_enabled: bool


def _defaults() -> RuntimeSettings:
    return RuntimeSettings(
        db_statement_timeout_ms=settings.DB_STATEMENT_TIMEOUT_MS,
        max_result_rows=settings.MAX_RESULT_ROWS,
        conversation_context_turns=settings.CONVERSATION_CONTEXT_TURNS,
        dax_entity_resolution_enabled=settings.DAX_ENTITY_RESOLUTION_ENABLED,
        dax_entity_max_domain_values=settings.DAX_ENTITY_MAX_DOMAIN_VALUES,
        dax_entity_match_threshold=settings.DAX_ENTITY_MATCH_THRESHOLD,
        dax_entity_cross_column_enabled=settings.DAX_ENTITY_CROSS_COLUMN_ENABLED,
    )


# Module-level cache: (value, expires_at).
_cached: Optional[RuntimeSettings] = None
_expires_at: float = 0.0


def clamp(key: str, value: Any) -> Any:
    """Coerce ``value`` to the key's type and constrain it to the allowed range.

    Raises ``ValueError``/``TypeError`` when the value cannot be coerced, so
    callers can distinguish a bad input from a merely out-of-range one.
    """
    spec = _SPECS[key]
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in _TRUTHY
    if spec.kind == "float":
        return max(spec.lo, min(spec.hi, float(value)))
    return max(int(spec.lo), min(int(spec.hi), int(value)))


def bounds() -> Dict[str, Dict[str, float]]:
    """Return clamp bounds for the UI (min/max per numeric key).

    Booleans are omitted: they have no range, and the Settings UI reads this
    map only to constrain numeric inputs.
    """
    return {
        k: {"min": s.lo, "max": s.hi}
        for k, s in _SPECS.items()
        if s.kind != "bool"
    }


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
    values: Dict[str, Any] = {
        key: getattr(defaults, key) for key in _SPECS
    }

    try:
        from src.metadata import get_metadata_pool

        pool = await get_metadata_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])",
                list(_SPECS.keys()),
            )
        for r in rows:
            key = r["key"]
            raw = r["value"]
            if key in values and raw is not None:
                try:
                    values[key] = clamp(key, raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "runtime_settings: ignoring unparseable value for %s: %r "
                        "(keeping %r)",
                        key, raw, values[key],
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


async def set_runtime_setting(key: str, value: Any) -> Any:
    """Upsert a single runtime setting (clamped) and invalidate the cache.

    Returns the clamped value that was stored.
    """
    if key not in _SPECS:
        raise KeyError(f"Unknown runtime setting: {key}")
    try:
        clamped = clamp(key, value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid value for {key}: {value!r}") from exc

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
            "true" if clamped is True else "false" if clamped is False else str(clamped),
        )
    invalidate_cache()
    logger.info("runtime_settings: %s = %s", key, clamped)
    return clamped
