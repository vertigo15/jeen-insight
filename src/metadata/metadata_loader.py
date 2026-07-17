"""Read curated metadata for the active connection and format it for the prompt.

We pull the data from tables that are already curated by `schema-modeler`:
  * public.metadata_tables          (tables)
  * public.metadata_columns         (columns)
  * public.metadata_relationships   (relationships)
  * public.metadata_sources         (sources / connection registry)
  * public.knowledge_pairs          (pre-baked Q→SQL examples)
  * public.metadata_business_terms  (business glossary)

The partitioning column on every table except `metadata_sources` is `source`,
matching `metadata_sources.source_key`.

The output of `load_all` is a dict keyed by the placeholders in the system
prompt (`tables`, `columns`, `relationships`, `sources`, `knowledge_pairs`,
`business_terms`), each value already formatted as a multi-line string ready
for `str.format` injection.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import asyncpg

from .catalog_filter import filter_tables_rich, filter_columns

logger = logging.getLogger(__name__)

# Cache TTL in seconds. Curated metadata changes rarely; 60s is a safe default
# so the prompt feels fresh without hammering the metadata DB.
_CACHE_TTL_SECONDS = 60


class MetadataLoader:
    """Loads curated metadata bundles per `source_key`, with a small TTL cache."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        # cache[source_key] = (expires_at_epoch, bundle_dict)
        self._cache: Dict[str, tuple[float, Dict[str, str]]] = {}
        # Whether `metadata_sources` exposes `connection_schema` or `database_schema`.
        # Probed lazily on first use.
        self._schema_column: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def load_all(self, source_key: str) -> Dict[str, str]:
        """Return a dict with all six prompt placeholders for `source_key`."""
        now = time.monotonic()
        cached = self._cache.get(source_key)
        if cached and cached[0] > now:
            return cached[1]

        await self._probe_schema_column()

        tables = await self._load_tables(source_key)
        columns = await self._load_columns(source_key)
        relationships = await self._load_relationships(source_key)
        sources = await self._load_sources(source_key)
        knowledge_pairs = await self._load_knowledge_pairs(source_key)
        business_terms = await self._load_business_terms(source_key)

        bundle: Dict[str, str] = {
            "tables": _format_lines(tables, empty="No tables registered."),
            "columns": _format_lines(columns, empty="No columns registered."),
            "relationships": _format_relationships(relationships),
            "sources": _format_lines(sources, empty="No source description."),
            "knowledge_pairs": _format_lines(
                knowledge_pairs, empty="No knowledge pairs registered."
            ),
            "business_terms": _format_lines(
                business_terms, empty="No business terms registered."
            ),
        }
        self._cache[source_key] = (now + _CACHE_TTL_SECONDS, bundle)
        return bundle

    def invalidate(self, source_key: Optional[str] = None) -> None:
        """Drop the cache for a single source (or everything if None)."""
        if source_key is None:
            self._cache.clear()
        else:
            self._cache.pop(source_key, None)
            self._cache.pop(f"kq::{source_key}", None)
            self._cache.pop(f"tables_rich::{source_key}", None)
            # Drop column caches (per-table and ALL).
            for k in [k for k in self._cache if k.startswith(f"cols::{source_key}::")]:
                self._cache.pop(k, None)

    async def load_tables_rich(
        self, source_key: str
    ) -> List[Dict[str, Any]]:
        """Return all catalogued tables with description + visible column count.

        Sourced entirely from the metadata DB — not from a live connection.
        Cached under ``tables_rich::<source_key>``.
        """
        now = time.monotonic()
        cache_key = f"tables_rich::{source_key}"
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]  # type: ignore[return-value]

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    t.table_name,
                    t.table_description,
                    COUNT(c.column_name)
                        FILTER (WHERE NOT COALESCE(c.is_hidden, FALSE)) AS col_count
                FROM public.metadata_tables t
                LEFT JOIN public.metadata_columns c
                    ON  c.source            = t.source
                    AND lower(c.table_name) = lower(t.table_name)
                WHERE t.source = $1
                GROUP BY t.table_name, t.table_description
                ORDER BY t.table_name
                """,
                source_key,
            )

        items: List[Dict[str, Any]] = filter_tables_rich([
            {
                "name":        r["table_name"],
                "description": r["table_description"],
                "col_count":   int(r["col_count"] or 0),
            }
            for r in rows
        ])
        self._cache[cache_key] = (now + _CACHE_TTL_SECONDS, items)
        return items

    async def load_knowledge_questions(self, source_key: str) -> List[Dict[str, Any]]:
        """Lean fetch of `knowledge_pairs.question` (+ category, tags) for autocomplete.

        Cached separately under the key `("kq", source_key)` in the same
        TTL cache used by `load_all`, so warming one warms the other.
        Server-capped at 2000 rows; very large sources should not blow the
        client-side filter budget.
        """
        now = time.monotonic()
        cache_key = f"kq::{source_key}"
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT question, category, tags
                FROM public.knowledge_pairs
                WHERE source = $1
                  AND question IS NOT NULL
                  AND length(trim(question)) > 0
                ORDER BY question
                LIMIT 2000
                """,
                source_key,
            )
        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "question": r["question"],
                    "category": r["category"],
                    "tags": r["tags"],
                }
            )
        # store as a tuple shaped like the bundle entries: (expires_at, value)
        self._cache[cache_key] = (now + _CACHE_TTL_SECONDS, items)
        return items

    async def load_columns(
        self, source_key: str, table_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Lean fetch of `metadata_columns` for the autocomplete `#` trigger.

        Cached at `cols::<source_key>::<table_or_ALL>`. Server-capped at 5000
        rows so very large schemas can't blow the dropdown filter budget.
        """
        now = time.monotonic()
        scope = (table_name or "").strip() or "ALL"
        cache_key = f"cols::{source_key}::{scope}"
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        async with self.pool.acquire() as conn:
            if scope == "ALL":
                rows = await conn.fetch(
                    """
                    SELECT table_name, column_name, data_type, description,
                           is_primary_key, is_nullable, is_hidden
                    FROM public.metadata_columns
                    WHERE source = $1
                      AND COALESCE(is_hidden, FALSE) = FALSE
                    ORDER BY table_name, column_name
                    LIMIT 5000
                    """,
                    source_key,
                )
            else:
                # Case-insensitive table match: the catalog tends to lowercase
                # `table_name` while the SQL runner returns the original casing
                # (e.g. `DimProduct`). Compare in lower-case so either spelling
                # resolves to the same column set.
                rows = await conn.fetch(
                    """
                    SELECT table_name, column_name, data_type, description,
                           is_primary_key, is_nullable, is_hidden
                    FROM public.metadata_columns
                    WHERE source = $1 AND lower(table_name) = lower($2)
                      AND COALESCE(is_hidden, FALSE) = FALSE
                    ORDER BY column_name
                    LIMIT 2000
                    """,
                    source_key,
                    table_name,
                )
        items: List[Dict[str, Any]] = filter_columns([
            {
                "table": r["table_name"],
                "column": r["column_name"],
                "data_type": r["data_type"],
                "description": r["description"],
                "is_pk": bool(r["is_primary_key"]),
                "is_nullable": bool(r["is_nullable"]),
            }
            for r in rows
        ])
        self._cache[cache_key] = (now + _CACHE_TTL_SECONDS, items)
        return items

    async def metadata_summary(self, source_key: str) -> Dict[str, int]:
        """Return row counts per metadata table for a source (used by the UI)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  (SELECT COUNT(*) FROM public.metadata_tables          WHERE source = $1) AS tables,
                  (SELECT COUNT(*) FROM public.metadata_columns         WHERE source = $1) AS columns,
                  (SELECT COUNT(*) FROM public.metadata_relationships   WHERE source = $1) AS relationships,
                  (SELECT COUNT(*) FROM public.knowledge_pairs          WHERE source = $1) AS knowledge_pairs,
                  (SELECT COUNT(*) FROM public.metadata_business_terms  WHERE source = $1) AS business_terms
                """,
                source_key,
            )
        if not rows:
            return {
                "tables": 0,
                "columns": 0,
                "relationships": 0,
                "knowledge_pairs": 0,
                "business_terms": 0,
            }
        row = rows[0]
        return {k: int(row[k] or 0) for k in row.keys()}

    # ------------------------------------------------------------------
    # Internal helpers (one query per prompt placeholder)
    # ------------------------------------------------------------------
    async def _probe_schema_column(self) -> None:
        """Detect whether `metadata_sources` ships `connection_schema` or `database_schema`."""
        if self._schema_column is not None:
            return
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'metadata_sources'
                  AND column_name IN ('connection_schema', 'database_schema')
                """
            )
        names = {r["column_name"] for r in rows}
        if "connection_schema" in names:
            self._schema_column = "connection_schema"
        elif "database_schema" in names:
            self._schema_column = "database_schema"
        else:
            self._schema_column = ""  # neither column exists; sources query degrades gracefully
        logger.info("metadata_sources schema column: %r", self._schema_column)

    async def _load_tables(self, source_key: str) -> List[str]:
        """Return one line per table.  Description is omitted when absent."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    CASE
                        WHEN table_description IS NOT NULL
                             AND trim(table_description) <> ''
                        THEN table_name || ' - ' || table_description
                        ELSE table_name
                    END AS line
                FROM public.metadata_tables
                WHERE source = $1
                ORDER BY table_name
                """,
                source_key,
            )
        return [r["line"] for r in rows]

    async def _load_columns(self, source_key: str) -> List[str]:
        """Return one line per column.  Only meaningful attributes are emitted:

        - Description is omitted when absent (no 'No description' noise).
        - PK flag only shown when TRUE  (most columns are not PKs).
        - NOT NULL constraint only shown when NOT nullable (default is nullable).
        - Hidden flag omitted entirely (irrelevant to query generation).
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    table_name || '.' || column_name ||
                    ' - Type: ' || data_type ||
                    CASE
                        WHEN description IS NOT NULL AND trim(description) <> ''
                        THEN ', Description: ' || description
                        ELSE ''
                    END ||
                    CASE WHEN COALESCE(is_primary_key, FALSE) = TRUE THEN ', PK: true' ELSE '' END ||
                    CASE WHEN COALESCE(is_nullable,    TRUE)  = FALSE THEN ', NOT NULL'   ELSE '' END
                    AS line
                FROM public.metadata_columns
                WHERE source = $1
                ORDER BY table_name, column_name
                """,
                source_key,
            )
        return [r["line"] for r in rows]

    async def _load_relationships(self, source_key: str) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT relation
                FROM public.metadata_relationships
                WHERE source = $1
                ORDER BY relation
                """,
                source_key,
            )
        return [r["relation"] for r in rows]

    async def _load_sources(self, source_key: str) -> List[str]:
        col = self._schema_column or ""
        if col:
            sql = f"""
                SELECT
                    description || ' | ' || database_type || ' | ' || COALESCE({col}, '') ||
                    ' | (Active: ' || is_active || ')' AS line
                FROM public.metadata_sources
                WHERE source_key = $1
            """
        else:
            sql = """
                SELECT
                    description || ' | ' || database_type ||
                    ' | (Active: ' || is_active || ')' AS line
                FROM public.metadata_sources
                WHERE source_key = $1
            """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, source_key)
        return [r["line"] for r in rows]

    async def _load_knowledge_pairs(self, source_key: str) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    'Category: ' || COALESCE(category, 'General') ||
                    ' | Question: ' || COALESCE(question, 'No question') ||
                    ' | SQL: ' || COALESCE(sql_statement, 'No statement') ||
                    ' | Tags: ' || COALESCE(tags, 'No tags') AS line
                FROM public.knowledge_pairs
                WHERE source = $1
                ORDER BY category NULLS LAST, question
                """,
                source_key,
            )
        return [r["line"] for r in rows]

    async def _load_business_terms(self, source_key: str) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    'Term: ' || term ||
                    ' | Definition: ' || COALESCE(definition, 'No definition provided') ||
                    ' | Category: ' || COALESCE(category, 'General') AS line
                FROM public.metadata_business_terms
                WHERE source = $1
                ORDER BY category NULLS LAST, term
                """,
                source_key,
            )
        return [r["line"] for r in rows]


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------
def _format_lines(lines: List[str], empty: str) -> str:
    """Join non-empty lines with newlines; return `empty` when nothing matched."""
    cleaned = [line for line in lines if line and line.strip()]
    if not cleaned:
        return empty
    return "\n".join(f"- {line}" for line in cleaned)


def _format_relationships(lines: List[str]) -> str:
    """Render relationships as a Python-ish list literal, matching the user's example."""
    cleaned = [line for line in lines if line and line.strip()]
    if not cleaned:
        return "No relationships registered."
    body = ", ".join(f"('{line}',)" for line in cleaned)
    return f"[{body}]"
