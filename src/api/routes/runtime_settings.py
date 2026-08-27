"""Runtime guardrail settings API.

Exposes the live-editable global knobs stored in ``app_settings``: DB statement
timeout, max result rows, the conversation-context window, and the text-to-DAX
entity-resolution controls (including the switch that turns it off).
Backed by ``src/metadata/runtime_settings.py``.
"""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.dependencies import require_admin
from src.metadata import runtime_settings as rs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class RuntimeSettingsResponse(BaseModel):
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
    bounds: Dict[str, Dict[str, float]]


class RuntimeSettingsUpdate(BaseModel):
    db_statement_timeout_ms: int | None = None
    max_result_rows: int | None = None
    conversation_context_turns: int | None = None
    dax_entity_resolution_enabled: bool | None = None
    dax_entity_max_domain_values: int | None = None
    dax_entity_match_threshold: float | None = None
    dax_entity_cross_column_enabled: bool | None = None
    sql_filter_resolution_enabled: bool | None = None
    sql_filter_max_domain_values: int | None = None
    sql_filter_match_threshold: float | None = None
    sql_filter_lookup_timeout_ms: int | None = None
    sql_filter_cache_ttl_seconds: int | None = None


def _response(current: rs.RuntimeSettings) -> RuntimeSettingsResponse:
    return RuntimeSettingsResponse(
        db_statement_timeout_ms=current.db_statement_timeout_ms,
        max_result_rows=current.max_result_rows,
        conversation_context_turns=current.conversation_context_turns,
        dax_entity_resolution_enabled=current.dax_entity_resolution_enabled,
        dax_entity_max_domain_values=current.dax_entity_max_domain_values,
        dax_entity_match_threshold=current.dax_entity_match_threshold,
        dax_entity_cross_column_enabled=current.dax_entity_cross_column_enabled,
        sql_filter_resolution_enabled=current.sql_filter_resolution_enabled,
        sql_filter_max_domain_values=current.sql_filter_max_domain_values,
        sql_filter_match_threshold=current.sql_filter_match_threshold,
        sql_filter_lookup_timeout_ms=current.sql_filter_lookup_timeout_ms,
        sql_filter_cache_ttl_seconds=current.sql_filter_cache_ttl_seconds,
        bounds=rs.bounds(),
    )


@router.get("/runtime", response_model=RuntimeSettingsResponse)
async def get_runtime():
    """Return the effective runtime guardrails plus their clamp bounds."""
    current = await rs.get_runtime_settings(use_cache=False)
    return _response(current)


@router.put("/runtime", response_model=RuntimeSettingsResponse, dependencies=[Depends(require_admin)])
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
            await rs.set_runtime_setting(key, value)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"Could not save runtime settings: {exc}"
        ) from exc

    current = await rs.get_runtime_settings(use_cache=False)
    return _response(current)
