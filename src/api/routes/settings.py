"""Settings API routes.

Provides CRUD for prompt templates and read-only app info.

Prompt storage strategy
-----------------------
Default prompts live as ``.md`` / ``.txt`` files in ``src/agent/prompts/``
and ``templates/``.  When the user saves a custom version:

1. The original file is backed up to ``src/agent/prompts/.defaults/{name}.{ext}``
   (only on the very first save, so the default is never overwritten again).
2. The custom text is written directly to the main prompt file so the
   running PromptLoader picks it up on the next ``/api/settings/prompts/reload``
   call (or container restart).

Reset restores the backed-up original and removes the backup.
"""

from __future__ import annotations

import asyncio
import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# ── Prompt file registry ──────────────────────────────────────────────────────────────────

_BASE = Path(__file__).resolve().parent.parent.parent  # src/
_PROMPTS_DIR = _BASE / "agent" / "prompts"
_INSIGHTS_PROMPT = _BASE.parent / "templates" / "insight_prompt.txt"  # templates/

# Ordered list: defines nav order in the UI.
# Each entry: (name, display_label, group, description, file_path)
PROMPT_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "jeen_insights_system",
        "label": "System Prompt",
        "group": "AI Agent",
        "description": (
            "Main system prompt injected as the first message on every query. "
            "Defines the agent's persona, rules, SQL behaviour, and how dynamic "
            "metadata (tables, columns, knowledge pairs…) is presented to the LLM."
        ),
        "path": _PROMPTS_DIR / "jeen_insights_system.md",
    },
    {
        "name": "fused_router",
        "label": "Router",
        "group": "AI Agent",
        "description": (
            "Classifies each user message into a route: sql_query, memory_answer, "
            "greeting, or out_of_scope. Runs before the SQL generator and "
            "uses a single fast LLM call."
        ),
        "path": _PROMPTS_DIR / "fused_router.md",
    },
    {
        "name": "fused_eval_analytics",
        "label": "Eval & Analytics",
        "group": "AI Agent",
        "description": (
            "Evaluates whether the SQL result actually answers the user's intent. "
            "Also produces a one-line summary, key insights, and optional "
            "follow-up suggestions shown in the results card."
        ),
        "path": _PROMPTS_DIR / "fused_eval_analytics.md",
    },
    {
        "name": "memory_answer",
        "label": "Memory Answer",
        "group": "AI Agent",
        "description": (
            "Answers the user directly from conversation history when no "
            "live database query is needed (e.g. follow-ups about previous results). "
            "If a new query is required it signals the graph to fall through to SQL."
        ),
        "path": _PROMPTS_DIR / "memory_answer.md",
    },
    {
        "name": "memory_summarizer",
        "label": "Memory Summary",
        "group": "AI Agent",
        "description": (
            "Condenses the conversation history into a short paragraph when the "
            "token budget is exceeded. The summary is re-injected into the router "
            "prompt as {conversation_summary}."
        ),
        "path": _PROMPTS_DIR / "memory_summarizer.md",
    },
    {
        "name": "sql_generator",
        "label": "SQL Retry",
        "group": "AI Agent",
        "description": (
            "User message injected only on retry attempts (retry_count > 0). "
            "Feeds the structured error context back to the LLM so it can "
            "generate a corrected SQL query. On the first attempt the raw "
            "user question is used directly — this prompt is not called."
        ),
        "path": _PROMPTS_DIR / "sql_generator.md",
    },
    {
        "name": "chart_editor",
        "label": "Chart Editor",
        "group": "Other Features",
        "description": (
            "Receives the user's natural-language chart edit instruction and the "
            "current ECharts config. Returns a new config plus any derived-series "
            "overlays (moving average, trend line…) that the client computes locally."
        ),
        "path": _PROMPTS_DIR / "chart_editor.md",
    },
    {
        "name": "insights",
        "label": "Insights",
        "group": "Other Features",
        "description": (
            "Analyzes query results and returns a JSON payload with a summary, "
            "key findings backed by specific numbers, and 4–6 follow-up questions "
            "rendered as clickable chips below the results."
        ),
        "path": _INSIGHTS_PROMPT,
    },
    {
        "name": "autocomplete_suggestions",
        "label": "Autocomplete",
        "group": "Other Features",
        "description": (
            "Generates 3–4 contextual question completions as the user types. "
            "Detects typos against available table names and returns corrections "
            "alongside the suggestions. Runs only when no local cache hit exists."
        ),
        "path": _PROMPTS_DIR / "autocomplete_suggestions.md",
    },
]

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_ESCAPED_RE = re.compile(r"\{\{[^}]*\}\}")  # {{ }} — literal braces in output


def _extract_placeholders(text: str) -> List[str]:
    """Return unique {placeholder} names, ignoring {{ escaped }} literals."""
    cleaned = _ESCAPED_RE.sub("", text)
    return sorted(set(_PLACEHOLDER_RE.findall(cleaned)))


def _entry_for(name: str) -> Optional[Dict[str, Any]]:
    return next((e for e in PROMPT_REGISTRY if e["name"] == name), None)


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# ── Response models ──────────────────────────────────────────────────────────────────

class PromptMeta(BaseModel):
    name: str
    label: str
    group: str
    description: str
    is_custom: bool
    placeholders: List[str]
    version: int = 1
    model_id: Optional[int] = None
    model_name: Optional[str] = None


class PromptDetail(PromptMeta):
    content: str


class PromptUpdate(BaseModel):
    content: str


class SetPromptModelRequest(BaseModel):
    model_name: Optional[str] = None  # None = clear override, use global default


class _SafeFormatDict(dict):
    """Leave unknown placeholders visible instead of failing prompt preview."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# ── DB helpers for prompts ──────────────────────────────────────────────────────────────

_LIST_PROMPTS_SQL = """
    SELECT ip.prompt_place, ip.version, ip.is_custom, ip.model_id,
           am.name AS model_name, ip.content
    FROM insights_prompts ip
    LEFT JOIN admin_models am ON am.id = ip.model_id
    WHERE ip.is_active = true
"""


async def _db_list_prompts() -> Dict[str, Any]:
    """Return a dict keyed by prompt_place with DB row data."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIST_PROMPTS_SQL)
    return {r["prompt_place"]: dict(r) for r in rows}


async def _db_get_prompt(place: str) -> Optional[Dict[str, Any]]:
    """Return the active row for *place*, or None."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT ip.prompt_place, ip.version, ip.is_custom, ip.model_id,
                   am.name AS model_name, ip.content
            FROM insights_prompts ip
            LEFT JOIN admin_models am ON am.id = ip.model_id
            WHERE ip.prompt_place = $1 AND ip.is_active = true
            LIMIT 1
            """,
            place,
        )
    return dict(row) if row else None


async def _db_save_prompt(place: str, content: str) -> int:
    """Deactivate current active row, insert new version (preserving model_id).
    Returns the new version number.
    """
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT version, model_id FROM insights_prompts "
                "WHERE prompt_place = $1 AND is_active = true",
                place,
            )
            if not current:
                raise HTTPException(404, f"No active prompt row for '{place}'. Restart to re-seed.")
            new_version = current["version"] + 1
            model_id = current["model_id"]
            await conn.execute(
                "UPDATE insights_prompts SET is_active = false, updated_at = NOW() "
                "WHERE prompt_place = $1 AND is_active = true",
                place,
            )
            await conn.execute(
                """
                INSERT INTO insights_prompts
                    (prompt_place, content, version, is_active, is_custom, model_id)
                VALUES ($1, $2, $3, true, true, $4)
                """,
                place, content, new_version, model_id,
            )
    return new_version


async def _db_reset_prompt(place: str, default_content: str) -> int:
    """Deactivate current row, insert file default (is_custom=false, model_id=NULL)."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT version, is_custom FROM insights_prompts "
                "WHERE prompt_place = $1 AND is_active = true",
                place,
            )
            if not current:
                raise HTTPException(404, f"No active prompt row for '{place}'. Restart to re-seed.")
            if not current["is_custom"]:
                return current["version"]  # already at default
            new_version = current["version"] + 1
            await conn.execute(
                "UPDATE insights_prompts SET is_active = false, updated_at = NOW() "
                "WHERE prompt_place = $1 AND is_active = true",
                place,
            )
            await conn.execute(
                """
                INSERT INTO insights_prompts
                    (prompt_place, content, version, is_active, is_custom, model_id)
                VALUES ($1, $2, $3, true, false, NULL)
                """,
                place, default_content, new_version,
            )
    return new_version


def _invalidate_cache(place: str) -> None:
    """Best-effort: drop the cache entry so the next request reloads from DB."""
    try:
        from src.api import state as app_state
        if app_state.prompt_cache:
            app_state.prompt_cache.invalidate(place)
    except Exception as exc:  # noqa: BLE001
        logger.debug("settings: cache invalidation skipped: %s", exc)


def _estimate_tokens(text: Any) -> int:
    """Token count for prompt previews; tiktoken when installed, rough fallback."""
    raw = "" if text is None else str(text)
    if not raw:
        return 0
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(raw))
    except Exception:
        # Same conservative rule used elsewhere in the graph when tokenizer
        # libraries are unavailable.
        return max(1, (len(raw) + 3) // 4)


def _tokenizer_name() -> str:
    try:
        import tiktoken  # noqa: F401
        return "tiktoken:cl100k_base"
    except Exception:
        return "estimate:chars/4"


def _extract_table_names(tables_text: str, limit: int = 200) -> List[str]:
    names: List[str] = []
    for line in (tables_text or "").splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if not stripped:
            continue
        name = re.split(r"\s[-—]\s|\s\|\s|,", stripped, maxsplit=1)[0].strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


async def _catalog_source() -> str:
    from src.api import state as app_state

    if app_state.mcp_server_service:
        try:
            return await app_state.mcp_server_service.get_catalog_source()
        except Exception:
            pass
    return "db"


async def _list_prompt_context_connections() -> Dict[str, Any]:
    """Return connection choices from the active catalog source."""
    from src.api import state as app_state

    source = await _catalog_source()
    connections: List[Dict[str, Any]] = []

    if source == "mcp" and app_state.mcp_catalog_client:
        try:
            raw = await app_state.mcp_catalog_client.load_connections()
            connections = [
                {
                    "source_key": c.get("source_key") or c.get("name"),
                    "display_name": c.get("display_name") or c.get("name") or c.get("source_key"),
                    "database_type": c.get("database_type") or "",
                    "description": c.get("description"),
                    "catalog_source": "mcp",
                }
                for c in raw
                if c.get("source_key") or c.get("name")
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings: MCP prompt contexts failed: %s", exc)

    if not connections and app_state.connection_service:
        try:
            db_connections = await app_state.connection_service.list_connections()
            connections = [
                {
                    **c.to_public_dict(),
                    "catalog_source": "db",
                }
                for c in db_connections
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings: DB prompt contexts failed: %s", exc)

    return {"catalog_source": source, "connections": connections}


async def _connection_info(source_key: str) -> Dict[str, Any]:
    """Best-effort display metadata for a selected source_key."""
    from src.api import state as app_state

    if app_state.connection_service:
        try:
            c = await app_state.connection_service.get_connection(source_key)
            return c.to_public_dict()
        except Exception:
            pass

    if app_state.mcp_catalog_client:
        try:
            for c in await app_state.mcp_catalog_client.load_connections():
                if c.get("source_key") == source_key or c.get("name") == source_key:
                    return {
                        "source_key": source_key,
                        "display_name": c.get("display_name") or c.get("name") or source_key,
                        "description": c.get("description"),
                        "database_type": c.get("database_type") or "",
                    }
        except Exception:
            pass

    return {
        "source_key": source_key,
        "display_name": source_key,
        "database_type": "",
        "description": None,
    }


async def _load_prompt_catalog_bundle(source_key: str) -> Dict[str, Any]:
    """Load the same catalog bundle the query graph uses for prompt context."""
    from src.api import state as app_state

    source = await _catalog_source()
    cache_status: Optional[Dict[str, Any]] = None

    if source == "mcp" and app_state.mcp_catalog_client and app_state.mcp_server_service:
        active = await app_state.mcp_server_service.get_active()
        if active:
            try:
                cache_status = await app_state.mcp_catalog_client.get_cache_status(
                    active.id, source_key
                )
            except Exception:
                cache_status = None
        try:
            bundle = await app_state.mcp_catalog_client.load_all(source_key)
            return {"source": "mcp", "bundle": bundle, "cache": cache_status}
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings: MCP resolved prompt catalog failed: %s", exc)

    if not app_state.metadata_loader:
        raise HTTPException(503, "MetadataLoader not initialised")
    bundle = await app_state.metadata_loader.load_all(source_key)
    return {"source": "db", "bundle": bundle, "cache": None}


def _placeholder_meta(values: Dict[str, str], sources: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = []
    for key in sorted(values):
        value = values[key]
        rows.append({
            "name": key,
            "source": sources.get(key, "runtime"),
            "characters": len(value),
            "tokens": _estimate_tokens(value),
            "preview": value[:240],
        })
    return rows


def _render_prompt_template(content: str, values: Dict[str, str]) -> str:
    """Render placeholders while tolerating custom prompts with stray braces."""
    try:
        return content.format_map(_SafeFormatDict(values))
    except Exception:
        rendered = content
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", value)
        return rendered.replace("{{", "{").replace("}}", "}")


# ── Routes ────────────────────────────────────────────────────────────────────────

@router.get("/prompts", response_model=List[PromptMeta])
async def list_prompts():
    """Return metadata for all prompts (no content), ordered by the registry."""
    db_rows = await _db_list_prompts()
    result = []
    for entry in PROMPT_REGISTRY:
        place = entry["name"]
        row = db_rows.get(place, {})
        content = row.get("content") or _read_file(entry["path"])
        result.append(PromptMeta(
            name=place,
            label=entry["label"],
            group=entry["group"],
            description=entry["description"],
            is_custom=row.get("is_custom", False),
            placeholders=_extract_placeholders(content),
            version=row.get("version", 1),
            model_id=row.get("model_id"),
            model_name=row.get("model_name"),
        ))
    return result


@router.get("/prompts/{name}", response_model=PromptDetail)
async def get_prompt(name: str):
    """Return full content for a single prompt."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    row = await _db_get_prompt(name)
    if row:
        content   = row["content"]
        is_custom = row["is_custom"]
        version   = row["version"]
        model_id  = row["model_id"]
        model_name = row["model_name"]
    else:
        content   = _read_file(entry["path"])
        is_custom = False
        version   = 1
        model_id  = None
        model_name = None
    return PromptDetail(
        name=name,
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=is_custom,
        placeholders=_extract_placeholders(content),
        content=content,
        version=version,
        model_id=model_id,
        model_name=model_name,
    )


@router.get("/prompt-contexts")
async def list_prompt_contexts():
    """Connection choices for prompt resolved view."""
    return await _list_prompt_context_connections()


@router.get("/prompts/{name}/resolved")
async def resolve_prompt(
    name: str,
    connection: str = Query(..., description="source_key/catalog to resolve against"),
):
    """Render a prompt with real catalog values for a selected connection.

    Runtime-only placeholders such as ``{question}`` stay visible but are
    labelled as runtime values. Catalog placeholders are loaded from MCP or the
    metadata DB according to the global catalog source.
    """
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    row = await _db_get_prompt(name)
    content = (row or {}).get("content") or _read_file(entry["path"])
    placeholders = _extract_placeholders(content)

    info = await _connection_info(connection)
    catalog = await _load_prompt_catalog_bundle(connection)
    bundle = catalog["bundle"]

    table_names = _extract_table_names(bundle.get("tables", ""))
    source_description = (
        bundle.get("sources")
        or info.get("description")
        or info.get("display_name")
        or connection
    )

    values: Dict[str, str] = {
        "connection_display_name": str(info.get("display_name") or connection),
        "database_type": str(info.get("database_type") or ""),
        "source_description": str(source_description),
        "tables": bundle.get("tables", ""),
        "columns": bundle.get("columns", ""),
        "relationships": bundle.get("relationships", ""),
        "sources": bundle.get("sources", ""),
        "knowledge_pairs": bundle.get("knowledge_pairs", ""),
        "business_terms": bundle.get("business_terms", ""),
        "available_tables": "\n".join(f"- {name}" for name in table_names),
        "business_rules": bundle.get("business_terms", ""),
        # Runtime placeholders cannot be known before a user action. Keep
        # explicit markers so the resolved view is honest rather than blank.
        "question": "{runtime: user question}",
        "partial": "{runtime: partial user input}",
        "conversation_history": "{runtime: conversation history}",
        "conversation_summary": "{runtime: conversation summary}",
        "recent_questions": "{runtime: recent questions}",
        "recent_messages": "{runtime: recent chart chat messages}",
        "instruction": "{runtime: chart edit instruction}",
        "current_config": "{runtime: current chart config}",
        "column_names": "{runtime: result column names}",
        "column_types": "{runtime: result column types}",
        "sample_rows": "{runtime: result sample rows}",
        "original_question": "{runtime: original user question}",
        "row_count": "{runtime: result row count}",
        "data_sample": "{runtime: data sample}",
        "column_stats": "{runtime: column statistics}",
        "sql": "{runtime: generated SQL}",
        "results_sample": "{runtime: result sample}",
        "error_context": "{runtime: SQL error context}",
        "retry_count": "{runtime: retry count}",
    }
    sources = {
        key: catalog["source"]
        for key in (
            "tables", "columns", "relationships", "sources",
            "knowledge_pairs", "business_terms", "available_tables",
            "business_rules", "source_description",
        )
    }
    sources.update({
        "connection_display_name": "connection",
        "database_type": "connection",
    })

    resolved = _render_prompt_template(content, values)
    unresolved = [
        p for p in placeholders
        if p not in values or values.get(p, "").startswith("{runtime:")
    ]

    return {
        "name": name,
        "label": entry["label"],
        "connection": {
            "source_key": connection,
            "display_name": info.get("display_name") or connection,
            "database_type": info.get("database_type") or "",
        },
        "catalog_source": catalog["source"],
        "catalog_cache": catalog.get("cache"),
        "tokenizer": _tokenizer_name(),
        "content": content,
        "resolved_content": resolved,
        "placeholders": placeholders,
        "unresolved_placeholders": unresolved,
        "placeholder_tokens": _placeholder_meta(
            {p: values[p] for p in placeholders if p in values},
            sources,
        ),
        "tokens": {
            "template": _estimate_tokens(content),
            "resolved": _estimate_tokens(resolved),
            "catalog": sum(_estimate_tokens(bundle.get(k, "")) for k in (
                "tables", "columns", "relationships", "sources",
                "knowledge_pairs", "business_terms",
            )),
        },
    }


@router.put("/prompts/{name}", response_model=PromptDetail)
async def save_prompt(name: str, body: PromptUpdate):
    """Save a custom prompt (new DB version)."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    new_version = await _db_save_prompt(name, body.content)
    _invalidate_cache(name)
    logger.info("settings: saved prompt '%s' v%d (%d chars)", name, new_version, len(body.content))

    row = await _db_get_prompt(name)
    return PromptDetail(
        name=name,
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=True,
        placeholders=_extract_placeholders(body.content),
        content=body.content,
        version=new_version,
        model_id=row["model_id"] if row else None,
        model_name=row["model_name"] if row else None,
    )


@router.delete("/prompts/{name}", response_model=PromptDetail)
async def reset_prompt(name: str):
    """Reset a prompt to its file default (inserts a new DB version)."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    default_content = _read_file(entry["path"])
    new_version = await _db_reset_prompt(name, default_content)
    _invalidate_cache(name)
    logger.info("settings: reset prompt '%s' to default (v%d)", name, new_version)

    return PromptDetail(
        name=name,
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=False,
        placeholders=_extract_placeholders(default_content),
        content=default_content,
        version=new_version,
        model_id=None,
        model_name=None,
    )


@router.post("/prompts/reload")
def reload_prompts():
    """Clear the in-process prompt cache so every prompt is re-read from DB."""
    try:
        from src.api import state as app_state
        if app_state.prompt_cache:
            app_state.prompt_cache.clear()
            return {"reloaded": True}
    except Exception as exc:  # noqa: BLE001
        logger.debug("settings: prompt reload error: %s", exc)
    return {"reloaded": False}


@router.put("/prompts/{name}/model")
async def set_prompt_model(name: str, body: SetPromptModelRequest):
    """Assign a specific model to this prompt (or clear with ``model_name=null``).

    ``null`` removes the override — the prompt will use the global active model.
    """
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()

    model_id: Optional[int] = None
    if body.model_name:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM admin_models WHERE name = $1 AND is_enabled = true",
                body.model_name,
            )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{body.model_name}' not found or not enabled",
            )
        model_id = row["id"]

    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE insights_prompts SET model_id = $1, updated_at = NOW() "
            "WHERE prompt_place = $2 AND is_active = true",
            model_id, name,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail=f"No active prompt row for '{name}'")

    _invalidate_cache(name)
    logger.info(
        "settings: prompt '%s' model → %s",
        name, body.model_name or "global default",
    )
    return {"name": name, "model_id": model_id, "model_name": body.model_name}


# ── Prompt version history endpoints ─────────────────────────────────────────

class PromptVersionMeta(BaseModel):
    id: int
    version: int
    is_active: bool
    is_custom: bool
    model_name: Optional[str] = None
    created_at: str  # ISO 8601


class PromptVersionDetail(PromptVersionMeta):
    content: str


@router.get("/prompts/{name}/versions", response_model=List[PromptVersionMeta])
async def list_prompt_versions(name: str):
    """Return all saved version rows for *name*, newest first."""
    if not _entry_for(name):
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ip.id, ip.version, ip.is_active, ip.is_custom,
                   am.name AS model_name, ip.created_at
            FROM insights_prompts ip
            LEFT JOIN admin_models am ON am.id = ip.model_id
            WHERE ip.prompt_place = $1
            ORDER BY ip.version DESC
            """,
            name,
        )
    return [
        PromptVersionMeta(
            id=r["id"],
            version=r["version"],
            is_active=r["is_active"],
            is_custom=r["is_custom"],
            model_name=r["model_name"],
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
        for r in rows
    ]


@router.get("/prompts/{name}/versions/{version_id}", response_model=PromptVersionDetail)
async def get_prompt_version(name: str, version_id: int):
    """Return full content for a specific version row by its DB id."""
    if not _entry_for(name):
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT ip.id, ip.version, ip.is_active, ip.is_custom,
                   am.name AS model_name, ip.created_at, ip.content
            FROM insights_prompts ip
            LEFT JOIN admin_models am ON am.id = ip.model_id
            WHERE ip.prompt_place = $1 AND ip.id = $2
            """,
            name, version_id,
        )
    if not row:
        raise HTTPException(
            status_code=404, detail=f"Version id={version_id} not found for '{name}'"
        )
    return PromptVersionDetail(
        id=row["id"],
        version=row["version"],
        is_active=row["is_active"],
        is_custom=row["is_custom"],
        model_name=row["model_name"],
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        content=row["content"],
    )


@router.post("/prompts/{name}/restore/{version_id}", response_model=PromptDetail)
async def restore_prompt_version(name: str, version_id: int):
    """Restore a past version as a new active version row."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        src = await conn.fetchrow(
            "SELECT content FROM insights_prompts WHERE prompt_place = $1 AND id = $2",
            name, version_id,
        )
    if not src:
        raise HTTPException(
            status_code=404, detail=f"Version id={version_id} not found for '{name}'"
        )

    new_version = await _db_save_prompt(name, src["content"])
    _invalidate_cache(name)
    logger.info(
        "settings: restored prompt '%s' from version_id=%d → new v%d",
        name, version_id, new_version,
    )

    active_row = await _db_get_prompt(name)
    return PromptDetail(
        name=name,
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=True,
        placeholders=_extract_placeholders(src["content"]),
        content=src["content"],
        version=new_version,
        model_id=active_row["model_id"] if active_row else None,
        model_name=active_row["model_name"] if active_row else None,
    )


# ── AI Model endpoints ───────────────────────────────────────────────────────

class ModelInfo(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    available: bool        # True = a credential row is configured (NOT proof it works)
    is_active: bool        # True = currently loaded in the LLM service
    is_default: bool       # True = flagged is_default in admin_models_providers
    healthy: Optional[bool] = None   # True/False from last live probe; None = not probed yet
    health_detail: Optional[str] = None  # reason from last probe (e.g. "401 …")


class SetModelRequest(BaseModel):
    name: str


async def _list_models_from_db() -> List[Dict[str, Any]]:
    """Query admin_models with credential availability from admin_models_providers."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                am.id,
                am.name,
                am.display_name,
                am.description,
                am.is_enabled,
                am.deployment_name,
                EXISTS(
                    SELECT 1 FROM admin_models_providers amp
                    WHERE amp.model_id = am.id AND amp.is_enabled = true
                ) AS has_credentials,
                EXISTS(
                    SELECT 1 FROM admin_models_providers amp
                    WHERE amp.model_id = am.id
                      AND amp.is_default = true
                      AND amp.is_enabled = true
                ) AS is_db_default
            FROM admin_models am
            ORDER BY am.sort_order, am.id
            """
        )
    return [dict(r) for r in rows]


async def _get_active_from_db() -> Optional[str]:
    """Return the persisted active model name, or None."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'active_model'"
        )
    return row["value"] if row else None


async def _set_active_in_db(name: str) -> None:
    """Upsert active_model in app_settings."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
                VALUES ('active_model', $1, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
            name,
        )


@router.get("/models", response_model=List[ModelInfo])
async def list_models():
    """Return all admin_models with availability and active flags.

    ``available`` is true when the model has at least one enabled row in
    ``admin_models_providers`` (i.e. credentials are configured).
    ``is_active`` reflects the persisted ``active_model`` setting.
    ``is_default`` reflects the ``is_default`` flag in admin_models_providers.
    """
    try:
        rows, active_model = await asyncio.gather(
            _list_models_from_db(),
            _get_active_from_db(),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not query models: {exc}") from exc

    # Merge in the last live health probe if one is cached (never probes here —
    # this endpoint must stay fast; call /models/health to refresh the snapshot).
    from src.agent import llm_health
    health = llm_health.cached_health()

    result = []
    for r in rows:
        available = bool(r.get("has_credentials")) and bool(r.get("is_enabled", True))
        h = health.get(r["name"])
        result.append(ModelInfo(
            id=r["id"],
            name=r["name"],
            display_name=r["display_name"] or r["name"],
            description=r["description"] or "",
            available=available,
            is_active=(active_model is not None and r["name"] == active_model),
            is_default=bool(r.get("is_db_default")),
            healthy=(h.healthy if h is not None else None),
            health_detail=(h.detail if h is not None else None),
        ))
    return result


@router.get("/models/health")
async def models_health(refresh: bool = False):
    """Probe each enabled model's credentials and report which actually work.

    Unlike ``available`` (which only means "a credential row exists"), this runs
    a tiny live generation against every provider so the result reflects real
    connectivity — expired keys, wrong endpoints, decommissioned deployments and
    missing drivers all show up here.

    Results are cached for a few minutes; pass ``?refresh=true`` to force a fresh
    probe (slower — it contacts every provider).
    """
    from src.agent import llm_health
    from src.metadata import get_metadata_pool

    try:
        pool = await get_metadata_pool()
        health = await llm_health.get_health(pool, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not probe models: {exc}") from exc

    # Sort working first, then failing, then skipped (non-chat / no driver).
    _rank = {llm_health.PASS: 0, llm_health.FAIL: 1, llm_health.SKIP: 2}
    items = sorted(health.values(), key=lambda h: (_rank.get(h.status, 3), h.name))
    healthy = [h.name for h in items if h.healthy is True]
    failing = [h.name for h in items if h.healthy is False]
    skipped = [h.name for h in items if h.healthy is None]
    return {
        "checked_age_seconds": llm_health.cache_age_seconds(),
        "total": len(items),
        "healthy_count": len(healthy),
        "failing_count": len(failing),
        "skipped_count": len(skipped),
        "healthy": healthy,
        "models": [h.as_dict() for h in items],
    }


@router.get("/models/active")
async def get_active_model():
    """Return the persisted active model name.

    Returns ``null`` for ``name`` when no explicit selection has been made yet.
    """
    from src.api import state as app_state

    active = await _get_active_from_db()
    current = app_state.llm_service.get_deployment() if app_state.llm_service else None
    return {"name": active, "current_deployment": current}


@router.put("/models/active")
async def set_active_model(body: SetModelRequest):
    """Switch to a different model live and persist the choice."""
    from src.api import state

    # Validate: must exist and have credentials configured.
    try:
        rows = await _list_models_from_db()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not query models: {exc}") from exc

    match = next((r for r in rows if r["name"] == body.name), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Model '{body.name}' not found")
    if not match.get("has_credentials"):
        raise HTTPException(
            status_code=400,
            detail=f"Model '{body.name}' has no configured credentials in admin_models_providers",
        )

    # Switch live — reloads credentials from DB.
    if state.llm_service:
        try:
            await state.llm_service.set_model(body.name)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Failed to switch to model '{body.name}': {exc}",
            ) from exc
    else:
        logger.warning("set_active_model: llm_service not in state, skipping live switch")

    # Persist.
    await _set_active_in_db(body.name)

    # Clear prompt cache: prompts with no model override (model_id=NULL) use
    # the global active model as fallback, so their cached chat model is stale.
    if state.prompt_cache:
        state.prompt_cache.clear()

    logger.info("settings: active model → %s", body.name)
    return {"name": body.name, "is_default": bool(match.get("is_db_default"))}


@router.get("/app-info")
def app_info():
    """Return read-only application metadata shown on the About page."""
    from src.config import settings as cfg
    from src.api import state as app_state

    active_model = (
        app_state.llm_service.get_deployment()
        if app_state.llm_service
        else getattr(cfg, "AZURE_OPENAI_DEPLOYMENT_NAME", "—")
    )
    return {
        "name": "Jeen Insights",
        "version": "2.0.0",
        "description": "Natural-language analytics powered by multiple LLM providers.",
        "llm_model": active_model,
        "llm_endpoint": _mask_url(getattr(cfg, "AZURE_OPENAI_ENDPOINT", "—")),
        "api_version": getattr(cfg, "AZURE_OPENAI_API_VERSION", "—"),
        "llm_timeout": getattr(cfg, "LLM_TIMEOUT_SECONDS", 30),
        "prompt_count": len(PROMPT_REGISTRY),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_reload_prompt_loader() -> bool:
    """Best-effort call to PromptLoader.reload() via the running agent."""
    try:
        from src.api.lifespan import get_agent  # type: ignore[import]
        agent = get_agent()
        if agent and hasattr(agent, "_prompt_loader"):
            agent._prompt_loader.reload()
            return True
    except Exception as exc:
        logger.debug("settings: could not reload PromptLoader: %s", exc)
    return False


def _mask_url(url: str) -> str:
    """Mask the subdomain of an Azure endpoint for display."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        parts = host.split(".")
        if len(parts) >= 2:
            parts[0] = parts[0][:4] + "****"
        return parsed.scheme + "://" + ".".join(parts)
    except Exception:
        return "—"
