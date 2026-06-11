"""Runtime guardrail settings API.

Exposes the live-editable global knobs stored in ``app_settings``:
DB statement timeout, max result rows, and the conversation-context window.
Backed by ``src/metadata/runtime_settings.py``.
"""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.metadata import runtime_settings as rs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class RuntimeSettingsResponse(BaseModel):
    db_statement_timeout_ms: int
    max_result_rows: int
    conversation_context_turns: int
    bounds: Dict[str, Dict[str, int]]


class RuntimeSettingsUpdate(BaseModel):
    db_statement_timeout_ms: int | None = None
    max_result_rows: int | None = None
    conversation_context_turns: int | None = None


@router.get("/runtime", response_model=RuntimeSettingsResponse)
async def get_runtime():
    """Return the effective runtime guardrails plus their clamp bounds."""
    current = await rs.get_runtime_settings(use_cache=False)
    return RuntimeSettingsResponse(
        db_statement_timeout_ms=current.db_statement_timeout_ms,
        max_result_rows=current.max_result_rows,
        conversation_context_turns=current.conversation_context_turns,
        bounds=rs.bounds(),
    )


@router.put("/runtime", response_model=RuntimeSettingsResponse)
async def update_runtime(body: RuntimeSettingsUpdate):
    """Upsert any provided runtime guardrail(s); values are clamped to bounds."""
    updates = {
        k: v
        for k, v in body.model_dump().items()
        if v is not None
    }
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    try:
        for key, value in updates.items():
            await rs.set_runtime_setting(key, int(value))
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"Could not save runtime settings: {exc}"
        ) from exc

    current = await rs.get_runtime_settings(use_cache=False)
    return RuntimeSettingsResponse(
        db_statement_timeout_ms=current.db_statement_timeout_ms,
        max_result_rows=current.max_result_rows,
        conversation_context_turns=current.conversation_context_turns,
        bounds=rs.bounds(),
    )
