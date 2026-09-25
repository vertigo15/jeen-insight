"""User history: recent / pinned / feedback / conversation.

Identity comes from the verified internal-token Principal only. The legacy
``user_id`` query/body fields are still accepted for backward compatibility
with older UI builds but are never trusted.
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import get_history_service, get_principal
from src.api.models import (
    AnswerFeedbackRequest,
    AnswerFeedbackResponse,
    FeedbackRequest,
    PinQuestionRequest,
)
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["history"])


@router.get("/user/recent-questions")
async def get_user_recent_questions(
    connection: str = Query(...),
    user_id: Optional[str] = Query(None, include_in_schema=False),
    limit: int = 15,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    questions = await history.get_user_recent_questions(
        user_id=principal.user_id, source_key=connection, limit=limit
    )
    return {"questions": questions}


@router.get("/user/pinned-questions")
async def get_user_pinned_questions(
    connection: str = Query(...),
    user_id: Optional[str] = Query(None, include_in_schema=False),
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    questions = await history.get_user_pinned_questions(
        user_id=principal.user_id, source_key=connection
    )
    return {"questions": questions}


@router.post("/user/pin-question")
async def pin_question(
    request: PinQuestionRequest,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    success = await history.pin_question(
        user_id=principal.user_id,
        source_key=request.connection,
        question=request.question,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pin question")
    return {"success": True, "message": "Question pinned"}


@router.post("/user/unpin-question")
async def unpin_question(
    request: PinQuestionRequest,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    success = await history.unpin_question(
        user_id=principal.user_id,
        source_key=request.connection,
        question=request.question,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to unpin question")
    return {"success": True, "message": "Question unpinned"}


@router.get("/user/history-log")
async def get_history_log(
    connection: str = Query(...),
    user_id: Optional[str] = Query(None, include_in_schema=False),
    limit: int = 100,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    entries = await history.get_history_log(
        user_id=principal.user_id, source_key=connection, limit=limit
    )
    return {"entries": entries}


@router.post("/feedback")
async def record_feedback(
    request: FeedbackRequest,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    # Thumbs from older UI builds land in the append-only log once migration
    # 035 is applied, so the conversation DTO keeps seeing them; notes / SQL
    # corrections and the non-quality values stay on the turn row.
    thumb_to_log = request.feedback if request.feedback in ("thumbs_up", "thumbs_down") else None
    if thumb_to_log and _answer_feedback_ready(history):
        saved = await _append_answer_feedback(
            history, query_id=request.query_id, user_id=principal.user_id,
            thumb=thumb_to_log, message=request.notes,
        )
        connection = saved.get("source_key") or request.connection
        if request.corrected_sql:
            await history.record_feedback(
                query_id=request.query_id, user_id=principal.user_id,
                user_feedback=request.feedback, corrected_sql=request.corrected_sql,
                feedback_notes=request.notes,
            )
    else:
        success = await history.record_feedback(
            query_id=request.query_id,
            user_id=principal.user_id,
            user_feedback=request.feedback,
            corrected_sql=request.corrected_sql,
            feedback_notes=request.notes,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Query not found for this user")
        connection = request.connection
    await _log_result_feedback(
        query_id=request.query_id,
        user_id=principal.user_id,
        connection=connection,
        feedback=request.feedback,
        has_notes=bool(request.notes),
        has_corrected_sql=bool(request.corrected_sql),
    )
    return {"status": "success", "message": "Feedback recorded"}


@router.post("/answer-feedback", response_model=AnswerFeedbackResponse)
async def record_answer_feedback(
    request: AnswerFeedbackRequest,
    principal: Principal = Depends(get_principal),
):
    """Append one answer-feedback event (thumb click or Give feedback dialog).

    Writes to ``insights_answer_feedback`` (migration 035). The turn must
    belong to the caller; otherwise 404, never a write. ``connection`` in the
    body is not trusted: the stored source_key is the turn's own.
    """
    history = get_history_service()
    if not _answer_feedback_ready(history):
        raise HTTPException(status_code=503, detail="Answer feedback is not available on this database")
    saved = await _append_answer_feedback(
        history,
        query_id=request.query_id,
        user_id=principal.user_id,
        thumb=request.thumb,
        rating=request.rating,
        feedback_type=request.feedback_type,
        message=request.message,
    )
    await _log_result_feedback(
        query_id=request.query_id,
        user_id=principal.user_id,
        connection=saved.get("source_key"),
        feedback=request.thumb,
        rating=request.rating,
        feedback_type=request.feedback_type,
        has_notes=bool(request.message),
    )
    return AnswerFeedbackResponse(feedback_id=saved["id"], thumb=saved.get("thumb"))


def _answer_feedback_ready(history) -> bool:
    """Migration 035 probed present. Strict ``is True`` so a stub/absent flag reads as off."""
    return getattr(history, "answer_feedback_schema_ready", False) is True


async def _append_answer_feedback(history, **fields) -> dict:
    """Write one event; 404 when the turn is not the caller's, 500 on a DB fault.

    Keeping the two apart matters: a swallowed database error reported as
    "not found" hides outages and schema drift behind an ownership message.
    """
    try:
        saved = await history.record_answer_feedback(**fields)
    except Exception:  # noqa: BLE001
        logger.exception("answer feedback write failed")
        raise HTTPException(status_code=500, detail="Could not save feedback")
    if not saved:
        raise HTTPException(status_code=404, detail="Query not found for this user")
    return saved


async def _log_result_feedback(
    *,
    query_id: UUID,
    user_id: str,
    connection: Optional[str],
    feedback: Optional[str],
    rating: Optional[int] = None,
    feedback_type: Optional[str] = None,
    has_notes: bool = False,
    has_corrected_sql: bool = False,
) -> None:
    """One structured line per feedback so quality can be measured per method.

    This event is what makes a thumb / rating actionable in the logs
    (thumbs-down rate by skill / method / accuracy band); the durable record
    is the ``insights_answer_feedback`` row. Best-effort: the feedback is
    already stored when this runs.
    """
    event: dict = {
        "event": "result_feedback",
        "query_id": str(query_id),
        "feedback": feedback,
        "rating": rating,
        "feedback_type": feedback_type,
        "has_notes": has_notes,
        "has_corrected_sql": has_corrected_sql,
    }
    if connection:
        try:
            turn = await get_history_service().get_turn_analysis(
                turn_id=query_id, user_id=user_id, source_key=connection,
            )
        except Exception:  # noqa: BLE001
            turn = None
        analysis = (turn or {}).get("analysis") if isinstance(turn, dict) else None
        if isinstance(analysis, dict) and analysis.get("skill"):
            validation = analysis.get("validation") or {}
            event.update({
                "skill": analysis.get("skill"),
                "method": analysis.get("method_used"),
                "metric": validation.get("metric"),
                "metric_value": validation.get("value"),
                "band": validation.get("band"),
                "coverage": validation.get("coverage"),
                "low_confidence": bool(analysis.get("low_confidence") or (turn or {}).get("low_confidence")),
            })
    logger.info("result_feedback %s", json.dumps(event, default=str, ensure_ascii=False))


@router.get("/conversation/{session_id}")
async def get_conversation_history(
    session_id: UUID,
    include_insights: bool = True,
    user_id: Optional[str] = Query(None, include_in_schema=False),
    principal: Principal = Depends(get_principal),
):
    """Legacy raw dump of one conversation. Superseded by /api/conversations."""
    history = get_history_service()
    return await history.get_conversation_history(
        session_id=session_id,
        user_id=principal.user_id,
        include_insights=include_insights,
    )
