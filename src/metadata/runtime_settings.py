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
  - ``sql_filter_resolution_enabled``    bool   text-to-SQL filter grounding
  - ``sql_filter_max_domain_values``     int    distinct values probed per column
  - ``sql_filter_match_threshold``       float  fuzzy-match score cutoff (0-100)
  - ``sql_filter_lookup_timeout_ms``     int    per-filter value lookup deadline
  - ``sql_filter_cache_ttl_seconds``     int    user-scoped value-domain cache TTL
  - ``sql_filter_metadata_evidence_enabled`` bool  read Schema Modeler profiles/captured values first
  - ``sql_filter_value_visibility``      enum   none | source_wide | user_scoped
  - ``sql_filter_unverified_execution``  enum   ask | allow
  - ``sql_filter_source_probe_enabled``  bool   confirm/search on the source (server-side timeout only)
  - ``sql_filter_source_distinct_enabled`` bool SELECT DISTINCT small uncaptured domains
  - ``sql_filter_probe_denylist``        text   comma-separated table.column globs never probed
  - ``sql_filter_existence_max_age_hours`` int  metadata age accepted to prove a value exists
  - ``sql_filter_absence_max_age_hours`` int    metadata age accepted to tell the user it does not

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
    """How one key is parsed and constrained.

    ``lo``/``hi`` apply to numbers; ``choices`` to ``enum`` keys; ``text`` keys
    are free strings capped at ``hi`` characters (0 = 512).
    """

    kind: str  # "int" | "float" | "bool" | "enum" | "text"
    lo: float = 0.0
    hi: float = 0.0
    choices: tuple = ()


VALUE_VISIBILITY_CHOICES = ("none", "source_wide", "user_scoped")
UNVERIFIED_EXECUTION_CHOICES = ("ask", "allow")

# Allowed keys + parse/clamp rules. Defaults come from src/config.py.
_SPECS: Dict[str, _Spec] = {
    "db_statement_timeout_ms": _Spec("int", 0, 600_000),   # 0 = no timeout, up to 10 min
    "max_result_rows": _Spec("int", 1, 1_000_000),
    "conversation_context_turns": _Spec("int", 0, 50),
    "dax_entity_resolution_enabled": _Spec("bool"),
    "dax_entity_max_domain_values": _Spec("int", 1, 100_000),
    "dax_entity_match_threshold": _Spec("float", 0.0, 100.0),
    "dax_entity_cross_column_enabled": _Spec("bool"),
    "sql_filter_resolution_enabled": _Spec("bool"),
    "sql_filter_max_domain_values": _Spec("int", 1, 100_000),
    "sql_filter_match_threshold": _Spec("float", 0.0, 100.0),
    "sql_filter_lookup_timeout_ms": _Spec("int", 100, 60_000),
    "sql_filter_cache_ttl_seconds": _Spec("int", 1, 3_600),
    "sql_filter_metadata_evidence_enabled": _Spec("bool"),
    "sql_filter_value_visibility": _Spec("enum", choices=VALUE_VISIBILITY_CHOICES),
    "sql_filter_unverified_execution": _Spec("enum", choices=UNVERIFIED_EXECUTION_CHOICES),
    "sql_filter_source_probe_enabled": _Spec("bool"),
    "sql_filter_source_distinct_enabled": _Spec("bool"),
    "sql_filter_probe_denylist": _Spec("text", 0, 2_000),
    "sql_filter_existence_max_age_hours": _Spec("int", 1, 24 * 365),
    "sql_filter_absence_max_age_hours": _Spec("int", 1, 24 * 365),
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
    sql_filter_resolution_enabled: bool
    sql_filter_max_domain_values: int
    sql_filter_match_threshold: float
    sql_filter_lookup_timeout_ms: int
    sql_filter_cache_ttl_seconds: int
    sql_filter_metadata_evidence_enabled: bool
    sql_filter_value_visibility: str
    sql_filter_unverified_execution: str
    sql_filter_source_probe_enabled: bool
    sql_filter_source_distinct_enabled: bool
    sql_filter_probe_denylist: str
    sql_filter_existence_max_age_hours: int
    sql_filter_absence_max_age_hours: int


def _defaults() -> RuntimeSettings:
    return RuntimeSettings(
        db_statement_timeout_ms=settings.DB_STATEMENT_TIMEOUT_MS,
        max_result_rows=settings.MAX_RESULT_ROWS,
        conversation_context_turns=settings.CONVERSATION_CONTEXT_TURNS,
        dax_entity_resolution_enabled=settings.DAX_ENTITY_RESOLUTION_ENABLED,
        dax_entity_max_domain_values=settings.DAX_ENTITY_MAX_DOMAIN_VALUES,
        dax_entity_match_threshold=settings.DAX_ENTITY_MATCH_THRESHOLD,
        dax_entity_cross_column_enabled=settings.DAX_ENTITY_CROSS_COLUMN_ENABLED,
        sql_filter_resolution_enabled=settings.SQL_FILTER_RESOLUTION_ENABLED,
        sql_filter_max_domain_values=settings.SQL_FILTER_MAX_DOMAIN_VALUES,
        sql_filter_match_threshold=settings.SQL_FILTER_MATCH_THRESHOLD,
        sql_filter_lookup_timeout_ms=settings.SQL_FILTER_LOOKUP_TIMEOUT_MS,
        sql_filter_cache_ttl_seconds=settings.SQL_FILTER_CACHE_TTL_SECONDS,
        sql_filter_metadata_evidence_enabled=settings.SQL_FILTER_METADATA_EVIDENCE_ENABLED,
        sql_filter_value_visibility=clamp(
            "sql_filter_value_visibility", settings.SQL_FILTER_VALUE_VISIBILITY
        ),
        sql_filter_unverified_execution=clamp(
            "sql_filter_unverified_execution", settings.SQL_FILTER_UNVERIFIED_EXECUTION
        ),
        sql_filter_source_probe_enabled=settings.SQL_FILTER_SOURCE_PROBE_ENABLED,
        sql_filter_source_distinct_enabled=settings.SQL_FILTER_SOURCE_DISTINCT_ENABLED,
        sql_filter_probe_denylist=settings.SQL_FILTER_PROBE_DENYLIST or "",
        sql_filter_existence_max_age_hours=settings.SQL_FILTER_EXISTENCE_MAX_AGE_HOURS,
        sql_filter_absence_max_age_hours=settings.SQL_FILTER_ABSENCE_MAX_AGE_HOURS,
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
    if spec.kind == "enum":
        text = str(value).strip().lower()
        if text not in spec.choices:
            raise ValueError(f"{key} must be one of {', '.join(spec.choices)}")
        return text
    if spec.kind == "text":
        cap = int(spec.hi) or 512
        return str(value if value is not None else "").strip()[:cap]
    if spec.kind == "float":
        return max(spec.lo, min(spec.hi, float(value)))
    return max(int(spec.lo), min(int(spec.hi), int(value)))


def bounds() -> Dict[str, Dict[str, float]]:
    """Return clamp bounds for the UI (min/max per numeric key).

    Booleans, enums and free text are omitted: they have no numeric range, and
    the Settings UI reads this map only to constrain numeric inputs.
    """
    return {
        k: {"min": s.lo, "max": s.hi}
        for k, s in _SPECS.items()
        if s.kind in ("int", "float")
    }


def choices() -> Dict[str, tuple]:
    """Allowed values per enum key, for the Settings UI."""
    return {k: s.choices for k, s in _SPECS.items() if s.kind == "enum"}


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
