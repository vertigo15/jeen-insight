"""Catalog and prompt-building nodes.

catalog_lookup   Fetches the metadata bundle and extracts known table names.
                 Routes to MCP or DB depending on the single global
                 ``catalog_source`` setting in ``app_settings``.
prompt_builder   Assembles the system prompt and the ``structured_prompt`` dict
                 (used by the UI's "Show Prompt" panel).

Both are factory functions that close over their dependencies.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.config import settings
from src.connectors.dialects import dialect_rules_for
from src.metadata import MetadataLoader, link_bundle

logger = logging.getLogger(__name__)


# ── Catalog router ───────────────────────────────────────────────────────────────────


async def _load_catalog_bundle(
    source_key: str,
    metadata_loader: MetadataLoader,
    question: str = "",
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """
    Load the catalog bundle for *source_key*, routing to MCP or DB depending
    on the global ``app_settings.catalog_source`` setting.

    Returns ``(bundle, meta)`` where ``meta`` records which provider served the
    catalog, for MCP whether it was a cache hit or miss, and how long the load
    took — so the developer trace can show where the metadata came from.

    This is the single entry point for catalog loading. Both agents pre-load
    through it before starting the graph and both ``catalog_lookup`` nodes go
    through it on refresh, so an MCP-backed source can never end up with the
    pre-graph and in-graph loads reading different catalogs.

    Falls back silently to the metadata DB on any MCP error.
    """
    t0 = time.monotonic()
    bundle, meta = await _route_catalog_load(
        source_key, metadata_loader, question=question
    )
    meta["load_ms"] = round((time.monotonic() - t0) * 1000)
    return bundle, meta


async def _route_catalog_load(
    source_key: str,
    metadata_loader: MetadataLoader,
    *,
    question: str = "",
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Pick the provider and load. See ``_load_catalog_bundle``."""
    meta: Dict[str, Any] = {"source": "db", "cache": None}
    # Lazy import avoids a circular dependency at module load time.
    try:
        from src.api import state as _state  # noqa: PLC0415
        if _state.mcp_server_service and _state.mcp_catalog_client:
            catalog_source = await _state.mcp_server_service.get_catalog_source(source_key)
            if catalog_source == "mcp":
                meta["source"] = "mcp"
                if question.strip():
                    try:
                        bundle = await _state.mcp_catalog_client.load_filtered(
                            source_key, question
                        )
                        meta["filtered"] = True
                        logger.info(
                            "catalog_lookup: using filtered MCP provider for source_key=%s",
                            source_key,
                        )
                        return bundle, meta
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "catalog_lookup: filtered MCP load failed (%s); "
                            "using full catalog",
                            exc,
                        )
                # Probe cache state before loading so the trace can report HIT/MISS.
                try:
                    active = await _state.mcp_server_service.get_active()
                    if active:
                        status = await _state.mcp_catalog_client.get_cache_status(
                            active.id, source_key
                        )
                        meta["cache"] = (
                            "hit"
                            if status.get("cache_hit") and not status.get("is_stale")
                            else "miss"
                        )
                    else:
                        meta["cache"] = "miss"
                except Exception:  # noqa: BLE001
                    meta["cache"] = "miss"
                logger.info(
                    "catalog_lookup: using MCP provider for source_key=%s", source_key
                )
                return await _state.mcp_catalog_client.load_all(source_key), meta
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "catalog_lookup: MCP routing failed (%s) — falling back to metadata DB", exc
        )
        meta = {"source": "db", "cache": None}
    return await metadata_loader.load_all(source_key), meta


# ── Shared acquire / derive ──────────────────────────────────────────────────


async def _acquire_catalog(
    state: Dict[str, Any],
    source_key: str,
    metadata_loader: MetadataLoader,
    *,
    log_label: str,
    failure_message: str,
) -> Tuple[Dict[str, str], Dict[str, Any], int, str]:
    """Get the catalog bundle, reusing the agent's pre-graph load when possible.

    Both agents already load the catalog before ``ainvoke`` — concurrently with
    the history fetch and the audit insert — and seed it into ``metadata_bundle``.
    No node between START and the lookup reads that key, so consuming it here is
    always safe and saves a duplicate load on every single query.

    ``catalog_seeded`` is a one-shot ticket: the node clears it after consuming,
    so the explicit refresh paths (``missing_table`` in SQL, ``refresh_catalog``
    in DAX) re-enter this node and get a genuine reload. That is the whole point
    of those paths, and keying off the flag rather than off the feedback type
    means a new refresh route cannot silently inherit a stale catalog.

    Returns ``(bundle, meta, load_ms, catalog_error)``. Never raises: a failed
    load yields an empty bundle so the caller can fail closed.
    """
    seeded = state.get("metadata_bundle") or {}
    if state.get("catalog_seeded") and seeded:
        logger.info("%s: reusing pre-graph catalog for source_key=%s", log_label, source_key)
        return (
            seeded,
            {
                "source": state.get("catalog_source_used") or "db",
                "cache": state.get("catalog_cache"),
            },
            int(state.get("catalog_load_ms") or 0),
            "",
        )

    logger.info("%s: loading metadata for source_key=%s", log_label, source_key)
    try:
        bundle, meta = await _load_catalog_bundle(
            source_key,
            metadata_loader,
            question=str(state.get("question") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — fail closed rather than query blindly
        logger.error("%s: metadata load failed for source_key=%s: %s", log_label, source_key, exc)
        return {}, {"source": "db", "cache": None}, 0, failure_message
    return bundle, meta, int(meta.get("load_ms") or 0), ""


# Fail-closed wording, per engine: (error, user-facing answer).
_BLOCKED_MESSAGES: Dict[str, Tuple[str, str]] = {
    "sql": (
        "{reason} Queries are blocked until the schema (tables and columns) "
        "is registered in Settings.",
        "I don't have any catalog metadata for {display} yet, so I can't "
        "safely build a query. Please register the tables and columns in "
        "Settings (or ask an admin), then try again.",
    ),
    "dax": (
        "{reason} DAX queries are blocked until the dataset's tables, "
        "columns and measures are registered in Settings.",
        "I don't have any catalog metadata for {display} yet, so I can't "
        "safely build a DAX query. Please register the tables, columns and "
        "measures for this dataset (or ask an admin), then try again.",
    ),
}


def _derive_catalog_state(
    bundle: Dict[str, str],
    meta: Dict[str, Any],
    *,
    load_ms: int,
    catalog_error: str,
    require_catalog: bool,
    display: str,
    engine: str = "sql",
) -> Dict[str, Any]:
    """Build the state updates common to both catalog nodes.

    Covers the table allowlist, the provider/cache trace fields and the
    fail-closed gate. Callers layer their engine-specific column parsing on top:
    SQL splits plain columns, DAX additionally separates curated measures and
    detects a date table.
    """
    known_tables = _extract_table_names(bundle.get("tables", ""))
    catalog_available = bool(known_tables)

    updates: Dict[str, Any] = {
        "metadata_bundle": bundle,
        "known_tables": known_tables,
        "catalog_source_used": meta.get("source", "db"),
        "catalog_cache": meta.get("cache"),
        "catalog_load_ms": load_ms,
        "catalog_available": catalog_available,
        "catalog_error": catalog_error or None,
        "catalog_blocked": False,
        # One-shot ticket spent: a re-entry must reload.
        "catalog_seeded": False,
    }

    if require_catalog and not catalog_available:
        reason = catalog_error or f"No catalog metadata is registered for '{display}'."
        error_tpl, answer_tpl = _BLOCKED_MESSAGES[engine]
        updates["catalog_blocked"] = True
        updates["error"] = error_tpl.format(reason=reason)
        updates["answer"] = answer_tpl.format(display=display)

    return updates


# ── catalog_lookup ───────────────────────────────────────────────────────────────────────


def make_catalog_lookup(metadata_loader: MetadataLoader, require_catalog: bool = True):
    """Return an async node that loads the metadata bundle for the active source.

    When *require_catalog* is True (the default), a failed or empty catalog
    fails closed: the node sets ``catalog_blocked`` so the graph short-circuits
    to a clear error instead of letting the model query arbitrary, unvalidated
    tables.
    """

    async def catalog_lookup(state: AgentState) -> Dict[str, Any]:
        source_key = state["source_key"]
        bundle, meta, load_ms, catalog_error = await _acquire_catalog(
            state,
            source_key,
            metadata_loader,
            log_label="catalog_lookup",
            failure_message="Failed to load catalog metadata for this connection.",
        )

        table_columns, known_columns = _extract_columns(bundle.get("columns", ""))
        updates = _derive_catalog_state(
            bundle,
            meta,
            load_ms=load_ms,
            catalog_error=catalog_error,
            require_catalog=require_catalog,
            display=state.get("connection_display_name") or source_key,
            engine="sql",
        )
        updates["known_columns"] = known_columns
        updates["table_columns"] = table_columns

        logger.info(
            "catalog_lookup: %d known tables, %d known columns via %s (cache=%s, %dms)",
            len(updates["known_tables"]), len(known_columns),
            meta.get("source"), meta.get("cache"), load_ms,
        )
        if updates["catalog_blocked"]:
            logger.warning(
                "catalog_lookup: blocking query for source_key=%s — no usable catalog",
                source_key,
            )

        return updates

    return catalog_lookup


# ── prompt_builder ────────────────────────────────────────────────────────────


def make_prompt_builder(prompt_loader: PromptLoader):
    """Return a node that builds the system prompt and structured_prompt.

    Async so it can pull the active DB prompt version (Settings-UI edits) via
    ``PromptLoader.arender``; falls back to the disk template automatically.
    """

    async def prompt_builder(state: AgentState) -> Dict[str, Any]:
        bundle = state.get("metadata_bundle") or {}
        display_name = state.get("connection_display_name", "")
        db_type = state.get("database_type", "")
        source_key = state.get("source_key", "")
        catalog = state.get("connection_catalog") or ""
        schema = state.get("connection_schema") or ""
        database = state.get("connection_database") or ""
        dialect_rules = dialect_rules_for(db_type)
        question = state.get("question", "")
        history = state.get("conversation_history") or []

        # Schema linking: for large catalogs, inject only the tables/columns most
        # relevant to the question instead of the whole catalog. Validation still
        # uses the full allowlist (set in catalog_lookup), so pruning the prompt
        # never blocks a valid query. Small schemas pass through unchanged.
        prompt_bundle = bundle
        schema_pruned = False
        if settings.SCHEMA_LINK_ENABLED and question:
            try:
                prompt_bundle, schema_pruned = link_bundle(
                    bundle,
                    question,
                    min_columns=settings.SCHEMA_LINK_MIN_COLUMNS,
                    max_tables=settings.SCHEMA_LINK_MAX_TABLES,
                    max_columns=settings.SCHEMA_LINK_MAX_COLUMNS,
                    max_columns_per_table=settings.SCHEMA_LINK_MAX_COLUMNS_PER_TABLE,
                )
            except Exception:  # noqa: BLE001 — never fail a query over linking
                logger.warning("prompt_builder: schema linking failed; using full catalog",
                               exc_info=True)
                prompt_bundle, schema_pruned = bundle, False

        system_prompt = await prompt_loader.arender(
            "jeen_insights_system",
            connection_display_name=display_name,
            source_key=source_key,
            database_type=db_type,
            connection_database=database or "not specified",
            connection_catalog=catalog or "not specified",
            connection_schema=schema or "not specified",
            dialect_rules=dialect_rules,
            tables=prompt_bundle.get("tables", ""),
            columns=prompt_bundle.get("columns", ""),
            relationships=prompt_bundle.get("relationships", ""),
            sources=prompt_bundle.get("sources", ""),
            knowledge_pairs=prompt_bundle.get("knowledge_pairs", ""),
            business_terms=prompt_bundle.get("business_terms", ""),
        )

        # structured_prompt is forwarded as-is to the UI "Show Prompt" panel.
        # Mirror the pruned bundle so the panel shows exactly what the model saw.
        structured_prompt: Dict[str, Any] = {
            "tables": prompt_bundle.get("tables", ""),
            "columns": prompt_bundle.get("columns", ""),
            "relationships": prompt_bundle.get("relationships", ""),
            "sources": prompt_bundle.get("sources", ""),
            "knowledge_pairs": prompt_bundle.get("knowledge_pairs", ""),
            "business_terms": prompt_bundle.get("business_terms", ""),
            "schema_pruned": schema_pruned,
            "dialect_rules": dialect_rules,
            "conversation_history": [
                {
                    "question": qa.get("natural_language_query"),
                    "sql": qa.get("generated_sql"),
                }
                for qa in history
                if qa.get("natural_language_query") and qa.get("generated_sql")
            ],
            "current_question": question,
            "full_text": system_prompt,
            "connection": {
                "source_key": state.get("source_key"),
                "display_name": display_name,
                "database_type": db_type,
                "database": database,
                "catalog": catalog,
                "schema": schema,
            },
        }

        logger.info("prompt_builder: system prompt built (%d chars)", len(system_prompt))
        return {
            "system_prompt": system_prompt,
            "structured_prompt": structured_prompt,
            "dialect_rules": dialect_rules,
        }

    return prompt_builder


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_table_names(tables_text: str) -> List[str]:
    """Parse lower-cased table names from the metadata ``tables`` string.

    DB-backed metadata lines look like ``- TableName`` or
    ``- TableName - description``. MCP catalog prompts can use
    ``TableName: description``. Keep only the table token so validation doesn't
    mistake descriptions for table-name suffixes.
    """
    names: List[str] = []
    for line in tables_text.splitlines():
        stripped = line.lstrip("- ").strip()
        if not stripped:
            continue
        lowered = stripped.lower().rstrip(":")
        if lowered in {
            "tables",
            "tables available for querying",
            "available tables",
        }:
            continue
        table_name = stripped
        for separator in (" - ", " — ", " | ", ":"):
            if separator in table_name:
                table_name = table_name.split(separator, 1)[0].strip()
                break
        if table_name:
            parts = _catalog_identifier_parts(table_name)
            if parts:
                # Validation compares sqlglot's bare ``Table.name``. MCP may
                # return schema-qualified identifiers such as
                # ``"public"."dimdate"``; retain the table component only.
                names.append(parts[-1].lower())
    return names


def _catalog_identifier_parts(identifier: str) -> List[str]:
    """Split a catalog identifier and remove common SQL quoting.

    Catalog providers can emit ``table.column``, ``public.table.column`` or
    quoted equivalents. Metadata identifiers do not contain dots in practice,
    so taking the final components is both portable and deterministic.
    """
    parts: List[str] = []
    for raw in identifier.split("."):
        part = raw.strip()
        if len(part) >= 2 and (
            (part[0] == part[-1] and part[0] in {'"', "'", "`"})
            or (part[0] == "[" and part[-1] == "]")
        ):
            part = part[1:-1].strip()
        if part:
            parts.append(part)
    return parts


def _extract_columns(columns_text: str) -> Tuple[Dict[str, List[str]], List[str]]:
    """Parse ``table.column`` pairs from the metadata ``columns`` string.

    Lines look like ``table_name.column_name - Type: …`` (see
    ``MetadataLoader._load_columns``). Returns a lower-cased
    ``{table: [columns]}`` map and a flat, de-duplicated list of column names.
    Lines that don't match the ``table.column`` shape are skipped.
    """
    table_columns: Dict[str, List[str]] = {}
    flat: List[str] = []
    seen: set[str] = set()
    for line in columns_text.splitlines():
        stripped = line.lstrip("- ").strip()
        if not stripped:
            continue
        # The qualified name is everything before the first " - " separator.
        qualified = stripped.split(" - ")[0].strip()
        parts = _catalog_identifier_parts(qualified)
        if len(parts) < 2:
            continue
        table = parts[-2].lower()
        column = parts[-1].lower()
        if not table or not column:
            continue
        table_columns.setdefault(table, [])
        if column not in table_columns[table]:
            table_columns[table].append(column)
        if column not in seen:
            seen.add(column)
            flat.append(column)
    return table_columns, flat
