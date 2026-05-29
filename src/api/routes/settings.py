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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Provider tag used to identify Azure-backed models (selectable without extra creds).
_AZURE_TAG = "(Azure)"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# ── Prompt file registry ─────────────────────────────────────────────────────

_BASE = Path(__file__).resolve().parent.parent.parent  # src/
_PROMPTS_DIR = _BASE / "agent" / "prompts"
_DEFAULTS_DIR = _PROMPTS_DIR / ".defaults"
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
    # Blank out escaped double-brace sequences first so they're not matched.
    cleaned = _ESCAPED_RE.sub("", text)
    return sorted(set(_PLACEHOLDER_RE.findall(cleaned)))


def _entry_for(name: str) -> Optional[Dict[str, Any]]:
    return next((e for e in PROMPT_REGISTRY if e["name"] == name), None)


def _read_prompt(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _is_custom(entry: Dict[str, Any]) -> bool:
    """True when a backup exists — meaning the main file has been customised."""
    backup = _DEFAULTS_DIR / entry["path"].name
    return backup.exists()


# ── Response models ──────────────────────────────────────────────────────────

class PromptMeta(BaseModel):
    name: str
    label: str
    group: str
    description: str
    is_custom: bool
    placeholders: List[str]


class PromptDetail(PromptMeta):
    content: str


class PromptUpdate(BaseModel):
    content: str


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/prompts", response_model=List[PromptMeta])
def list_prompts():
    """Return metadata for all prompts (no content)."""
    result = []
    for entry in PROMPT_REGISTRY:
        text = _read_prompt(entry["path"])
        result.append(PromptMeta(
            name=entry["name"],
            label=entry["label"],
            group=entry["group"],
            description=entry["description"],
            is_custom=_is_custom(entry),
            placeholders=_extract_placeholders(text),
        ))
    return result


@router.get("/prompts/{name}", response_model=PromptDetail)
def get_prompt(name: str):
    """Return full content for a single prompt."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    text = _read_prompt(entry["path"])
    return PromptDetail(
        name=entry["name"],
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=_is_custom(entry),
        placeholders=_extract_placeholders(text),
        content=text,
    )


@router.put("/prompts/{name}", response_model=PromptDetail)
def save_prompt(name: str, body: PromptUpdate):
    """Save a custom prompt.  Backs up the original on the first save."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    prompt_path: Path = entry["path"]

    # Ensure defaults directory exists.
    _DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = _DEFAULTS_DIR / prompt_path.name

    # Back up the original only once so it stays pristine.
    if not backup_path.exists() and prompt_path.exists():
        backup_path.write_text(prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("settings: backed up default prompt → %s", backup_path)

    # Write the custom content.
    prompt_path.write_text(body.content, encoding="utf-8")
    logger.info("settings: saved custom prompt '%s' (%d chars)", name, len(body.content))

    # Hot-reload the PromptLoader if the agent exposes one.
    _try_reload_prompt_loader()

    return PromptDetail(
        name=entry["name"],
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=True,
        placeholders=_extract_placeholders(body.content),
        content=body.content,
    )


@router.delete("/prompts/{name}", response_model=PromptDetail)
def reset_prompt(name: str):
    """Reset a prompt to its default by restoring the backup."""
    entry = _entry_for(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    prompt_path: Path = entry["path"]
    backup_path = _DEFAULTS_DIR / prompt_path.name

    if not backup_path.exists():
        # Already at default — nothing to do.
        text = _read_prompt(prompt_path)
        return PromptDetail(
            name=entry["name"],
            label=entry["label"],
            group=entry["group"],
            description=entry["description"],
            is_custom=False,
            placeholders=_extract_placeholders(text),
            content=text,
        )

    # Restore original.
    original = backup_path.read_text(encoding="utf-8")
    prompt_path.write_text(original, encoding="utf-8")
    backup_path.unlink()
    logger.info("settings: reset prompt '%s' to default", name)

    _try_reload_prompt_loader()

    return PromptDetail(
        name=entry["name"],
        label=entry["label"],
        group=entry["group"],
        description=entry["description"],
        is_custom=False,
        placeholders=_extract_placeholders(original),
        content=original,
    )


@router.post("/prompts/reload")
def reload_prompts():
    """Force the PromptLoader to re-read all files from disk."""
    reloaded = _try_reload_prompt_loader()
    return {"reloaded": reloaded}


# ── AI Model endpoints ───────────────────────────────────────────────────────

class ModelInfo(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    available: bool        # True = selectable with current credentials
    is_active: bool        # True = currently loaded in the LLM service
    is_default: bool       # True = matches AZURE_OPENAI_DEPLOYMENT_NAME env setting


class SetModelRequest(BaseModel):
    name: str


async def _list_models_from_db() -> List[Dict[str, Any]]:
    """Query admin_models, returning rows ordered by sort_order."""
    from src.metadata import get_metadata_pool
    pool = await get_metadata_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, display_name, description, is_enabled "
            "FROM admin_models "
            "ORDER BY sort_order, id"
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

    ``is_active`` is based on the persisted ``active_model`` key in
    ``app_settings`` — not the env-var deployment name.  This lets the UI
    correctly highlight whichever model the user last explicitly selected,
    even when Azure deployment names differ from DB model names.
    ``is_default`` marks the model whose name matches the env-var deployment
    when no active selection has been made yet.
    """
    from src.config import settings as cfg

    default_deployment = getattr(cfg, "AZURE_OPENAI_DEPLOYMENT_NAME", "")

    try:
        rows, active_model = await asyncio.gather(
            _list_models_from_db(),
            _get_active_from_db(),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not query models: {exc}") from exc

    result = []
    for r in rows:
        available = _AZURE_TAG in (r["display_name"] or "")
        result.append(ModelInfo(
            id=r["id"],
            name=r["name"],
            display_name=r["display_name"] or r["name"],
            description=r["description"] or "",
            available=available,
            is_active=(active_model is not None and r["name"] == active_model),
            is_default=(r["name"] == default_deployment),
        ))
    return result


@router.get("/models/active")
async def get_active_model():
    """Return the persisted active model name (the DB model name, not the
    deployment string).  Returns ``null`` for ``name`` when no explicit
    selection has been made yet.
    """
    from src.config import settings as cfg

    default_deployment = getattr(cfg, "AZURE_OPENAI_DEPLOYMENT_NAME", "")
    active = await _get_active_from_db()
    return {"name": active, "default_deployment": default_deployment}


@router.put("/models/active")
async def set_active_model(body: SetModelRequest):
    """Switch to a different model live and persist the choice."""
    from src.config import settings as cfg
    from src.api import state

    # Validate: must exist and be available (Azure).
    try:
        rows = await _list_models_from_db()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not query models: {exc}") from exc

    match = next((r for r in rows if r["name"] == body.name), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Model '{body.name}' not found")
    if _AZURE_TAG not in (match["display_name"] or ""):
        raise HTTPException(
            status_code=400,
            detail=f"Model '{body.name}' is not available with current credentials",
        )

    # Apply live.
    if state.llm_service:
        state.llm_service.set_deployment(body.name)
    else:
        logger.warning("set_active_model: llm_service not in state, skipping live switch")

    # Persist.
    await _set_active_in_db(body.name)

    default_deployment = getattr(cfg, "AZURE_OPENAI_DEPLOYMENT_NAME", "")
    logger.info("settings: active model → %s", body.name)
    return {"name": body.name, "is_default": body.name == default_deployment}


@router.get("/app-info")
def app_info():
    """Return read-only application metadata shown on the About page."""
    from src.config import settings as cfg
    return {
        "name": "Jeen Insights",
        "version": "2.0.0",
        "description": "Natural-language analytics powered by Azure OpenAI.",
        "llm_model": getattr(cfg, "AZURE_OPENAI_DEPLOYMENT_NAME", "—"),
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
