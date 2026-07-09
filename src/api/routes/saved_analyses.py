"""Saved analysis snapshots: table data + chart + insights restore."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from src.api.dependencies import get_history_service, require_user_id
from src.api.models import SaveAnalysisRequest, UpdateSavedAnalysisRequest

router = APIRouter(prefix="/api", tags=["saved-analyses"])


def _rows_from_results(results: dict) -> tuple[list, list]:
    columns = list(results.get("columns") or [])
    rows = results.get("rows") or results.get("data") or []
    return columns, list(rows or [])


@router.get("/saved-analyses")
async def list_saved_analyses(
    connection: str = Query(...),
    user_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
):
    user_id = require_user_id(user_id)
    history = get_history_service()
    entries = await history.list_saved_analyses(
        user_id=user_id, source_key=connection, limit=limit
    )
    return {"items": entries}


@router.post("/saved-analyses")
async def save_analysis(request: SaveAnalysisRequest):
    user_id = require_user_id(request.user_id)
    columns, rows = _rows_from_results(request.results or {})
    if not columns or not rows:
        raise HTTPException(status_code=400, detail="Cannot save an empty result set")

    history = get_history_service()
    try:
        saved_id = await history.save_analysis(
            user_id=user_id,
            source_key=request.connection,
            connection_id=request.connection,
            name=request.name or request.question[:80] or "Saved analysis",
            question=request.question,
            generated_sql=request.sql,
            query_id=request.query_id,
            columns=columns,
            rows=rows,
            chart_spec=request.chart_spec,
            chart_config=request.chart_config,
            insights_payload=request.insights,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Query not found for this user") from exc
    return {"id": str(saved_id), "success": True}


@router.get("/saved-analyses/{saved_id}")
async def get_saved_analysis(saved_id: UUID, user_id: str = Query(...)):
    user_id = require_user_id(user_id)
    history = get_history_service()
    item = await history.get_saved_analysis(saved_id=saved_id, user_id=user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Saved analysis not found")
    return item


@router.patch("/saved-analyses/{saved_id}")
async def update_saved_analysis(saved_id: UUID, request: UpdateSavedAnalysisRequest):
    user_id = require_user_id(request.user_id)
    history = get_history_service()
    ok = await history.update_saved_analysis(
        saved_id=saved_id,
        user_id=user_id,
        name=request.name,
        chart_spec=request.chart_spec,
        chart_config=request.chart_config,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Saved analysis not found")
    return {"success": True}


@router.delete("/saved-analyses/{saved_id}")
async def delete_saved_analysis(saved_id: UUID, user_id: str = Query(...)):
    user_id = require_user_id(user_id)
    history = get_history_service()
    ok = await history.delete_saved_analysis(saved_id=saved_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Saved analysis not found")
    return {"success": True}
