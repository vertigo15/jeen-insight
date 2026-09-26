"""Metadata-backed value evidence for filter grounding (tier 1).

Schema Modeler profiles every column of a connection and, for approved
columns, captures its distinct values:

  * ``public.metadata_column_profiles``         one row per column per profile
    run: ``distinct_count`` (+ ``distinct_is_approximate``), ``semantic_type``,
    ``column_role``, ``value_shape``, ``sensitivity_tag``, ``top_values`` /
    ``example_values`` (JSON ``[{"value", "count"}]``) and the profiler's own
    ``domain_is_complete`` verdict.
  * ``public.metadata_column_value_embeddings``  captured distinct values
    (``value_text``, ``value_count``, ``last_seen_at``, ``last_seen_snapshot``,
    a pgvector embedding for ``categorical`` / ``large_categorical`` classes)
    with a trigram index on ``value_text``.
  * ``public.metadata_relationships``            structured join edges.

The grounder reads these *before* touching the customer's warehouse: a
categorical column's complete, fresh domain lets a typo be corrected locally in
milliseconds, a reverse lookup tells which columns actually contain a value
like "mosco", and ``sensitivity_tag`` decides which columns must never be
probed or shown. A source probe is then only needed to *confirm* a candidate
the metadata could not certify.

Evidence semantics are deliberately asymmetric. A hit proves the value
*existed* when the snapshot was taken; absence proves nothing unless the
capture is complete **and** fresh enough for an absence claim. Completeness is
snapshot-proven by :class:`CaptureContract` — never inferred from an estimated
count.

Two implementations share one interface: :class:`MetadataDbValueStore` reads
the tables directly through the metadata pool (the default when the catalog
came from the DB), :class:`McpValueStore` calls the provider's
``get_column_profile`` / ``search_column_values`` tools when the catalog came
from MCP. Both degrade to "no evidence" rather than raise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from .value_index import ValueDomain, match_values, normalize

logger = logging.getLogger(__name__)

# Semantic classes whose capture can be a complete domain. ``large_categorical``
# is captured top-N by count and ``large_unit_id`` is trigram-only, so neither
# can ever prove absence.
CATEGORICAL_CLASSES = frozenset({"boolean", "categorical"})
# Column roles / shapes that hold keys, not user-facing values. They are never
# candidates for a reverse value lookup ("mosco" is not a customer id).
_KEY_ROLES = frozenset({"primary_key", "foreign_key", "identifier"})
_TEXT_TYPE_HINTS = ("char", "text", "string", "varchar", "nvarchar", "character", "uuid")
_MIN_REVERSE_SIMILARITY = 0.35
_PROFILE_CACHE_TTL = 60.0
_RELATIONSHIP_CACHE_TTL = 60.0
_MAX_PROFILE_VALUES = 5000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "t", "yes")


def _json_values(raw: Any) -> Tuple[Tuple[str, Optional[int]], ...]:
    """Parse a ``top_values`` / ``example_values`` JSON payload into (value, count)."""
    if raw is None:
        return ()
    data = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ()
    if not isinstance(data, list):
        return ()
    out: List[Tuple[str, Optional[int]]] = []
    seen: set = set()
    for item in data:
        if isinstance(item, dict):
            value = item.get("value")
            count = _as_int(item.get("count"))
        else:
            value, count = item, None
        if value is None:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append((text, count))
    return tuple(out)


def is_text_type(data_type: Optional[str]) -> bool:
    lowered = (data_type or "").lower()
    return not lowered or any(hint in lowered for hint in _TEXT_TYPE_HINTS)


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColumnProfile:
    """The latest Schema Modeler profile of one column (subset the grounder uses)."""

    table: str
    column: str
    data_type: Optional[str] = None
    semantic_type: Optional[str] = None
    column_role: Optional[str] = None
    value_shape: Optional[str] = None
    distinct_count: Optional[int] = None
    distinct_is_approximate: bool = False
    domain_is_complete: Optional[bool] = None
    sensitivity_tag: Optional[str] = None
    top_values: Tuple[Tuple[str, Optional[int]], ...] = ()
    example_values: Tuple[Tuple[str, Optional[int]], ...] = ()
    example_strategy: Optional[str] = None
    sample_method: Optional[str] = None
    status: Optional[str] = None
    profiled_at: Optional[datetime] = None
    profile_run_id: Optional[int] = None
    row_count: Optional[int] = None
    row_count_is_estimate: bool = False
    is_hidden: bool = False
    description: Optional[str] = None

    @property
    def is_sensitive(self) -> bool:
        return bool((self.sensitivity_tag or "").strip())

    @property
    def is_key_like(self) -> bool:
        return (self.column_role or "") in _KEY_ROLES or self.value_shape == "identifier"

    @property
    def is_text(self) -> bool:
        return is_text_type(self.data_type)

    @property
    def snapshot(self) -> str:
        stamp = self.profiled_at.isoformat() if self.profiled_at else ""
        return f"profile:{self.profile_run_id or ''}:{stamp}"

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.profiled_at is None:
            return None
        current = now or _utcnow()
        profiled = self.profiled_at
        if profiled.tzinfo is None:
            profiled = profiled.replace(tzinfo=timezone.utc)
        return max(0.0, (current - profiled).total_seconds())

    @property
    def domain_values(self) -> Tuple[str, ...]:
        source = self.top_values or self.example_values
        return tuple(value for value, _ in source)


@dataclass(frozen=True)
class CaptureContract:
    """When metadata evidence may be trusted for existence, absence and completeness.

    ``existence_max_age_seconds`` bounds how old a snapshot may be for "this
    value exists" (a hit); ``absence_max_age_seconds`` is stricter because
    "this value does not exist" is what gets shown to the user as a fact and
    a row inserted yesterday falsifies it. ``estimate_margin`` is the factor a
    captured count must exceed an *approximate* distinct count by before the
    capture is believed complete; an estimate alone never certifies anything.
    """

    existence_max_age_seconds: int = 7 * 86400
    absence_max_age_seconds: int = 86400
    estimate_margin: float = 2.0

    def fresh_for_existence(self, age_seconds: Optional[float]) -> bool:
        return age_seconds is not None and age_seconds <= self.existence_max_age_seconds

    def fresh_for_absence(self, age_seconds: Optional[float]) -> bool:
        return age_seconds is not None and age_seconds <= self.absence_max_age_seconds

    def profile_domain_complete(self, profile: ColumnProfile) -> bool:
        """The profiler enumerated the whole domain into ``top_values``."""
        if profile.status not in (None, "success"):
            return False
        if profile.domain_is_complete is not True or profile.distinct_is_approximate:
            return False
        if profile.distinct_count is None:
            return False
        return len(profile.top_values) >= profile.distinct_count

    def capture_complete(
        self,
        profile: Optional[ColumnProfile],
        captured_count: int,
        captured_class: Optional[str],
    ) -> bool:
        """A captured value set covers every distinct value of the column."""
        if profile is None or captured_count <= 0:
            return False
        if (captured_class or profile.semantic_type or "") not in CATEGORICAL_CLASSES:
            return False
        if profile.status not in (None, "success") or profile.distinct_count is None:
            return False
        if profile.distinct_is_approximate:
            return captured_count >= profile.distinct_count * self.estimate_margin
        return captured_count >= profile.distinct_count


@dataclass(frozen=True)
class DomainEvidence:
    """What the metadata store knows about one column's values."""

    values: Tuple[str, ...]
    complete: bool
    source: str                      # profile | captured | mcp
    snapshot: str
    age_seconds: Optional[float]
    fresh_for_existence: bool
    fresh_for_absence: bool
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def conclusive_for_absence(self) -> bool:
        return self.complete and self.fresh_for_absence

    def as_domain(self) -> ValueDomain:
        return ValueDomain(
            values=self.values,
            complete=self.complete and self.fresh_for_existence,
            source=self.source,
            snapshot=self.snapshot,
            fresh_for_absence=self.fresh_for_absence,
        )


@dataclass(frozen=True)
class ColumnHit:
    """A captured value in some column that resembles the user's literal."""

    table: str
    column: str
    value: str
    similarity: float
    count: Optional[int] = None
    semantic_type: Optional[str] = None
    source: str = "captured"

    @property
    def target(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


class ValueStore(Protocol):
    """Read-only access to Schema Modeler's value evidence for one source."""

    @property
    def available(self) -> bool: ...

    async def column_profile(self, source: str, table: str, column: str) -> Optional[ColumnProfile]: ...

    async def domain(
        self, source: str, table: str, column: str, *, needle: Optional[str] = None, limit: int = 1000
    ) -> Optional[DomainEvidence]: ...

    async def find_columns_for_value(
        self, source: str, needles: Sequence[str], *, tables: Optional[Sequence[str]] = None, limit: int = 12
    ) -> List[ColumnHit]: ...

    async def relationships(self, source: str) -> List[Relationship]: ...


class NullValueStore:
    """No metadata evidence (feature disabled or provider unavailable)."""

    available = False

    async def column_profile(self, source: str, table: str, column: str) -> Optional[ColumnProfile]:
        return None

    async def domain(self, source, table, column, *, needle=None, limit=1000):
        return None

    async def find_columns_for_value(self, source, needles, *, tables=None, limit=12):
        return []

    async def relationships(self, source: str) -> List[Relationship]:
        return []


def profile_evidence(
    profile: ColumnProfile, contract: CaptureContract, *, now: Optional[datetime] = None
) -> Optional[DomainEvidence]:
    """Evidence built from a profile's enumerated ``top_values`` alone."""
    values = profile.domain_values
    if not values:
        return None
    age = profile.age_seconds(now)
    complete = contract.profile_domain_complete(profile)
    return DomainEvidence(
        values=values,
        complete=complete,
        source="profile",
        snapshot=profile.snapshot,
        age_seconds=age,
        fresh_for_existence=contract.fresh_for_existence(age),
        fresh_for_absence=complete and contract.fresh_for_absence(age),
        counts={v: c for v, c in profile.top_values if c is not None},
    )


def rank_hits(needles: Sequence[str], hits: Iterable[ColumnHit], *, threshold: float) -> List[ColumnHit]:
    """Re-rank raw trigram hits with the local matcher and drop weak ones.

    Trigram similarity is a recall device; the shared RapidFuzz matcher is the
    precision device every other grounding decision already uses, so a hit is
    kept only when it would also be a candidate inside its own column.
    """
    kept: List[ColumnHit] = []
    for hit in hits:
        best = 0.0
        for needle in needles:
            matches = match_values(needle, [hit.value], limit=1, threshold=threshold)
            if matches:
                best = max(best, matches[0].score)
        if best:
            kept.append(ColumnHit(
                table=hit.table, column=hit.column, value=hit.value,
                similarity=round(best / 100.0, 4), count=hit.count,
                semantic_type=hit.semantic_type, source=hit.source,
            ))
    kept.sort(key=lambda h: (h.similarity, h.count or 0), reverse=True)
    return kept


# ── Metadata DB implementation ───────────────────────────────────────────────


class MetadataDbValueStore:
    """Reads profiles, captured values and relationships from the metadata Postgres.

    Accepts an open pool or an async ``pool_getter`` so the store can be built
    at graph-compile time before the pool exists.
    """

    def __init__(
        self,
        pool: Any = None,
        *,
        pool_getter: Optional[Any] = None,
        contract: Optional[CaptureContract] = None,
    ) -> None:
        self.pool = pool
        self._pool_getter = pool_getter
        self.contract = contract or CaptureContract()
        self._features: Optional[Dict[str, bool]] = None
        self._features_lock = asyncio.Lock()
        self._profile_cache: Dict[Tuple[str, str, str], Tuple[float, Optional[ColumnProfile]]] = {}
        self._relationship_cache: Dict[str, Tuple[float, List[Relationship]]] = {}

    @property
    def available(self) -> bool:
        return self.pool is not None or self._pool_getter is not None

    async def _pool(self) -> Any:
        if self.pool is None and self._pool_getter is not None:
            self.pool = await self._pool_getter()
        return self.pool

    # -- schema discovery -----------------------------------------------------

    async def features(self) -> Dict[str, bool]:
        """Which optional tables/extensions exist. Probed once, never assumed."""
        if self._features is not None:
            return self._features
        async with self._features_lock:
            if self._features is not None:
                return self._features
            found = {"profiles": False, "table_profiles": False, "captured": False, "trgm": False}
            try:
                async with (await self._pool()).acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = ANY($1::text[])
                        """,
                        ["metadata_column_profiles", "metadata_table_profiles",
                         "metadata_column_value_embeddings"],
                    )
                    names = {r["table_name"] for r in rows}
                    found["profiles"] = "metadata_column_profiles" in names
                    found["table_profiles"] = "metadata_table_profiles" in names
                    found["captured"] = "metadata_column_value_embeddings" in names
                    ext = await conn.fetchval(
                        "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"
                    )
                    found["trgm"] = bool(ext)
            except Exception as exc:  # noqa: BLE001 - evidence is optional
                logger.warning("value_store: feature probe failed (%s); metadata evidence disabled", exc)
            self._features = found
            logger.info("value_store: features %s", found)
            return found

    # -- profiles ---------------------------------------------------------------

    async def column_profile(self, source: str, table: str, column: str) -> Optional[ColumnProfile]:
        key = (source, table.lower(), column.lower())
        cached = self._profile_cache.get(key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        features = await self.features()
        if not features.get("profiles"):
            return None
        table_join = (
            "LEFT JOIN public.metadata_table_profiles tp ON tp.id = p.table_profile_id"
            if features.get("table_profiles") else ""
        )
        table_cols = (
            "tp.row_count AS row_count, tp.row_count_is_estimate AS row_count_is_estimate"
            if features.get("table_profiles") else
            "NULL::bigint AS row_count, NULL::boolean AS row_count_is_estimate"
        )
        sql = f"""
            SELECT p.table_name, p.column_name, p.data_type, p.semantic_type, p.column_role,
                   p.value_shape, p.distinct_count, p.distinct_is_approximate,
                   p.domain_is_complete, p.sensitivity_tag, p.top_values, p.example_values,
                   p.example_strategy, p.sample_method, p.status, p.profiled_at,
                   p.profile_run_id, {table_cols},
                   c.is_hidden, c.description
            FROM public.metadata_column_profiles p
            {table_join}
            LEFT JOIN public.metadata_columns c
                   ON c.source = p.source
                  AND lower(c.table_name) = lower(p.table_name)
                  AND lower(c.column_name) = lower(p.column_name)
            WHERE p.source = $1
              AND lower(p.table_name) = lower($2)
              AND lower(p.column_name) = lower($3)
            ORDER BY p.profiled_at DESC NULLS LAST
            LIMIT 1
        """
        profile: Optional[ColumnProfile] = None
        try:
            async with (await self._pool()).acquire() as conn:
                row = await conn.fetchrow(sql, source, table, column)
            if row:
                profile = _row_to_profile(row)
        except Exception as exc:  # noqa: BLE001
            logger.info("value_store: profile lookup failed for %s.%s (%s)", table, column, exc)
        self._profile_cache[key] = (now + _PROFILE_CACHE_TTL, profile)
        return profile

    # -- domains ----------------------------------------------------------------

    async def domain(
        self, source: str, table: str, column: str, *, needle: Optional[str] = None, limit: int = 1000
    ) -> Optional[DomainEvidence]:
        features = await self.features()
        profile = await self.column_profile(source, table, column)
        now = _utcnow()
        from_profile = profile_evidence(profile, self.contract, now=now) if profile else None
        if from_profile and from_profile.complete:
            return from_profile
        if not features.get("captured"):
            return from_profile
        captured = await self._captured_domain(source, table, column, needle, limit, profile, now)
        if captured is None:
            return from_profile
        if from_profile and not captured.complete and len(from_profile.values) > len(captured.values):
            # Neither is complete; keep whichever saw more of the column.
            return from_profile
        return captured

    async def _captured_domain(
        self,
        source: str,
        table: str,
        column: str,
        needle: Optional[str],
        limit: int,
        profile: Optional[ColumnProfile],
        now: datetime,
    ) -> Optional[DomainEvidence]:
        bounded = max(1, min(int(limit), _MAX_PROFILE_VALUES))
        use_similarity = bool(needle and (await self.features()).get("trgm"))
        order = "similarity(value_text, $4) DESC, value_count DESC" if use_similarity else "value_count DESC NULLS LAST"
        params: List[Any] = [source, table, column]
        if use_similarity:
            params.append(needle)
        sql = f"""
            SELECT value_text, value_count, semantic_type, last_seen_at, last_seen_snapshot,
                   count(*) OVER () AS total
            FROM public.metadata_column_value_embeddings
            WHERE source = $1 AND lower(table_name) = lower($2) AND lower(column_name) = lower($3)
            ORDER BY {order}
            LIMIT {bounded}
        """
        try:
            async with (await self._pool()).acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except Exception as exc:  # noqa: BLE001
            logger.info("value_store: captured lookup failed for %s.%s (%s)", table, column, exc)
            return None
        if not rows:
            return None
        total = int(rows[0]["total"] or len(rows))
        captured_class = rows[0]["semantic_type"]
        last_seen = max((r["last_seen_at"] for r in rows if r["last_seen_at"]), default=None)
        snapshot_no = max((r["last_seen_snapshot"] for r in rows if r["last_seen_snapshot"] is not None), default=None)
        age = None
        if last_seen is not None:
            seen = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - seen).total_seconds())
        complete = total <= bounded and self.contract.capture_complete(profile, total, captured_class)
        values = tuple(str(r["value_text"]) for r in rows if r["value_text"] is not None)
        return DomainEvidence(
            values=values,
            complete=complete,
            source="captured",
            snapshot=f"captured:{snapshot_no or ''}:{last_seen.isoformat() if last_seen else ''}",
            age_seconds=age,
            fresh_for_existence=self.contract.fresh_for_existence(age),
            fresh_for_absence=complete and self.contract.fresh_for_absence(age),
            counts={str(r["value_text"]): int(r["value_count"]) for r in rows if r["value_count"] is not None},
        )

    # -- reverse lookup ---------------------------------------------------------

    async def find_columns_for_value(
        self, source: str, needles: Sequence[str], *, tables: Optional[Sequence[str]] = None, limit: int = 12
    ) -> List[ColumnHit]:
        features = await self.features()
        clean = [str(n).strip() for n in needles if str(n or "").strip()]
        if not clean or not features.get("trgm"):
            return []
        table_filter = [t.lower() for t in (tables or []) if t]
        hits: List[ColumnHit] = []
        bounded = max(1, min(int(limit) * 3, 60))
        # Governance is applied at read time, in SQL, before any value leaves
        # the store: a hidden/deleted catalog column, or one any profile run
        # tagged sensitive, is never a reverse-lookup candidate.
        not_hidden = """
            NOT EXISTS (
                SELECT 1 FROM public.metadata_columns c
                WHERE c.source = {alias}.source
                  AND lower(c.table_name) = lower({alias}.table_name)
                  AND lower(c.column_name) = lower({alias}.column_name)
                  AND (COALESCE(c.is_hidden, FALSE) OR COALESCE(c.is_deleted, FALSE))
            )"""
        not_sensitive = """
            NOT EXISTS (
                SELECT 1 FROM public.metadata_column_profiles s
                WHERE s.source = {alias}.source
                  AND lower(s.table_name) = lower({alias}.table_name)
                  AND lower(s.column_name) = lower({alias}.column_name)
                  AND s.sensitivity_tag IS NOT NULL
            )"""
        try:
            async with (await self._pool()).acquire() as conn:
                if features.get("captured"):
                    sensitive_guard = not_sensitive.format(alias="e") if features.get("profiles") else "TRUE"
                    rows = await conn.fetch(
                        f"""
                        SELECT e.table_name, e.column_name, e.value_text, e.value_count, e.semantic_type,
                               max(similarity(e.value_text, n.needle)) AS sim
                        FROM public.metadata_column_value_embeddings e
                        JOIN unnest($2::text[]) AS n(needle) ON e.value_text % n.needle
                        WHERE e.source = $1
                          AND COALESCE(e.semantic_type, '') <> 'large_unit_id'
                          AND {not_hidden.format(alias="e")}
                          AND {sensitive_guard}
                          {"AND lower(e.table_name) = ANY($3::text[])" if table_filter else ""}
                        GROUP BY 1, 2, 3, 4, 5
                        ORDER BY sim DESC, e.value_count DESC NULLS LAST
                        LIMIT {bounded}
                        """,
                        *([source, clean, table_filter] if table_filter else [source, clean]),
                    )
                    hits.extend(
                        ColumnHit(
                            table=str(r["table_name"]), column=str(r["column_name"]),
                            value=str(r["value_text"]), similarity=float(r["sim"] or 0.0),
                            count=_as_int(r["value_count"]), semantic_type=r["semantic_type"],
                            source="captured",
                        )
                        for r in rows
                        if float(r["sim"] or 0.0) >= _MIN_REVERSE_SIMILARITY
                    )
                if features.get("profiles"):
                    rows = await conn.fetch(
                        f"""
                        WITH latest AS (
                            SELECT DISTINCT ON (lower(p.table_name), lower(p.column_name)) p.*
                            FROM public.metadata_column_profiles p
                            WHERE p.source = $1
                              AND COALESCE(p.status, 'success') = 'success'
                              AND p.sensitivity_tag IS NULL
                              AND COALESCE(p.value_shape, 'discrete') = 'discrete'
                              AND COALESCE(p.column_role, '') NOT IN ('primary_key', 'foreign_key', 'identifier', 'measure', 'timestamp')
                              AND {not_hidden.format(alias="p")}
                              AND {not_sensitive.format(alias="p")}
                              {"AND lower(p.table_name) = ANY($3::text[])" if table_filter else ""}
                            ORDER BY lower(p.table_name), lower(p.column_name), p.profiled_at DESC NULLS LAST
                        )
                        SELECT l.table_name, l.column_name, tv->>'value' AS value_text,
                               (tv->>'count')::bigint AS value_count, l.semantic_type,
                               max(similarity(tv->>'value', n.needle)) AS sim
                        FROM latest l
                        CROSS JOIN LATERAL jsonb_array_elements(
                            CASE WHEN jsonb_typeof(l.top_values) = 'array' THEN l.top_values ELSE '[]'::jsonb END
                            || CASE WHEN jsonb_typeof(l.example_values) = 'array' THEN l.example_values ELSE '[]'::jsonb END
                        ) AS tv
                        JOIN unnest($2::text[]) AS n(needle) ON similarity(tv->>'value', n.needle) >= {_MIN_REVERSE_SIMILARITY}
                        GROUP BY 1, 2, 3, 4, 5
                        ORDER BY sim DESC
                        LIMIT {bounded}
                        """,
                        *([source, clean, table_filter] if table_filter else [source, clean]),
                    )
                    seen = {(h.table.lower(), h.column.lower(), h.value) for h in hits}
                    for r in rows:
                        key = (str(r["table_name"]).lower(), str(r["column_name"]).lower(), str(r["value_text"]))
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append(ColumnHit(
                            table=str(r["table_name"]), column=str(r["column_name"]),
                            value=str(r["value_text"]), similarity=float(r["sim"] or 0.0),
                            count=_as_int(r["value_count"]), semantic_type=r["semantic_type"],
                            source="profile",
                        ))
        except Exception as exc:  # noqa: BLE001
            logger.info("value_store: reverse lookup failed (%s)", exc)
            return []
        hits.sort(key=lambda h: (h.similarity, h.count or 0), reverse=True)
        return hits[: max(1, int(limit) * 3)]

    # -- relationships ----------------------------------------------------------

    async def relationships(self, source: str) -> List[Relationship]:
        cached = self._relationship_cache.get(source)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        try:
            async with (await self._pool()).acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT from_table, from_column, to_table, to_column
                    FROM public.metadata_relationships
                    WHERE source = $1
                      AND from_table IS NOT NULL AND to_table IS NOT NULL
                      AND COALESCE(is_deleted, FALSE) = FALSE
                      AND COALESCE(is_active, TRUE) = TRUE
                    """,
                    source,
                )
        except Exception as exc:  # noqa: BLE001
            # A failed read is not "no relationships": caching it would switch
            # reachability off for the whole TTL after one transient error.
            logger.info("value_store: relationship lookup failed (%s)", exc)
            return []
        edges = [
            Relationship(
                str(r["from_table"]).lower(), str(r["from_column"] or "").lower(),
                str(r["to_table"]).lower(), str(r["to_column"] or "").lower(),
            )
            for r in rows
        ]
        self._relationship_cache[source] = (now + _RELATIONSHIP_CACHE_TTL, edges)
        return edges


def _row_to_profile(row: Any) -> ColumnProfile:
    return ColumnProfile(
        table=str(row["table_name"]),
        column=str(row["column_name"]),
        data_type=row["data_type"],
        semantic_type=row["semantic_type"],
        column_role=row["column_role"],
        value_shape=row["value_shape"],
        distinct_count=_as_int(row["distinct_count"]),
        distinct_is_approximate=bool(row["distinct_is_approximate"]),
        domain_is_complete=_as_bool(row["domain_is_complete"]),
        sensitivity_tag=row["sensitivity_tag"],
        top_values=_json_values(row["top_values"]),
        example_values=_json_values(row["example_values"]),
        example_strategy=row["example_strategy"],
        sample_method=row["sample_method"],
        status=row["status"],
        profiled_at=row["profiled_at"],
        profile_run_id=_as_int(row["profile_run_id"]),
        row_count=_as_int(row["row_count"]),
        row_count_is_estimate=bool(row["row_count_is_estimate"]),
        is_hidden=bool(row["is_hidden"]),
        description=row["description"],
    )


# ── MCP implementation ───────────────────────────────────────────────────────


class McpValueStore:
    """Same interface, served by the MCP provider's profile and value-search tools.

    ``fallback`` (normally the metadata-DB store) answers whatever the provider
    has not mapped; it is never consulted when the provider did answer.
    """

    def __init__(
        self,
        client: Any,
        *,
        contract: Optional[CaptureContract] = None,
        fallback: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.contract = contract or CaptureContract()
        self.fallback = fallback

    @property
    def available(self) -> bool:
        return self.client is not None

    async def column_profile(self, source: str, table: str, column: str) -> Optional[ColumnProfile]:
        getter = getattr(self.client, "get_column_profile", None)
        raw = None
        if getter is not None:
            try:
                raw = await getter(source, table=table, column=column)
            except Exception as exc:  # noqa: BLE001
                logger.info("value_store(mcp): profile lookup failed for %s.%s (%s)", table, column, exc)
        if raw is None:
            if self.fallback is not None:
                return await self.fallback.column_profile(source, table, column)
            return None
        return profile_from_mapping(raw, table=table, column=column)

    async def domain(self, source, table, column, *, needle=None, limit=1000):
        profile = await self.column_profile(source, table, column)
        now = _utcnow()
        from_profile = profile_evidence(profile, self.contract, now=now) if profile else None
        if from_profile and from_profile.complete:
            return from_profile
        try:
            result = await self.client.search_column_values(
                source, table=table, column=column, query=needle or "", limit=min(int(limit), 100),
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("value_store(mcp): value search failed for %s.%s (%s)", table, column, exc)
            result = {}
        values = tuple(str(v) for v in (result.get("values") or []))
        if not values:
            if self.fallback is not None:
                fallback = await self.fallback.domain(source, table, column, needle=needle, limit=limit)
                if fallback is not None:
                    return fallback
            return from_profile
        complete = bool(result.get("complete"))
        return DomainEvidence(
            values=values,
            complete=complete,
            source="mcp",
            snapshot=f"mcp:{result.get('snapshot') or ''}",
            age_seconds=0.0,
            fresh_for_existence=True,
            fresh_for_absence=complete,
        )

    async def find_columns_for_value(self, source, needles, *, tables=None, limit=12):
        async def _search(needle: str) -> Dict[str, Any]:
            try:
                return await self.client.search_column_values(
                    source, table=None, column=None, query=needle, limit=min(int(limit), 100),
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("value_store(mcp): reverse lookup failed (%s)", exc)
                return {}

        # One provider search per word, side by side: each takes seconds.
        wanted = [n for n in needles if str(n or "").strip()][:4]
        results = await asyncio.gather(*(_search(needle) for needle in wanted))
        hits: List[ColumnHit] = []
        for result in results:
            for match in (result or {}).get("matches") or []:
                if not isinstance(match, dict) or not match.get("column"):
                    continue
                table = str(match.get("table") or "")
                if tables and table.lower() not in {t.lower() for t in tables}:
                    continue
                hits.append(ColumnHit(
                    table=table, column=str(match["column"]), value=str(match.get("value") or ""),
                    similarity=float(match.get("score") or 0.0), count=_as_int(match.get("count")),
                    semantic_type=match.get("semantic_type"), source="mcp",
                ))
        if not hits and self.fallback is not None:
            return await self.fallback.find_columns_for_value(source, needles, tables=tables, limit=limit)
        hits.sort(key=lambda h: (h.similarity, h.count or 0), reverse=True)
        return hits[: max(1, int(limit) * 3)]

    async def relationships(self, source: str) -> List[Relationship]:
        if self.fallback is not None:
            return await self.fallback.relationships(source)
        return []


def profile_from_mapping(raw: Any, *, table: str, column: str) -> Optional[ColumnProfile]:
    """Build a profile from a provider payload with tolerant key names."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    data = raw.get("profile") if isinstance(raw.get("profile"), dict) else raw

    def pick(*names: str) -> Any:
        for name in names:
            if name in data and data[name] is not None:
                return data[name]
        return None

    profiled_raw = pick("profiled_at", "profiledAt")
    profiled_at: Optional[datetime] = None
    if isinstance(profiled_raw, str):
        try:
            profiled_at = datetime.fromisoformat(profiled_raw.replace("Z", "+00:00"))
        except ValueError:
            profiled_at = None
    elif isinstance(profiled_raw, datetime):
        profiled_at = profiled_raw
    return ColumnProfile(
        table=str(pick("table", "table_name") or table),
        column=str(pick("column", "column_name") or column),
        data_type=pick("data_type", "dataType"),
        semantic_type=pick("semantic_type", "semanticType"),
        column_role=pick("column_role", "columnRole", "role"),
        value_shape=pick("value_shape", "valueShape"),
        distinct_count=_as_int(pick("distinct_count", "distinctCount")),
        distinct_is_approximate=bool(pick("distinct_is_approximate", "distinctIsApproximate") or False),
        domain_is_complete=_as_bool(pick("domain_is_complete", "domainIsComplete")),
        sensitivity_tag=pick("sensitivity_tag", "sensitivityTag", "sensitivity"),
        top_values=_json_values(pick("top_values", "topValues")),
        example_values=_json_values(pick("example_values", "exampleValues", "examples")),
        example_strategy=pick("example_strategy", "exampleStrategy"),
        sample_method=pick("sample_method", "sampleMethod"),
        status=pick("status") or "success",
        profiled_at=profiled_at,
        profile_run_id=_as_int(pick("profile_run_id", "profileRunId")),
        row_count=_as_int(pick("row_count", "rowCount")),
        row_count_is_estimate=bool(pick("row_count_is_estimate", "rowCountIsEstimate") or False),
        is_hidden=bool(pick("is_hidden", "isHidden") or False),
        description=pick("description"),
    )


# ── Reachability ─────────────────────────────────────────────────────────────


def reachable_tables(
    anchors: Iterable[str], edges: Sequence[Relationship], *, max_hops: int = 2
) -> set:
    """Tables within ``max_hops`` join edges of any anchor table (anchors included)."""
    adjacency: Dict[str, set] = {}
    for edge in edges:
        adjacency.setdefault(edge.from_table, set()).add(edge.to_table)
        adjacency.setdefault(edge.to_table, set()).add(edge.from_table)
    frontier = {str(a).lower() for a in anchors if a}
    seen = set(frontier)
    for _ in range(max_hops):
        nxt: set = set()
        for table in frontier:
            nxt |= adjacency.get(table, set()) - seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return seen


__all__ = [
    "CATEGORICAL_CLASSES",
    "CaptureContract",
    "ColumnHit",
    "ColumnProfile",
    "DomainEvidence",
    "McpValueStore",
    "MetadataDbValueStore",
    "NullValueStore",
    "Relationship",
    "ValueStore",
    "is_text_type",
    "normalize",
    "profile_evidence",
    "profile_from_mapping",
    "rank_hits",
    "reachable_tables",
]
