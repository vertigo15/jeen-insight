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
from typing import Any, Dict, List, Optional

import httpx

from .mcp_server_service import (
    McpServer, McpServerService,
    NEED_LIST_SOURCES, NEED_LIST_TABLES, NEED_DESCRIBE_TABLE,
    NEED_LIST_RELATIONSHIPS, NEED_BUSINESS_GLOSSARY, NEED_KNOWLEDGE_PAIRS,
    NEED_TABLES_RICH, NEED_LIST_COLUMNS, NEED_KNOWLEDGE_QUESTIONS,
)
from .mcp_cache_service import (
    McpCacheService,
    KEY_CONNECTIONS,
    KEY_TABLES,
    KEY_COLUMNS,
    KEY_RELATIONSHIPS,
    KEY_BUSINESS_TERMS,
    KEY_KNOWLEDGE_PAIRS,
    KEY_TABLES_RICH,
    KEY_KNOWLEDGE_QUESTIONS,
    KEY_COLUMNS_STRUCT,
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

    # ── Public catalog API ────────────────────────────────────────────────────

    async def load_connections(self) -> List[Dict[str, Any]]:
        """
        Return the connection list from MCP.

        Shape: [{"source_key": str, "connection_id": int, "display_name": str,
                 "description": str|None, "database_type": str, "is_active": bool,
                 "ai_domain_context": str|None}]

        Returns [] on any error so the caller can fall back to settings_services.
        """
        server = await self._srv_svc.get_active()
        if not server:
            return []
        return await self._get_connections(server)

    async def load_all(self, source_key: str) -> Dict[str, str]:
        """
        Return a MetadataLoader-compatible bundle for source_key.
        Keys: tables, columns, relationships, sources, knowledge_pairs, business_terms.

        Calls get_catalog_prompt once → parses markdown → caches all 6 sections.
        Returns empty fallbacks on errors so the prompt degrades gracefully.
        """
        server = await self._srv_svc.get_active()
        if not server:
            return _empty_bundle()

        # Try full bundle from cache first.
        cached = await self._bundle_from_cache(server, source_key)
        if cached is not None:
            return cached

        # Cache miss — fetch from MCP and populate all sections.
        try:
            await self._ensure_catalog(server, source_key)
        except Exception as exc:
            logger.error("mcp: load_all failed for %s: %s", source_key, exc)
            return _empty_bundle()

        # Second attempt from cache (now populated).
        result = await self._bundle_from_cache(server, source_key)
        return result if result is not None else _empty_bundle()

    async def load_filtered(
        self, source_key: str, question: str
    ) -> Dict[str, str]:
        """Return a question-focused catalog bundle from ``get_filtered_prompt``.

        Filtered prompts are request-specific, so they deliberately bypass the
        shared full-catalog cache. Callers should fall back to ``load_all`` when
        this optional MCP capability is unavailable or fails.
        """
        server = await self._srv_svc.get_active()
        if not server:
            raise McpError("No active MCP server")

        tool = server.get_tool_for_need(NEED_DESCRIBE_TABLE)
        if not tool:
            raise McpError("No filtered prompt tool mapped (need: describe_table)")

        conn_id = await self._resolve_connection_id(server, source_key)
        if conn_id is None:
            raise McpError(f"No connection found for source_key={source_key!r}")

        raw = await self._call_tool(
            server,
            tool,
            {"connection_id": conn_id, "question": question},
        )
        text = _extract_text(raw)
        if not text:
            raise McpError(
                f"Empty response from {tool} for connection_id={conn_id}"
            )

        bundle = _parse_catalog_markdown(text)
        if not bundle.get("sources"):
            bundle["sources"] = await self._build_sources(server, source_key)
        logger.info(
            "mcp: filtered catalog loaded source_key=%s connection_id=%d (%d chars)",
            source_key,
            conn_id,
            len(text),
        )
        return bundle

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
        try:
            raw = await self._call_tool(server, tool, args)
            items = _normalise_list(raw)
            await self._cache_svc.set(server.id, source_key, cache_key, items, ttl)
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
        return filter_columns(items)

    async def get_cache_status(
        self, mcp_server_id: int, source_key: str
    ) -> Dict[str, Any]:
        return await self._cache_svc.get_status(mcp_server_id, source_key)

    async def invalidate(
        self, mcp_server_id: int, source_key: Optional[str] = None
    ) -> None:
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

        try:
            raw   = await self._call_tool(server, tool, {})
            items = _normalise_connections(raw)
            await self._cache_svc.set(
                server.id, SOURCE_GLOBAL, KEY_CONNECTIONS, items, server.cache_ttl_seconds
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

    async def _ensure_catalog(self, server: McpServer, source_key: str) -> None:
        """
        Call get_catalog_prompt for source_key, parse the markdown response,
        and store all 6 bundle sections in cache atomically.

        After this call, _bundle_from_cache() will find all keys populated.
        """
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
        ttl      = server.cache_ttl_seconds

        # Store catalog sections atomically.
        await asyncio.gather(
            self._cache_svc.set(server.id, source_key, KEY_TABLES,          sections["tables"],          ttl),
            self._cache_svc.set(server.id, source_key, KEY_COLUMNS,         sections["columns"],         ttl),
            self._cache_svc.set(server.id, source_key, KEY_RELATIONSHIPS,   sections["relationships"],   ttl),
            self._cache_svc.set(server.id, source_key, KEY_BUSINESS_TERMS,  sections["business_terms"],  ttl),
            self._cache_svc.set(server.id, source_key, KEY_KNOWLEDGE_PAIRS, sections["knowledge_pairs"], ttl),
        )

        # sources — prefer the ## Source section from the prompt; fall back to
        # connection list metadata.
        sources_text = sections.get("sources") or await self._build_sources(server, source_key)
        # Store sources keyed by (source_key, KEY_CONNECTIONS) — distinct from
        # the global connection list which lives at (SOURCE_GLOBAL, KEY_CONNECTIONS).
        await self._cache_svc.set(server.id, source_key, KEY_CONNECTIONS, sources_text, ttl)

        logger.info(
            "mcp: catalog cached source_key=%s connection_id=%d (%d chars)",
            source_key, conn_id, len(text),
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

    async def _bundle_from_cache(
        self, server: McpServer, source_key: str
    ) -> Optional[Dict[str, str]]:
        """
        Build a full bundle from cache.
        Returns None on any miss or stale entry (triggers _ensure_catalog).
        """
        spec = [
            (KEY_TABLES,          "No tables registered."),
            (KEY_COLUMNS,         "No columns registered."),
            (KEY_RELATIONSHIPS,   "No relationships registered."),
            (KEY_BUSINESS_TERMS,  "No business terms registered."),
            (KEY_KNOWLEDGE_PAIRS, "No knowledge pairs registered."),
        ]
        bundle: Dict[str, str] = {}
        ttl = server.cache_ttl_seconds

        for cache_key, empty in spec:
            entry = await self._cache_svc.get(server.id, source_key, cache_key, ttl)
            if entry is None or entry.is_stale:
                return None
            bundle[cache_key] = entry.payload if isinstance(entry.payload, str) else empty

        # sources stored under (source_key, KEY_CONNECTIONS)
        src_entry = await self._cache_svc.get(server.id, source_key, KEY_CONNECTIONS, ttl)
        if src_entry and not src_entry.is_stale and isinstance(src_entry.payload, str):
            bundle["sources"] = src_entry.payload
        else:
            bundle["sources"] = "No source description."

        return {
            "tables":          bundle[KEY_TABLES],
            "columns":         bundle[KEY_COLUMNS],
            "relationships":   bundle[KEY_RELATIONSHIPS],
            "sources":         bundle["sources"],
            "knowledge_pairs": bundle[KEY_KNOWLEDGE_PAIRS],
            "business_terms":  bundle[KEY_BUSINESS_TERMS],
        }

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

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(server.endpoint, json=payload, headers=headers)

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


# ── Catalog markdown parser ───────────────────────────────────────────────────

# Maps ``## Header`` text → MetadataLoader bundle key.
_SECTION_MAP: Dict[str, str] = {
    "Tables":                       KEY_TABLES,
    "Columns":                      KEY_COLUMNS,
    "Relationships (Foreign Keys)": KEY_RELATIONSHIPS,
    "Relationships":                KEY_RELATIONSHIPS,
    "Business Terms":               KEY_BUSINESS_TERMS,
    "Knowledge Pairs":              KEY_KNOWLEDGE_PAIRS,
    "Source":                       "sources",
    "Domain Context":               "sources",
}


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
    NEED_LIST_COLUMNS:        ["list_columns"],
    NEED_KNOWLEDGE_QUESTIONS: ["knowledge_questions", "list_knowledge_questions"],
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
    }
