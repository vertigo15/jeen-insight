"""User history: recent / pinned / feedback / conversation."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from src.api.dependencies import get_history_service, require_user_id
from src.api.models import FeedbackRequest, PinQuestionRequest

router = APIRouter(prefix="/api", tags=["history"])


@router.get("/user/recent-questions")
async def get_user_recent_questions(
    connection: str = Query(...),
    user_id: str = Query(...),
    limit: int = 15,
):
    user_id = require_user_id(user_id)
    history = get_history_service()
    questions = await history.get_user_recent_questions(
        user_id=user_id, source_key=connection, limit=limit
    )
    return {"questions": questions}


@router.get("/user/pinned-questions")
async def get_user_pinned_questions(connection: str = Query(...), user_id: str = Query(...)):
    user_id = require_user_id(user_id)
    history = get_history_service()
    questions = await history.get_user_pinned_questions(
        user_id=user_id, source_key=connection
    )
    return {"questions": questions}


@router.post("/user/pin-question")
async def pin_question(request: PinQuestionRequest):
    user_id = require_user_id(request.user_id)
    history = get_history_service()
    success = await history.pin_question(
        user_id=user_id,
        source_key=request.connection,
        question=request.question,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pin question")
    return {"success": True, "message": "Question pinned"}


@router.post("/user/unpin-question")
async def unpin_question(request: PinQuestionRequest):
    user_id = require_user_id(request.user_id)
    history = get_history_service()
    success = await history.unpin_question(
        user_id=user_id,
        source_key=request.connection,
        question=request.question,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to unpin question")
    return {"success": True, "message": "Question unpinned"}


@router.get("/user/history-log")
async def get_history_log(
    connection: str = Query(...),
    user_id: str = Query(...),
    limit: int = 100,
):
    user_id = require_user_id(user_id)
    history = get_history_service()
    entries = await history.get_history_log(
        user_id=user_id, source_key=connection, limit=limit
    )
    return {"entries": entries}


@router.post("/feedback")
async def record_feedback(request: FeedbackRequest):
    user_id = require_user_id(request.user_id)
    history = get_history_service()
    success = await history.record_feedback(
        query_id=request.query_id,
        user_id=user_id,
        user_feedback=request.feedback,
        corrected_sql=request.corrected_sql,
        feedback_notes=request.notes,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Query not found for this user")
    return {"status": "success", "message": "Feedback recorded"}


@router.get("/conversation/{session_id}")
async def get_conversation_history(
    session_id: UUID,
    include_insights: bool = True,
    user_id: str = Query(...),
):
    user_id = require_user_id(user_id)
    history = get_history_service()
    return await history.get_conversation_history(
        session_id=session_id, user_id=user_id, include_insights=include_insights
    )
