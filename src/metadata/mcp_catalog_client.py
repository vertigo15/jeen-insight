"""MCP catalog client.

Replaces MetadataLoader and ConnectionService when an active
insights_mcp_servers row exists and app_settings.catalog_source = 'mcp'.

Transport
---------
Streamable HTTP (POST) with JSON-RPC 2.0.
The server responds with Server-Sent Events (SSE) — responses are delivered
as  ``data: <json>``  lines.  The Accept header must include both
``application/json`` and ``text/event-stream`` or the server returns 406.

Actual MCP server tools (jeen-metadata-provider v1.0.0)
---------------------------------------------------------
list_connections    — returns all active connections (no args)
get_catalog_prompt  — returns full catalog as pre-formatted markdown
                      (arg: connection_id: int)
get_filtered_prompt — same but filtered by question (v1 = full prompt)

Response shape of get_catalog_prompt
--------------------------------------
A single pre-formatted markdown string with fixed ``## Section`` headers:
    ## Domain Context
    ## SQL Dialect
    ## Knowledge Pairs
    ## Business Terms
    ## Tables
    ## Columns
    ## Relationships (Foreign Keys)
    ## Source

_parse_catalog_markdown() splits this into the MetadataLoader bundle keys
so every downstream consumer (LangGraph nodes, prompt injection) is unchanged.

Connection resolution
-----------------------
MCP exposes connections by integer ``connection_id``, not by the string
``source_key`` used elsewhere.  The client maintains the connection list in
cache (SOURCE_GLOBAL / KEY_CONNECTIONS) and resolves names on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from .mcp_server_service import (
    McpServer, McpServerService,
    NEED_LIST_SOURCES, NEED_LIST_TABLES, NEED_DESCRIBE_TABLE,
    NEED_LIST_RELATIONSHIPS, NEED_BUSINESS_GLOSSARY, NEED_KNOWLEDGE_PAIRS,
    NEED_TABLES_RICH, NEED_LIST_COLUMNS, NEED_KNOWLEDGE_QUESTIONS,
    NEED_SEARCH_COLUMN_VALUES, NEED_COLUMN_PROFILE, NEED_TABLE_PROFILE,
    NEED_SOURCE_STATISTICS,
)
from .mcp_cache_service import (
    McpCacheService,
    NO_CACHE_TTL,
    KEY_CATALOG,
    KEY_CONNECTIONS,
    KEY_TABLES,
    KEY_COLUMNS,
    KEY_RELATIONSHIPS,
    KEY_BUSINESS_TERMS,
    KEY_KNOWLEDGE_PAIRS,
    KEY_TABLES_RICH,
    KEY_KNOWLEDGE_QUESTIONS,
    KEY_COLUMNS_STRUCT,
    KEY_COLUMN_STATISTICS,
    KEY_COLUMN_SAMPLES,
    SOURCE_GLOBAL,
)
from .catalog_filter import filter_tables_rich, filter_columns

logger = logging.getLogger(__name__)

_JSONRPC     = "2.0"
_CLIENT_INFO = {"name": "jeen-insight", "version": "1.0"}
_TIMEOUT_S   = 30.0

# SSE data line pattern:  data: <json>
_SSE_DATA_RE = re.compile(r"^data:\s*(.+)$", re.MULTILINE)


class McpError(Exception):
    """Raised when an MCP call returns an application-level error."""


# (column, flat line) of each native date/timestamp column, per table.
DateColumnIndex = Dict[str, List[Tuple[str, str]]]


@dataclass
class _ColumnsView:
    """A cached full-catalog columns section, normalised once, with its date index.

    Kept per source and reused while the cache hands back the same payload,
    so a question neither re-normalises nor re-scans a large catalog.
    """

    raw: str
    text: str
    dates: DateColumnIndex


@dataclass
class _CatalogFetch:
    """The shared full-catalog fetch for one source, and the cache generation it started at."""

    generation: Tuple[int, int]
    task: "asyncio.Task[Dict[str, str]]"


def _consume_task_exception(task: "asyncio.Task[Any]") -> None:
    """Mark a fire-and-forget task's failure as retrieved (it is handled elsewhere)."""
    if not task.cancelled():
        task.exception()


# ── Client ────────────────────────────────────────────────────────────────────

class McpCatalogClient:
    """
    MCP client for all catalog data.

    Public API (same interface as before — callers are unchanged):
      load_connections()       → replaces ConnectionService.list_connections()
      load_all(source_key)     → replaces MetadataLoader.load_all()
      load_filtered(source_key, question) → question-focused catalog bundle
      run_health_check(server) → rich health check + persists result
    """

    def __init__(
        self,
        server_service: McpServerService,
        cache_service: McpCacheService,
    ) -> None:
        self._srv_svc   = server_service
        self._cache_svc = cache_service
        self._http_client: Optional[httpx.AsyncClient] = None
        self._catalog_inflight: Dict[tuple[int, str], _CatalogFetch] = {}
        # Every running fetch, including ones an invalidation superseded, so
        # shutdown can cancel them all.
        self._catalog_tasks: Set["asyncio.Task[Dict[str, str]]"] = set()
        self._columns_views: Dict[tuple[int, str], _ColumnsView] = {}
        self._closing = False

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("MCP catalog client is closing")

    def _client(self) -> httpx.AsyncClient:
        self._ensure_open()
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=_TIMEOUT_S)
        return self._http_client

    async def aclose(self) -> None:
        """Close the pooled transport after all background work has drained."""
        self._closing = True
        tasks = list(self._catalog_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._catalog_inflight.clear()
        self._catalog_tasks.clear()
        self._columns_views.clear()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._http_client = None

    # ── Public catalog API ────────────────────────────────────────────────────

    async def load_connections(self) -> List[Dict[str, Any]]:
        """
        Return the connection list from MCP.

        Shape: [{"source_key": str, "connection_id": int, "display_name": str,
                 "description": str|None, "database_type": str, "is_active": bool,
                 "ai_domain_context": str|None}]

        Returns [] on any error so the caller can fall back to settings_services.
        """
        self._ensure_open()
        server = await self._srv_svc.get_active()
        if not server:
            return []
        return await self._get_connections(server)

    async def load_all(self, source_key: str) -> Dict[str, str]:
        """
        Return a MetadataLoader-compatible bundle for source_key.
        Keys: tables, columns, relationships, sources, knowledge_pairs,
        business_terms, column_statistics, column_samples.

        The full catalog is cached as one entry per MCP server and connection,
        so every conversation and user on the connection shares it. A copy
        past its TTL is still served while a background refresh runs; only a
        first load or an invalidated catalog makes the caller wait. Returns
        empty fallbacks on errors so the prompt degrades gracefully.
        """
        self._ensure_open()
        server = await self._srv_svc.get_active()
        if not server:
            return _empty_bundle()
        bundle, _state = await self._load_full(server, source_key)
        return bundle

    async def _load_full(self, server: McpServer, source_key: str) -> Tuple[Dict[str, str], str]:
        """The full catalog and how it was served.

        ``hit``, ``stale`` (expired copy, refresh started), ``invalidated`` /
        ``miss`` / ``off`` (fetched now), ``fallback`` (the fetch failed, an
        earlier copy was used) or ``failed`` (no catalog at all).
        """
        cached, state = await self._cached_bundle(server, source_key)
        if cached is not None:
            if state == "stale":
                self.refresh_in_background(server, source_key)
            return cached, state
        try:
            # Warm-cache and a first query can race; share one provider request.
            return await self._ensure_catalog_coalesced(server, source_key), state
        except Exception as exc:  # noqa: BLE001
            logger.error("mcp: load_all failed for %s: %s", source_key, exc)
            # Stale-if-error: while the provider is down, any earlier copy
            # (even an invalidated one) beats an empty catalog.
            fallback, _ = await self._cached_bundle(server, source_key, accept_invalidated=True)
            if fallback is not None:
                return fallback, "fallback"
            return _empty_bundle(), "failed"

    async def load_filtered(
        self, source_key: str, question: str
    ) -> Dict[str, str]:
        bundle, _timing = await self.load_filtered_with_meta(source_key, question)
        return bundle

    async def load_filtered_with_meta(
        self, source_key: str, question: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        """Return a question-focused catalog bundle from ``get_filtered_prompt``.

        Filtered prompts are request-specific, so they deliberately bypass the
        shared full-catalog cache. Callers should fall back to ``load_all`` when
        this optional MCP capability is unavailable or fails.

        ``timing`` carries the millisecond cost of each step plus
        ``full_restore_cache`` — how the reusable full catalog was served
        (see ``_load_full``) — so the trace can say why the date-column restore
        was fast or slow. The full catalog loads next to the question-specific
        call, so ``full_restore_ms`` is only the extra wait it added after it.
        """
        self._ensure_open()
        started = time.monotonic()
        timing: Dict[str, Any] = {
            "connection_ms": 0,
            "filtered_tool_ms": 0,
            "parse_ms": 0,
            "full_restore_ms": 0,
            "total_ms": 0,
        }
        server = await self._srv_svc.get_active()
        if not server:
            raise McpError("No active MCP server")

        tool = server.get_tool_for_need(NEED_DESCRIBE_TABLE)
        if not tool:
            raise McpError("No filtered prompt tool mapped (need: describe_table)")

        step = time.monotonic()
        conn_id = await self._resolve_connection_id(server, source_key)
        timing["connection_ms"] = int((time.monotonic() - step) * 1000)
        if conn_id is None:
            raise McpError(f"No connection found for source_key={source_key!r}")

        # The full catalog is only needed to restore date columns: load it next
        # to the question-specific call rather than after it. Usually a cache
        # hit; on a cold cache the two waits overlap. If the filtered call
        # fails, the fetch keeps going and the caller's load_all fallback joins it.
        full_task = asyncio.create_task(self._load_full(server, source_key))
        full_task.add_done_callback(_consume_task_exception)

        step = time.monotonic()
        raw = await self._call_tool(
            server,
            tool,
            {"connection_id": conn_id, "question": question},
        )
        timing["filtered_tool_ms"] = int((time.monotonic() - step) * 1000)
        step = time.monotonic()
        text = (
            raw.get("prompt", "")
            if isinstance(raw, dict) and isinstance(raw.get("prompt"), str)
            else _extract_text(raw)
        )
        if not text:
            raise McpError(
                f"Empty response from {tool} for connection_id={conn_id}"
            )

        bundle = _parse_catalog_markdown(text)
        timing["parse_ms"] = int((time.monotonic() - step) * 1000)
        if not bundle.get("tables"):
            raise McpError(
                f"No table section in response from {tool} "
                f"for connection_id={conn_id}"
            )
        # Interim: the question filter drops native date columns it deems
        # unnecessary; the time-series skills need them — see restore_date_columns.
        try:
            step = time.monotonic()
            full, timing["full_restore_cache"] = await full_task
            dates = self._columns_view(server.id, source_key, full.get("columns", "")).dates
            bundle["columns"] = restore_date_columns_from_index(bundle.get("columns", ""), dates)
            timing["full_restore_ms"] = int((time.monotonic() - step) * 1000)
        except Exception as exc:  # noqa: BLE001
            timing["full_restore_ms"] = int((time.monotonic() - step) * 1000)
            logger.warning("mcp: could not restore date columns for %s: %s", source_key, exc)
        if not bundle.get("sources"):
            bundle["sources"] = await self._build_sources(server, source_key)
        logger.info(
            "mcp: filtered catalog loaded source_key=%s connection_id=%d (%d chars) "
            "connection_ms=%d filtered_tool_ms=%d parse_ms=%d full_restore_ms=%d total_ms=%d",
            source_key,
            conn_id,
            len(text),
            timing["connection_ms"],
            timing["filtered_tool_ms"],
            timing["parse_ms"],
            timing["full_restore_ms"],
            int((time.monotonic() - started) * 1000),
        )
        timing["total_ms"] = int((time.monotonic() - started) * 1000)
        return bundle, timing

    async def search_column_values(
        self,
        source_key: str,
        *,
        table: Optional[str],
        column: Optional[str],
        query: str,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Return canonical values matching *query* in one catalogued column.

        With ``table``/``column`` omitted the provider searches every captured
        column of the source (a reverse "which column holds this value" lookup);
        each match then carries its own ``table`` / ``column``.

        Value search is deliberately a direct MCP call, rather than an entry in
        the shared catalog cache: a server may apply source-side row-level
        visibility, but the cache key has no requesting-user identity. The graph
        layer may cache an authorized response in its user-scoped value cache.

        MCP providers have not standardised parameter names for this operation.
        The health-check stores each tool's input schema, so build arguments
        from the discovered property names instead of hard-coding a provider
        contract. The return shape is stable for callers:
        ``{"values": [str, ...], "matches": [{value, table, column, score, count}],
        "complete": bool, "source": "mcp"}``.
        """
        server = await self._srv_svc.get_active()
        if not server:
            return _empty_value_search()

        tool = server.get_tool_for_need(NEED_SEARCH_COLUMN_VALUES)
        if not tool:
            return _empty_value_search()

        conn_id = await self._resolve_connection_id(server, source_key)
        if conn_id is None:
            return _empty_value_search()

        descriptor = _tool_descriptor_for_need(server, NEED_SEARCH_COLUMN_VALUES)
        args = _value_search_arguments(
            descriptor,
            connection_id=conn_id,
            table=table,
            column=column,
            query=query,
            limit=limit,
        )
        try:
            raw = await self._call_tool(server, tool, args)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "mcp: value search failed for %s.%s (%s)",
                table, column, type(exc).__name__,
            )
            return _empty_value_search()
        return _normalise_value_search(raw)

    async def get_column_profile(
        self, source_key: str, *, table: str, column: str
    ) -> Optional[Dict[str, Any]]:
        """Return the provider's profile of one column, or None when unmapped.

        The payload is passed through as-is (``value_store.profile_from_mapping``
        reads it with tolerant key names) so a provider can add fields without a
        client change.
        """
        server = await self._srv_svc.get_active()
        if not server:
            return None
        tool = server.get_tool_for_need(NEED_COLUMN_PROFILE)
        if not tool:
            return None
        conn_id = await self._resolve_connection_id(server, source_key)
        if conn_id is None:
            return None
        descriptor = _tool_descriptor_for_need(server, NEED_COLUMN_PROFILE)
        args = _value_search_arguments(
            descriptor, connection_id=conn_id, table=table, column=column, query=None, limit=None,
        )
        try:
            raw = await self._call_tool(server, tool, args)
        except Exception as exc:  # noqa: BLE001
            logger.info("mcp: column profile failed for %s.%s (%s)", table, column, type(exc).__name__)
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        return raw if isinstance(raw, dict) else None

    async def value_search_preserves_user_visibility(self) -> bool:
        """Whether the active value-search tool explicitly declares user scope.

        DAX always validates candidates with a delegated Power BI probe. This
        capability check additionally prevents even candidate lookup through an
        MCP server unless its discovery metadata says it preserves the caller's
        visibility (for example, its row-level security context).
        """
        server = await self._srv_svc.get_active()
        if not server or not server.get_tool_for_need(NEED_SEARCH_COLUMN_VALUES):
            return False
        descriptor = _tool_descriptor_for_need(server, NEED_SEARCH_COLUMN_VALUES)
        annotations = descriptor.get("annotations") or {}
        if not isinstance(annotations, dict):
            return False
        direct = (
            annotations.get("user_scoped")
            or annotations.get("userScoped")
            or annotations.get("rls_enforced")
            or annotations.get("rlsEnforced")
            or annotations.get("jeen.ai/user_scoped")
        )
        visibility = str(
            annotations.get("visibility_scope")
            or annotations.get("visibilityScope")
            or ""
        ).lower()
        explicit_true = direct is True or (
            isinstance(direct, str) and direct.strip().lower() in {"true", "1", "yes"}
        )
        return explicit_true or visibility in {"user", "caller", "rls"}

    # ── Structured autocomplete datasets (`/`, `#`, `@`) ──────────────────────

    async def _load_list_dataset(
        self,
        source_key: str,
        need: str,
        cache_key: str,
        arguments: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Fetch a structured JSON-array tool result, cached per (server, source).

        Returns ``[]`` (never raises) so the autocomplete routes degrade
        gracefully when the server lacks the tool or is unreachable.
        """
        server = await self._srv_svc.get_active()
        if not server:
            return []

        ttl = server.cache_ttl_seconds
        cached = await self._cache_svc.get(server.id, source_key, cache_key, ttl)
        if cached and not cached.is_stale:
            return cached.payload if isinstance(cached.payload, list) else []

        tool = server.get_tool_for_need(need)
        if not tool:
            logger.warning("mcp: no tool mapped for need=%s", need)
            return cached.payload if (cached and isinstance(cached.payload, list)) else []

        conn_id = await self._resolve_connection_id(server, source_key)
        if conn_id is None:
            return cached.payload if (cached and isinstance(cached.payload, list)) else []

        args = {"connection_id": conn_id, **arguments}
        generation = self._cache_svc.generation(server.id, source_key)
        try:
            raw = await self._call_tool(server, tool, args)
            items = _normalise_list(raw)
            await self._cache_svc.set(server.id, source_key, cache_key, items, ttl, generation=generation)
            return items
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp: %s failed: %s", tool, exc)
            return cached.payload if (cached and isinstance(cached.payload, list)) else []

    async def load_tables_rich(self, source_key: str) -> List[Dict[str, Any]]:
        """Mirror of MetadataLoader.load_tables_rich for the `@` table picker.

        Shape: ``[{name, description, col_count}]``. System-schema objects
        (information_schema / pg_catalog) and duplicate names are stripped so the
        picker shows only real user tables even when the MCP server harvested the
        whole database.
        """
        items = await self._load_list_dataset(
            source_key, NEED_TABLES_RICH, KEY_TABLES_RICH, {}
        )
        return filter_tables_rich(items)

    async def load_knowledge_questions(self, source_key: str) -> List[Dict[str, Any]]:
        """Mirror of MetadataLoader.load_knowledge_questions for `/` templates.

        Shape: ``[{question, category, tags}]``.
        """
        return await self._load_list_dataset(
            source_key, NEED_KNOWLEDGE_QUESTIONS, KEY_KNOWLEDGE_QUESTIONS, {}
        )

    async def load_columns(
        self, source_key: str, table_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Mirror of MetadataLoader.load_columns for `#` column autocomplete.

        Shape: ``[{table, column, data_type, description, is_pk, is_nullable}]``.
        Cached per scope (``ALL`` or a specific table).
        """
        scope = (table_name or "").strip() or "ALL"
        cache_key = f"{KEY_COLUMNS_STRUCT}:{scope.lower()}"
        arguments = {"table": table_name} if table_name else {}
        items = await self._load_list_dataset(
            source_key, NEED_LIST_COLUMNS, cache_key, arguments
        )
        return filter_columns(_flatten_columns(items, table_name))

    async def get_cache_status(
        self, mcp_server_id: int, source_key: str
    ) -> Dict[str, Any]:
        return await self._cache_svc.get_status(mcp_server_id, source_key)

    async def invalidate(
        self, mcp_server_id: int, source_key: Optional[str] = None
    ) -> None:
        for key in [k for k in self._columns_views if k[0] == mcp_server_id and source_key in (None, k[1])]:
            self._columns_views.pop(key, None)
        await self._cache_svc.invalidate(mcp_server_id, source_key)

    async def inspect_tools(self, server: McpServer) -> List[Dict[str, Any]]:
        """Return live MCP tool descriptors with Jeen catalog-need mapping.

        Used by the settings panel's tool inspector. Unlike the compact health
        blob, this keeps the schema fields so users can see what arguments each
        tool accepts before running a test call.
        """
        raw_tools = await self._list_tools(server)
        return [
            _normalise_tool_descriptor(t)
            for t in raw_tools
            if isinstance(t, dict)
        ]

    async def call_tool_for_test(
        self,
        server: McpServer,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call a tool for the admin inspector and preserve its MCP envelope.

        Runtime catalog methods use :meth:`_call_tool`, which deliberately
        normalises the first content block into the compact values expected by
        the catalog pipeline. The test inspector instead needs every content
        block, structured content, metadata, and ``isError`` for diagnosis.
        """
        result = await self._jsonrpc(
            server, "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )
        if not isinstance(result, dict):
            raise McpError(f"Invalid tools/call result from '{tool_name}'")
        return result

    # ── Health check ──────────────────────────────────────────────────────────

    async def run_health_check(self, server: McpServer) -> Dict[str, Any]:
        """
        Run a full health check:
          1. initialize  — handshake, get protocol/SDK/capabilities
          2. tools/list  — discover tools, map to catalog needs

        Persists the health blob to insights_mcp_servers and returns it.
        """
        import time as _time
        from datetime import datetime, timezone

        start = _time.monotonic()

        # Step 1: initialize
        try:
            init = await self._jsonrpc(server, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            })
            latency_ms = int((_time.monotonic() - start) * 1000)
        except Exception as exc:
            logger.warning("mcp: health check failed id=%d: %s", server.id, exc)
            return {"ok": False, "error": str(exc)}

        # Step 2: tools/list
        ping_start = _time.monotonic()
        try:
            raw_tools = await self._list_tools(server)
        except Exception as exc:
            return {"ok": False, "error": f"tools/list failed: {exc}"}
        ping_ms = int((_time.monotonic() - ping_start) * 1000)

        # Step 3: map tools to catalog needs
        tools_with_needs = [
            {
                "name":        t.get("name", ""),
                "description": t.get("description", "") or "",
                "need":        _map_tool_to_need(t.get("name", "")),
                "input_schema": (
                    t.get("inputSchema") or t.get("input_schema") or {}
                ),
                "output_schema": (
                    t.get("outputSchema") or t.get("output_schema") or {}
                ),
            }
            for t in raw_tools if isinstance(t, dict)
        ]
        discovered = sum(1 for t in tools_with_needs if t["need"])

        server_info  = init.get("serverInfo") or {}
        capabilities = list((init.get("capabilities") or {}).keys())

        health: Dict[str, Any] = {
            "status":         "healthy",
            "latency_ms":     latency_ms,
            "ping_ms":        ping_ms,
            "protocol":       init.get("protocolVersion", ""),
            "sdk":            server_info.get("name", ""),
            "server_version": server_info.get("version", ""),
            "capabilities":   capabilities,
            "tools":          tools_with_needs,
            "checked_at":     datetime.now(tz=timezone.utc).isoformat(),
        }

        await self._srv_svc.save_health(server.id, health)
        logger.info(
            "mcp: health check id=%d — %d tools, %d mapped",
            server.id, len(tools_with_needs), discovered,
        )
        return {"ok": True, "health": health}

    # ── Catalog internals ─────────────────────────────────────────────────────

    async def _get_connections(self, server: McpServer) -> List[Dict[str, Any]]:
        """Return the connection list, using L1/L2 cache when available."""
        cached = await self._cache_svc.get(
            server.id, SOURCE_GLOBAL, KEY_CONNECTIONS, server.cache_ttl_seconds
        )
        if cached and not cached.is_stale:
            logger.debug("mcp: connections from %s cache", cached.source)
            return cached.payload

        tool = server.get_tool_for_need(NEED_LIST_SOURCES)
        if not tool:
            logger.warning("mcp: no list_connections tool mapped for server id=%d", server.id)
            return cached.payload if cached else []

        # A list fetched before a refresh must not come back as fresh: the
        # refreshed catalog resolves its connection id from it.
        generation = self._cache_svc.generation(server.id, SOURCE_GLOBAL)
        try:
            raw   = await self._call_tool(server, tool, {})
            items = _normalise_connections(raw)
            await self._cache_svc.set(
                server.id, SOURCE_GLOBAL, KEY_CONNECTIONS, items, server.cache_ttl_seconds,
                generation=generation,
            )
            return items
        except Exception as exc:
            logger.warning("mcp: list_connections failed: %s", exc)
            return cached.payload if cached else []

    async def _resolve_connection_id(
        self, server: McpServer, source_key: str
    ) -> Optional[int]:
        """Map a source_key name to the MCP integer connection_id."""
        connections = await self._get_connections(server)
        for c in connections:
            if c.get("source_key") == source_key or c.get("name") == source_key:
                return c.get("connection_id")
        logger.warning("mcp: no connection_id found for source_key=%r", source_key)
        return None

    async def _ensure_catalog(
        self,
        server: McpServer,
        source_key: str,
        generation: Optional[Tuple[int, int]] = None,
    ) -> Dict[str, str]:
        """
        Call get_catalog_prompt for source_key, parse the markdown response,
        cache the bundle as one entry and return it.

        The bundle is returned directly rather than re-read from the cache, so
        with caching off (TTL 0, nothing stored) the caller still gets the
        catalog that was just fetched. ``generation`` is the cache generation
        when the fetch started: if the source is invalidated meanwhile, the
        result still reaches this fetch's waiters but is not cached.
        """
        if generation is None:
            generation = self._cache_svc.generation(server.id, source_key)
        conn_id = await self._resolve_connection_id(server, source_key)
        if conn_id is None:
            raise McpError(f"No connection found for source_key={source_key!r}")

        tool = server.get_tool_for_need(NEED_LIST_TABLES)  # mapped to get_catalog_prompt
        if not tool:
            raise McpError("No catalog prompt tool mapped (need: list_tables)")

        raw  = await self._call_tool(server, tool, {"connection_id": conn_id})
        text = _extract_text(raw)
        if not text:
            raise McpError(f"Empty response from {tool} for connection_id={conn_id}")

        sections = _parse_catalog_markdown(text)
        # sources — prefer the ## Source section from the prompt; fall back to
        # connection list metadata.
        sources_text = sections.get("sources") or await self._build_sources(server, source_key)
        bundle = {
            "tables":          sections["tables"],
            "columns":         self._columns_view(server.id, source_key, sections["columns"]).text,
            "relationships":   sections["relationships"],
            "sources":         sources_text,
            "knowledge_pairs": sections["knowledge_pairs"],
            "business_terms":  sections["business_terms"],
            "column_statistics": sections["column_statistics"],
            "column_samples":  sections["column_samples"],
        }
        stored = await self._cache_svc.set(
            server.id, source_key, KEY_CATALOG, bundle, server.cache_ttl_seconds,
            generation=generation,
        )
        logger.info(
            "mcp: catalog %s source_key=%s connection_id=%d (%d chars)",
            "cached" if stored else "fetched, not cached", source_key, conn_id, len(text),
        )
        return bundle

    def _start_catalog_fetch(
        self, server: McpServer, source_key: str
    ) -> "asyncio.Task[Dict[str, str]]":
        """The in-flight full-catalog fetch for this source, starting one if none runs.

        A fetch that started before the source was invalidated is not joined:
        its result may predate the change the invalidation was made for.
        """
        self._ensure_open()
        key = (server.id, source_key)
        generation = self._cache_svc.generation(server.id, source_key)
        current = self._catalog_inflight.get(key)
        if current is not None and current.generation == generation:
            return current.task
        task = asyncio.create_task(self._ensure_catalog(server, source_key, generation))
        self._catalog_inflight[key] = _CatalogFetch(generation, task)
        self._catalog_tasks.add(task)
        task.add_done_callback(
            lambda completed, cache_key=key: self._catalog_task_done(
                cache_key, completed
            )
        )
        return task

    async def _ensure_catalog_coalesced(
        self, server: McpServer, source_key: str
    ) -> Dict[str, str]:
        task = self._start_catalog_fetch(server, source_key)
        try:
            # Waiters share the result (it is also the cached payload): each
            # gets its own copy.
            return dict(await asyncio.shield(task))
        except asyncio.CancelledError:
            # The provider fetch remains shared and may still serve another
            # waiter. The done callback owns eviction and exception retrieval.
            raise

    def refresh_in_background(self, server: McpServer, source_key: str) -> None:
        """Refetch the full catalog without anyone waiting on it (coalesced per source).

        A no-op with caching off: nothing would keep the result.
        """
        if not self._closing and server.cache_ttl_seconds != NO_CACHE_TTL:
            self._start_catalog_fetch(server, source_key)

    def _catalog_task_done(
        self, key: tuple[int, str], task: "asyncio.Task[Dict[str, str]]"
    ) -> None:
        self._catalog_tasks.discard(task)
        current = self._catalog_inflight.get(key)
        if current is not None and current.task is task:
            self._catalog_inflight.pop(key, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            # A background refresh has no waiter to report to.
            logger.warning(
                "mcp: shared catalog fetch failed source_key=%s: %s",
                key[1],
                error,
            )

    async def _build_sources(self, server: McpServer, source_key: str) -> str:
        """Build the 'sources' bundle value from connection list metadata."""
        connections = await self._get_connections(server)
        entry = next(
            (c for c in connections
             if c.get("source_key") == source_key or c.get("name") == source_key),
            None,
        )
        if not entry:
            return "No source description."
        desc    = entry.get("description") or source_key
        db_type = entry.get("database_type", "")
        active  = entry.get("is_active", True)
        ctx     = entry.get("ai_domain_context")
        parts   = [f"{desc} | {db_type} | (Active: {active})"]
        if ctx:
            parts.append(ctx)
        return "\n".join(parts)

    async def _cached_bundle(
        self, server: McpServer, source_key: str, *, accept_invalidated: bool = False,
    ) -> Tuple[Optional[Dict[str, str]], str]:
        """The cached full bundle and how it may be used.

        ``hit``: fresh. ``stale``: past its TTL but not invalidated, so it may
        be served while it refreshes. ``invalidated``: someone asked for a
        refetch, so no copy is returned (unless ``accept_invalidated``: the
        provider-down fallback). ``miss``: never stored. ``off``: TTL 0.
        """
        ttl = server.cache_ttl_seconds
        if ttl == NO_CACHE_TTL:
            return None, "off"
        entry = await self._cache_svc.get(server.id, source_key, KEY_CATALOG, ttl)
        if entry is None or not isinstance(entry.payload, dict):
            return None, "miss"
        if entry.invalidated and not accept_invalidated:
            return None, "invalidated"
        # A new dict every time: the payload is the shared cached object.
        bundle = _empty_bundle()
        for key, default in bundle.items():
            value = entry.payload.get(key)
            bundle[key] = value if isinstance(value, str) else default
        bundle["columns"] = self._columns_view(server.id, source_key, bundle["columns"]).text
        return bundle, "stale" if entry.is_stale else "hit"

    def _columns_view(self, server_id: int, source_key: str, raw: str) -> _ColumnsView:
        """Normalised columns text and date index, reused while the payload is unchanged."""
        key = (server_id, source_key)
        view = self._columns_views.get(key)
        # Identity covers L1 hits; an L2 read decodes a new but equal string,
        # and comparing it is far cheaper than normalising and re-indexing.
        if view is not None and (raw is view.raw or raw is view.text or raw == view.raw or raw == view.text):
            return view
        text = normalize_columns_markdown(raw)
        view = _ColumnsView(raw=raw, text=text, dates=build_date_column_index(text))
        self._columns_views[key] = view
        return view

    # ── MCP protocol ─────────────────────────────────────────────────────────

    async def _list_tools(self, server: McpServer) -> List[Dict[str, Any]]:
        result = await self._jsonrpc(server, "tools/list", {})
        return result.get("tools", [])

    async def _call_tool(
        self,
        server: McpServer,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        result = await self._jsonrpc(
            server, "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        if result.get("isError"):
            raise McpError(f"Tool '{tool_name}' returned isError=true: {result}")
        content = result.get("content", [])
        if not content:
            return []
        first = content[0] if isinstance(content, list) else content
        if isinstance(first, dict):
            if first.get("type") == "text":
                text = first.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text     # plain text (markdown from get_catalog_prompt)
            if "data" in first:
                return first["data"]
        return first

    async def _jsonrpc(
        self,
        server: McpServer,
        method: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        POST a JSON-RPC 2.0 request and parse the SSE response.

        The server uses Streamable HTTP transport — responses arrive as:
            event: message
            data: {"result": {...}, "jsonrpc": "2.0", "id": 1}

        Both ``application/json`` and ``text/event-stream`` must appear in
        Accept, or the server returns 406 Not Acceptable.
        """
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept":       "application/json, text/event-stream",
        }
        if server.auth_type == "bearer" and server.bearer_token:
            headers["Authorization"] = f"Bearer {server.bearer_token}"

        payload = {"jsonrpc": _JSONRPC, "method": method, "params": params, "id": 1}

        response = await self._client().post(
            server.endpoint, json=payload, headers=headers
        )

        if response.status_code != 200:
            raise McpError(
                f"HTTP {response.status_code} from MCP server: {response.text[:300]}"
            )

        # Parse SSE stream: find the first ``data: {...}`` line.
        m = _SSE_DATA_RE.search(response.text)
        if m:
            envelope = json.loads(m.group(1))
            if "error" in envelope:
                raise McpError(f"JSON-RPC error from '{method}': {envelope['error']}")
            return envelope.get("result", {})

        # Fallback: plain JSON body (non-SSE server).
        try:
            data = response.json()
            if "error" in data:
                raise McpError(f"JSON-RPC error from '{method}': {data['error']}")
            return data.get("result", {})
        except (json.JSONDecodeError, ValueError):
            raise McpError(f"Unrecognised response from '{method}': {response.text[:200]}")


# ── Connection list normalisation ─────────────────────────────────────────────

def _normalise_connections(raw: Any) -> List[Dict[str, Any]]:
    """
    Convert the list_connections tool response to a standard list.

    Server returns:
        {"connections": [{"connection_id": 6, "name": "AdventureWorksDW",
                          "service_type": "Postgres", "description": "...",
                          "owner": null, "ai_domain_context": "..."}]}
    """
    if isinstance(raw, dict):
        items = raw.get("connections") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    result = []
    for c in items:
        if not isinstance(c, dict):
            continue
        result.append({
            "connection_id":     c.get("connection_id"),
            "source_key":        c.get("name", ""),
            "name":              c.get("name", ""),
            "display_name":      c.get("name", ""),
            "description":       c.get("description"),
            "database_type":     (c.get("service_type") or "").lower(),
            "ai_domain_context": c.get("ai_domain_context"),
            "is_active":         True,
        })
    return result


def _normalise_tool_descriptor(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the user-facing parts of a MCP tool descriptor.

    MCP SDKs commonly use camelCase schema keys; older Jeen UI code used
    snake_case in the persisted health blob. Return both through a stable
    snake_case API shape and preserve the raw descriptor for debugging.
    """
    name = tool.get("name", "")
    return {
        "name": name,
        "description": tool.get("description", "") or "",
        "need": _map_tool_to_need(name),
        "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
        "output_schema": tool.get("outputSchema") or tool.get("output_schema") or {},
        "annotations": tool.get("annotations") or {},
        "raw": tool,
    }


def _tool_descriptor_for_need(server: McpServer, need: str) -> Dict[str, Any]:
    """Return the stored health descriptor for a mapped MCP capability."""
    for tool in (server.health or {}).get("tools", []):
        if isinstance(tool, dict) and tool.get("need") == need:
            return tool
    return {}


def _normalise_schema_name(value: object) -> str:
    """Compare JSON-schema property names independent of casing/separators."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _value_search_arguments(
    descriptor: Dict[str, Any],
    *,
    connection_id: int,
    table: Optional[str],
    column: Optional[str],
    query: Optional[str],
    limit: Optional[int],
) -> Dict[str, Any]:
    """Map semantic value-search / profile inputs onto the tool's discovered schema.

    ``None`` inputs are omitted, which is how a reverse lookup asks the provider
    to search every column and how a profile call carries no query.
    """
    schema = descriptor.get("input_schema") or descriptor.get("inputSchema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    bounded_limit = max(1, min(int(limit), 100)) if limit is not None else None
    values: Dict[str, Any] = {
        "connection_id": connection_id,
        "table": table,
        "column": column,
        "query": query,
        "limit": bounded_limit,
    }
    if not isinstance(properties, dict) or not properties:
        # Old health rows may not have persisted the input schema. This is the
        # documented shape of the Jeen provider and is only a compatibility
        # fallback; new calls use the discovered names below.
        return {key: value for key, value in values.items() if value is not None}

    aliases = {
        "connection_id": (
            "connection_id", "connectionid", "source_id", "sourceid",
            "database_id", "databaseid",
        ),
        "table": ("table", "table_name", "tablename"),
        "column": ("column", "column_name", "columnname"),
        "query": ("query", "search", "search_term", "searchterm", "term", "value"),
        "limit": ("limit", "max_results", "maxresults", "page_size", "pagesize"),
    }
    normalized_properties = {
        _normalise_schema_name(name): name for name in properties
    }
    args: Dict[str, Any] = {}
    for semantic, names in aliases.items():
        if values[semantic] is None:
            continue
        target = next(
            (
                normalized_properties.get(_normalise_schema_name(name))
                for name in names
                if _normalise_schema_name(name) in normalized_properties
            ),
            None,
        )
        if target:
            args[target] = values[semantic]
    return args


def _normalise_value_search(raw: Any) -> Dict[str, Any]:
    """Normalize common MCP value-search payloads without inventing completeness."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {"values": [raw]}

    payload = raw if isinstance(raw, dict) else {}
    if isinstance(raw, list):
        items: Any = raw
    else:
        items = next(
            (
                payload.get(key)
                for key in ("values", "matches", "items", "results", "data")
                if isinstance(payload.get(key), list)
            ),
            [],
        )

    values: List[str] = []
    matches: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            value = next(
                (
                    item.get(key)
                    for key in ("canonical_value", "canonical", "value", "name", "label", "text")
                    if item.get(key) is not None
                ),
                None,
            )
        else:
            value = item
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if isinstance(item, dict):
            # Per-match provenance for reverse lookups: which column held it.
            column = item.get("column") or item.get("column_name")
            table = item.get("table") or item.get("table_name")
            score = item.get("score") or item.get("similarity") or item.get("distance")
            try:
                score_val = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_val = None
            matches.append({
                "value": text,
                "table": str(table) if table else None,
                "column": str(column) if column else None,
                "score": score_val,
                "count": item.get("count") or item.get("value_count"),
                "semantic_type": item.get("semantic_type") or item.get("semanticType"),
            })
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            values.append(text)

    complete = False
    if payload:
        if isinstance(payload.get("complete"), bool):
            complete = payload["complete"]
        elif isinstance(payload.get("is_complete"), bool):
            complete = payload["is_complete"]
        elif isinstance(payload.get("exhaustive"), bool):
            complete = payload["exhaustive"]
        elif isinstance(payload.get("truncated"), bool):
            complete = not payload["truncated"]
        elif isinstance(payload.get("has_more"), bool):
            complete = not payload["has_more"]
    snapshot = payload.get("snapshot") or payload.get("profiled_at") or payload.get("captured_at") if payload else None
    return {"values": values, "matches": matches, "complete": complete, "source": "mcp",
            "snapshot": str(snapshot) if snapshot else None}


def _empty_value_search() -> Dict[str, Any]:
    return {"values": [], "matches": [], "complete": False, "source": "mcp", "snapshot": None}


# ── Catalog markdown parser ───────────────────────────────────────────────────

# Maps ``## Header`` text → MetadataLoader bundle key.
_SECTION_MAP: Dict[str, str] = {
    "Tables":                       KEY_TABLES,
    "Columns":                      KEY_COLUMNS,
    "Column Statistics":            KEY_COLUMN_STATISTICS,
    "Column Stats":                 KEY_COLUMN_STATISTICS,
    "Statistics":                   KEY_COLUMN_STATISTICS,
    "Sample Values":                KEY_COLUMN_SAMPLES,
    "Column Samples":               KEY_COLUMN_SAMPLES,
    "Samples":                      KEY_COLUMN_SAMPLES,
    "Relationships (Foreign Keys)": KEY_RELATIONSHIPS,
    "Relationships":                KEY_RELATIONSHIPS,
    "Business Terms":               KEY_BUSINESS_TERMS,
    "Knowledge Pairs":              KEY_KNOWLEDGE_PAIRS,
    "Source":                       "sources",
    "Domain Context":               "sources",
}


# ── Column-section normalisation ──────────────────────────────────────────────
#
# Everything downstream of the catalog (the ML planner's catalog_candidates, the
# filter grounder's column_types, the SQL node's column allowlist) parses one
# flat line per column in the shape MetadataLoader emits from the metadata DB:
#
#     table.column - Type: data_type[, Description: …][, PK: true][, NOT NULL]
#
# The schema-modeler MCP groups columns under a table header instead:
#
#     "public"."dimdate": Contains date dimension data.
#       - "datekey" (integer) [PK] [distinct_count=2191, …]: description
#
# Passed through verbatim, that reads as table "public" / column "dimdate" with
# no type at all — so no date or numeric column is ever found and every ML
# skill falls back to SQL. The adapter is the one place that knows both shapes,
# so it rewrites the grouped form into the flat one here. Idempotent: flat
# lines pass through untouched.

_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[\w$]+)'
_GROUPED_TABLE_RE = re.compile(rf"^\s*(?P<table>{_IDENT}(?:\.{_IDENT})*)\s*:\s*(?P<desc>.*)$")
_GROUPED_COLUMN_RE = re.compile(
    rf"^\s*[-*]\s*(?P<col>{_IDENT})\s*\((?P<type>[^()]*(?:\([^()]*\)[^()]*)*)\)\s*(?P<rest>.*)$"
)
_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_FLAT_COLUMN_RE = re.compile(r"^\s*-?\s*\S+\s+-\s+Type\s*:", re.IGNORECASE)


def _grouped_column_line(table: str, match: "re.Match[str]") -> str:
    """One flat line for a grouped column entry."""
    column = match.group("col")
    dtype = " ".join(match.group("type").split())
    rest = match.group("rest") or ""
    flags = [flag.strip() for flag in _BRACKET_RE.findall(rest)]
    # The description follows the last bracket group (or the whole tail), after a colon.
    tail = _BRACKET_RE.sub("", rest).strip()
    description = tail[1:].strip() if tail.startswith(":") else tail.strip()
    parts = [f"{table}.{column} - Type: {dtype}"]
    if any(flag.upper() == "PK" for flag in flags):
        parts.append("PK: true")
    if any(flag.upper() in ("NOT NULL", "NN") for flag in flags):
        parts.append("NOT NULL")
    for flag in flags:
        distinct = re.match(r"distinct_count\s*=\s*(\d+)", flag)
        if distinct:
            parts.append(f"Distinct: {distinct.group(1)}")
    if description:
        parts.append(f"Description: {description}")
    return "- " + ", ".join(parts)


_LINE_TYPE_RE = re.compile(r"\btype\s*:\s*([^,|]+)", re.IGNORECASE)
_DATE_TYPE_TOKENS = ("date", "timestamp", "datetime")


def _flat_line_parts(line: str) -> "tuple[str, str, str]":
    """``(table, column, dtype)`` of one flat column line, lower-cased; empty when it is not one."""
    from .identifiers import table_column_from_identifier  # noqa: PLC0415  (leaf module)
    stripped = line.lstrip("- ").strip()
    table, column = table_column_from_identifier(stripped.split(" - ", 1)[0].strip())
    match = _LINE_TYPE_RE.search(stripped)
    return table, column, (match.group(1).strip().lower() if match else "")


def _is_date_type(dtype: str) -> bool:
    return dtype != "time" and any(token in dtype for token in _DATE_TYPE_TOKENS)


def build_date_column_index(full_columns: str) -> DateColumnIndex:
    """Each table's native date/timestamp columns, from the full columns section."""
    index: DateColumnIndex = {}
    for line in (full_columns or "").splitlines():
        table, column, dtype = _flat_line_parts(line)
        if table and _is_date_type(dtype):
            index.setdefault(table, []).append(
                (column, line.strip() if line.lstrip().startswith("-") else f"- {line.strip()}")
            )
    return index


def restore_date_columns(filtered_columns: str, full_columns: str) -> str:
    """Give each table in a question-filtered bundle its native date columns back.

    The schema-modeler MCP's question filter keeps a fact table's ``*datekey``
    surrogates but drops its ``date``/``timestamp`` column when a question only
    implies time ("the most recent quarter…"). Every time-series and
    contribution skill needs that column on the table itself, so the (cached)
    full catalog supplies it. Only date columns are added, so the filter's
    choice of measures and dimensions is untouched, and nothing changes once
    the modeler keeps them itself.
    """
    return restore_date_columns_from_index(filtered_columns, build_date_column_index(full_columns))


def restore_date_columns_from_index(filtered_columns: str, date_index: DateColumnIndex) -> str:
    """``restore_date_columns`` from a prebuilt index, so a large full catalog is scanned once."""
    if not (filtered_columns or "").strip() or not date_index:
        return filtered_columns
    tables: set[str] = set()
    present: set[tuple[str, str]] = set()
    has_date: set[str] = set()
    for line in filtered_columns.splitlines():
        table, column, dtype = _flat_line_parts(line)
        if not table:
            continue
        tables.add(table)
        present.add((table, column))
        if _is_date_type(dtype):
            has_date.add(table)
    additions = [
        line
        for table, columns in date_index.items()
        if table in tables and table not in has_date
        for column, line in columns
        if (table, column) not in present
    ]
    if not additions:
        return filtered_columns
    return filtered_columns.rstrip("\n") + "\n" + "\n".join(additions)


def normalize_columns_markdown(text: str) -> str:
    """Rewrite a grouped-by-table columns section into flat typed lines.

    Lines already in the flat ``table.column - Type: …`` shape are kept as they
    are, so applying this to cached (already normalised) content is a no-op.
    Prose that is neither a table header nor a column entry (an intro line) is
    dropped, since every consumer treats each line as one column.
    """
    if not text:
        return text
    lines = text.splitlines()
    if not any(_GROUPED_COLUMN_RE.match(line) for line in lines):
        return text  # nothing grouped here (flat DB shape, or empty)
    out: List[str] = []
    table: Optional[str] = None
    for line in lines:
        if not line.strip():
            continue
        if _FLAT_COLUMN_RE.match(line):
            out.append(line)
            continue
        column = _GROUPED_COLUMN_RE.match(line)
        if column and table:
            out.append(_grouped_column_line(table, column))
            continue
        header = _GROUPED_TABLE_RE.match(line)
        if header and not line.lstrip().startswith(("-", "*")):
            table = header.group("table").strip()
            continue
        # Anything else (an intro sentence, a stray note) has no column in it.
    return "\n".join(out)


def _parse_catalog_markdown(text: str) -> Dict[str, str]:
    """
    Split the pre-formatted catalog prompt into MetadataLoader bundle keys.

    Section headers are ``## Header`` lines.  Content runs until the next
    ``##`` header.  Missing sections default to empty string.
    """
    bundle: Dict[str, str] = {
        "tables":          "",
        "columns":         "",
        "relationships":   "",
        "sources":         "",
        "knowledge_pairs": "",
        "business_terms":  "",
        "column_statistics": "",
        "column_samples": "",
    }

    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    sources_parts: List[str] = []

    for part in parts:
        lines   = part.split("\n")
        header  = lines[0].lstrip("# ").strip() if lines else ""
        content = "\n".join(lines[1:]).strip()
        key     = _SECTION_MAP.get(header)

        if key == "sources":
            if content:
                sources_parts.append(content)
        elif key and key in bundle:
            bundle[key] = content

    if sources_parts:
        bundle["sources"] = "\n\n".join(sources_parts)

    bundle["columns"] = normalize_columns_markdown(bundle["columns"])
    return bundle


def _extract_text(raw: Any) -> str:
    """Extract a plain string from a _call_tool return value."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw)
    return ""


# ── Need-mapping heuristics ───────────────────────────────────────────────────

# Order matters: _map_tool_to_need returns the FIRST need whose keyword is a
# substring of the tool name. The specific autocomplete needs are listed
# before the generic ones so e.g. ``list_tables_rich`` maps to
# NEED_TABLES_RICH rather than NEED_LIST_TABLES (which also matches "tables").
_NEED_KEYWORDS: Dict[str, List[str]] = {
    NEED_LIST_SOURCES:        ["list_connections",   "connections",    "list_sources"],
    NEED_TABLES_RICH:         ["tables_rich",        "list_tables_rich"],
    # Profiling tools first: "column_profile" contains neither "columns" nor
    # "tables", but "table_profile"/"source_statistics" must not fall through to
    # the generic catalog needs below.
    NEED_COLUMN_PROFILE:      ["get_column_profile", "column_profile"],
    NEED_TABLE_PROFILE:       ["get_table_profile", "table_profile"],
    NEED_SOURCE_STATISTICS:   ["get_source_statistics", "source_statistics", "source_stats"],
    # ``get_columns`` returns JSON rows per column (with inlined statistics),
    # i.e. the structured columns dataset, not the filtered prompt.
    NEED_LIST_COLUMNS:        ["list_columns", "get_columns"],
    NEED_KNOWLEDGE_QUESTIONS: ["knowledge_questions", "list_knowledge_questions"],
    NEED_SEARCH_COLUMN_VALUES: ["search_column_values", "search_values", "column_values"],
    NEED_LIST_TABLES:         ["get_catalog_prompt", "catalog_prompt", "list_tables",    "tables"],
    NEED_DESCRIBE_TABLE:      ["get_filtered_prompt","filtered_prompt","describe_table", "describe", "columns"],
    NEED_LIST_RELATIONSHIPS:  ["relationships",      "list_relations"],
    NEED_BUSINESS_GLOSSARY:   ["business_terms",     "glossary",       "terms"],
    NEED_KNOWLEDGE_PAIRS:     ["knowledge_pairs",    "knowledge",      "examples"],
}


def _map_tool_to_need(tool_name: str) -> Optional[str]:
    """Return the catalog need key for *tool_name*, or None if unmapped."""
    lower = tool_name.lower()
    for need, keywords in _NEED_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return need
    return None


# ── Legacy helpers kept for tests ────────────────────────────────────────────
# These format functions are used in unit tests for the formatters.

def _fmt_tables(rows: List[Dict]) -> str:
    lines = []
    for r in rows:
        name = r.get("table_name") or r.get("name") or ""
        desc = r.get("table_description") or r.get("description") or ""
        line = f"{name} - {desc}" if desc else name
        if line:
            lines.append(f"- {line}")
    return "\n".join(lines) if lines else "No tables registered."


def _fmt_columns(rows: List[Dict]) -> str:
    lines = []
    for r in rows:
        table    = r.get("table_name") or r.get("table") or ""
        col      = r.get("column_name") or r.get("column") or ""
        dtype    = r.get("data_type") or r.get("type") or ""
        desc     = r.get("description") or ""
        is_pk    = bool(r.get("is_primary_key") or r.get("is_pk"))
        not_null = not bool(r.get("is_nullable", True))
        parts    = [f"{table}.{col}", f"Type: {dtype}"]
        if desc:
            parts.append(f"Description: {desc}")
        if is_pk:
            parts.append("PK: true")
        if not_null:
            parts.append("NOT NULL")
        lines.append(f"- {', '.join(parts)}")
    return "\n".join(lines) if lines else "No columns registered."


def _fmt_relationships(rows: List[Dict]) -> str:
    rels = []
    for r in rows:
        relation = r.get("relation") or r.get("relationship") or str(r)
        if relation:
            rels.append(relation)
    if not rels:
        return "No relationships registered."
    body = ", ".join(f"('{r}',)" for r in rels)
    return f"[{body}]"


def _fmt_knowledge_pairs(rows: List[Dict]) -> str:
    lines = []
    for r in rows:
        cat      = r.get("category") or "General"
        question = r.get("question") or "No question"
        sql      = r.get("sql_statement") or r.get("sql") or "No statement"
        tags     = r.get("tags") or "No tags"
        lines.append(f"- Category: {cat} | Question: {question} | SQL: {sql} | Tags: {tags}")
    return "\n".join(lines) if lines else "No knowledge pairs registered."


def _fmt_business_terms(rows: List[Dict]) -> str:
    lines = []
    for r in rows:
        term = r.get("term") or ""
        defn = r.get("definition") or "No definition provided"
        cat  = r.get("category") or "General"
        lines.append(f"- Term: {term} | Definition: {defn} | Category: {cat}")
    return "\n".join(lines) if lines else "No business terms registered."


def _bare_table_name(name: Any) -> str:
    """``"public"."dimcustomer"`` / ``public.dimcustomer`` / ``DimCustomer`` → ``dimcustomer``."""
    text = str(name or "").replace('"', "").replace("`", "").strip().lower()
    return text.rsplit(".", 1)[-1]


def _flatten_columns(items: List[Any], table_name: Optional[str]) -> List[Dict[str, Any]]:
    """Give ``load_columns`` the flat ``[{table, column, data_type, …}]`` shape
    the UI reads, whatever the server sent.

    The schema-modeler ``list_columns`` tool answers with one envelope —
    ``{columns: [{table_name, column_name, data_type, …}], count, tables}`` —
    and ignores its ``table`` argument, so the envelope is unwrapped, the
    field names are mapped and the scope is applied here. Servers that already
    return flat records pass through unchanged.
    """
    flat: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        nested = item.get("columns")
        if isinstance(nested, list):
            parent = item.get("table_name") or item.get("table")
            for col in nested:
                if isinstance(col, dict):
                    flat.append(_column_record(col, parent))
        else:
            flat.append(_column_record(item, None))
    if table_name:
        want = _bare_table_name(table_name)
        if want and any(rec.get("table") for rec in flat):
            flat = [rec for rec in flat if _bare_table_name(rec.get("table")) == want]
    return flat


def _column_record(col: Dict[str, Any], parent_table: Any) -> Dict[str, Any]:
    rec = dict(col)
    if not rec.get("column"):
        rec["column"] = col.get("column_name") or col.get("name") or ""
    if not rec.get("table"):
        rec["table"] = col.get("table_name") or parent_table or ""
    return rec


def _normalise_list(raw: Any) -> List[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("items", "data", "rows", "results"):
            if isinstance(raw.get(key), list):
                return raw[key]
        return [raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            pass
    return []


def _empty_bundle() -> Dict[str, str]:
    return {
        "tables":          "No tables registered.",
        "columns":         "No columns registered.",
        "relationships":   "No relationships registered.",
        "sources":         "No source description.",
        "knowledge_pairs": "No knowledge pairs registered.",
        "business_terms":  "No business terms registered.",
        "column_statistics": "",
        "column_samples": "",
    }
