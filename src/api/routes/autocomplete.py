"""Autocomplete: catalog questions, column lookup, and Tier-3 LLM suggestions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from src.api.dependencies import get_metadata_loader, resolve_agent
from src.api.llm_json import extract_json_object, normalise_corrections
from src.api.llm_params import AUTOCOMPLETE_PARAMS
from src.api.models import SuggestQuestionsRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["autocomplete"])

# ----------------------------------------------------------------------
# Prompt + budget constants
# ----------------------------------------------------------------------
_AUTOCOMPLETE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "agent"
    / "prompts"
    / "autocomplete_suggestions.md"
)
_MAX_PARTIAL_CHARS = 200
_MAX_TABLES_INJECTED = 200
_MAX_RECENT_INJECTED = 10
_MAX_RECENT_CHARS = 1500


def _load_autocomplete_prompt() -> str:
    """Re-read the externalised prompt on every call so editing the .md file
    has zero deploy cost in dev."""
    return _AUTOCOMPLETE_PROMPT_PATH.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Tier 2 catalog (knowledge_pairs questions)
# ----------------------------------------------------------------------
@router.get("/knowledge-questions")
async def get_knowledge_questions(
    connection: str = Query(..., description="source_key of the active connection"),
):
    loader = get_metadata_loader()
    items = await loader.load_knowledge_questions(connection)
    return {
        "connection": connection,
        "questions": items,
        "truncated": len(items) >= 2000,
    }


# ----------------------------------------------------------------------
# `#` trigger — columns for the active connection
# ----------------------------------------------------------------------
@router.get("/knowledge-columns")
async def get_knowledge_columns(
    connection: str = Query(..., description="source_key of the active connection"),
    table: Optional[str] = Query(None, description="Optional: scope to one table"),
):
    loader = get_metadata_loader()
    items = await loader.load_columns(connection, table)
    return {
        "connection": connection,
        "table": table,
        "columns": items,
        "truncated": (len(items) >= 5000 if not table else len(items) >= 2000),
    }


# ----------------------------------------------------------------------
# Tier 3 LLM
# ----------------------------------------------------------------------
@router.post("/suggest-questions")
async def suggest_questions(request: SuggestQuestionsRequest):
    """Tier 3 autocomplete: ask the LLM for completions when the cheap tiers
    returned nothing. Server-side guards: connection required, partial must be
    >= 10 chars (mirrors client gate), small token budget, JSON-only output.
    """
    partial = (request.partial or "").strip()
    if len(partial) < 10:
        raise HTTPException(
            status_code=400,
            detail="`partial` must be at least 10 characters.",
        )

    agent = await resolve_agent(request.connection)

    # Build the table list. Trust the client's hint if it sent one (already
    # filtered to the active connection); otherwise derive from the metadata
    # bundle we cache for the system prompt.
    table_names: List[str] = list(request.table_names or [])
    if not table_names:
        bundle = await agent.metadata_loader.load_all(agent.source_key)
        for line in bundle.get("tables", "").splitlines():
            line = line.strip()
            if line.startswith("- "):
                head = line[2:].split(" - ", 1)[0].strip()
                if head:
                    table_names.append(head)
    table_names = table_names[:_MAX_TABLES_INJECTED]

    recent = (request.recent_questions or [])[:_MAX_RECENT_INJECTED]
    recent_blob = (
        "\n".join(f"- {q}" for q in recent if q)[:_MAX_RECENT_CHARS] or "(none)"
    )

    available_tables_blob = (
        "\n".join(f"- {t}" for t in table_names) or "(no tables)"
    )

    from src.api import state as app_state
    if app_state.prompt_cache:
        try:
            template        = await app_state.prompt_cache.get_content("autocomplete_suggestions")
            model_override  = await app_state.prompt_cache.get_model_override("autocomplete_suggestions")
        except Exception:
            template       = _load_autocomplete_prompt()
            model_override = None
    else:
        template       = _load_autocomplete_prompt()
        model_override = None

    prompt = template.format(
        partial=partial[:_MAX_PARTIAL_CHARS],
        available_tables=available_tables_blob,
        recent_questions=recent_blob,
        connection_display_name=agent.display_name,
        database_type=agent.database_type,
    )

    try:
        response = await agent.llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": partial[:_MAX_PARTIAL_CHARS]},
            ],
            temperature=AUTOCOMPLETE_PARAMS.temperature,
            max_tokens=AUTOCOMPLETE_PARAMS.max_tokens,
            model_override=model_override,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Autocomplete LLM call failed")
        # Don't 500 — autocomplete is non-critical; return an empty result.
        return {"suggestions": [], "corrections": [], "error": str(e)}

    raw = response.get("content") or ""
    parsed = extract_json_object(raw)
    if not isinstance(parsed, dict):
        logger.warning("Autocomplete: unparseable LLM response (%s chars)", len(raw))
        return {"suggestions": [], "corrections": []}

    suggestions = parsed.get("suggestions") or []
    corrections = parsed.get("corrections") or []
    if not isinstance(suggestions, list):
        suggestions = []
    if not isinstance(corrections, list):
        corrections = []
    suggestions = [
        str(s).strip() for s in suggestions if s and str(s).strip()
    ][:4]
    cleaned_corrections = normalise_corrections(corrections)
    return {"suggestions": suggestions, "corrections": cleaned_corrections}
