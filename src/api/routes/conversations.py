"""Conversation restore / browse endpoints.

Identity comes exclusively from the verified internal-token Principal. Every
handler scopes its reads and writes by ``principal.user_id``; a conversation
that belongs to someone else is indistinguishable from a missing one (404).

* ``GET  /api/conversations/last``            hydration payload for the most
                                              recent conversation on a
                                              connection; spawns the on-open
                                              retention prune.
* ``GET  /api/conversations``                 cursor-paged list (one connection
                                              or ``all=true``).
* ``GET  /api/conversations/{id}``            header + paged turn metadata.
* ``GET  /api/conversations/{id}/turns/{turn_id}/artifact``
                                              result snapshot + chart baseline.
* ``POST /api/conversations/{id}/turns/{turn_id}/rerun``
                                              re-execute the stored query.
* ``PATCH /api/conversations/{id}``           rename.
* ``DELETE /api/conversations/{id}``          hard delete (cascades).
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api import conversation_retention
from src.api.conversation_rerun import rerun_turn
from src.api.dependencies import (
    get_connection_service,
    get_history_service,
    get_principal,
)
from src.api.models import (
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    ConversationTurn,
    RenameConversationRequest,
    RerunTurnResponse,
    TurnArtifact,
)
from src.config import settings
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["conversations"])


async def _available_source_keys() -> Optional[set]:
    """Active connection names, or None when the lookup is unavailable."""
    try:
        service = get_connection_service()
        connections = await service.list_connections()
        return {c.source_key for c in connections}
    except Exception:  # noqa: BLE001
        logger.debug("connection list unavailable for conversation summaries", exc_info=True)
        return None


def _summary(row: dict, available: Optional[set]) -> ConversationSummary:
    return ConversationSummary(
        **row,
        connection_available=(available is None or row["source_key"] in available),
    )


def _page_size(limit: Optional[int]) -> int:
    default = max(1, int(settings.CONVERSATION_MAX_TURNS_HYDRATED))
    if limit is None:
        return default
    return max(1, min(int(limit), 200))


async def _detail(
    history,
    *,
    summary_row: dict,
    user_id: str,
    limit: int,
    before: Optional[int],
    available: Optional[set],
) -> ConversationDetail:
    turns = await history.get_conversation_turns(
        conversation_id=UUID(summary_row["id"]),
        user_id=user_id,
        limit=limit,
        before_sequence=before,
    )
    next_cursor = None
    if len(turns) >= limit and turns:
        next_cursor = int(turns[-1]["sequence_number"])
    return ConversationDetail(
        conversation=_summary(summary_row, available),
        turns=[ConversationTurn(**t) for t in turns],
        next_cursor=next_cursor,
    )


@router.get("/conversations/last", response_model=Optional[ConversationDetail])
async def get_last_conversation(
    connection: str = Query(..., min_length=1),
    limit: Optional[int] = Query(None, ge=1, le=200),
    principal: Principal = Depends(get_principal),
):
    """Hydration payload for the user's most recent conversation on *connection*.

    Also the "app opened" signal: once the conversation is resolved, the
    per-user retention prune is spawned as a detached task with that
    conversation protected, so the response is never delayed by it.
    """
    history = get_history_service()
    user_id = principal.user_id
    summary_row = await history.get_last_conversation(
        user_id=user_id, source_key=connection
    )
    protect: Optional[UUID] = UUID(summary_row["id"]) if summary_row else None
    conversation_retention.maybe_schedule_prune(
        history,
        user_id=user_id,
        source_key=connection,
        protect_conversation_id=protect,
    )
    if summary_row is None:
        return None
    available = await _available_source_keys()
    return await _detail(
        history,
        summary_row=summary_row,
        user_id=user_id,
        limit=_page_size(limit),
        before=None,
        available=available,
    )


def _encode_list_cursor(last_activity_at: str, conversation_id: str) -> str:
    """Opaque, URL-safe cursor. ISO timestamps carry '+', which query-string
    decoding turns into a space, so the raw form is never exposed."""
    raw = f"{last_activity_at}|{conversation_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_list_cursor(raw: Optional[str]) -> Optional[Tuple[datetime, UUID]]:
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        ts_raw, id_raw = decoded.split("|", 1)
        return datetime.fromisoformat(ts_raw), UUID(id_raw)
    except (ValueError, AttributeError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(status_code=400, detail="Invalid cursor")


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    connection: Optional[str] = Query(None),
    all: bool = Query(False),  # noqa: A002 - query parameter name is part of the contract
    limit: int = Query(50, ge=1, le=200),
    before: Optional[str] = Query(None),
    principal: Principal = Depends(get_principal),
):
    """Conversations newest first, for one connection or (``all=true``) every
    connection the user has used, including ones that no longer exist."""
    if not all and not connection:
        raise HTTPException(status_code=400, detail="Provide `connection` or `all=true`")
    history = get_history_service()
    rows = await history.list_conversations(
        user_id=principal.user_id,
        source_key=None if all else connection,
        limit=limit,
        before=_parse_list_cursor(before),
    )
    available = await _available_source_keys()
    items: List[ConversationSummary] = [_summary(r, available) for r in rows]
    next_cursor = None
    if len(rows) >= limit and rows:
        last = rows[-1]
        next_cursor = _encode_list_cursor(str(last["last_activity_at"]), str(last["id"]))
    return ConversationList(items=items, next_cursor=next_cursor)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    limit: Optional[int] = Query(None, ge=1, le=200),
    before: Optional[int] = Query(None, ge=1),
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    summary_row = await history.get_conversation(
        conversation_id=conversation_id, user_id=principal.user_id
    )
    if summary_row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    available = await _available_source_keys()
    return await _detail(
        history,
        summary_row=summary_row,
        user_id=principal.user_id,
        limit=_page_size(limit),
        before=before,
        available=available,
    )


@router.get(
    "/conversations/{conversation_id}/turns/{turn_id}/artifact",
    response_model=TurnArtifact,
)
async def get_turn_artifact(
    conversation_id: UUID,
    turn_id: UUID,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    artifact = await history.get_turn_artifact(
        conversation_id=conversation_id, turn_id=turn_id, user_id=principal.user_id
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Turn not found")
    return TurnArtifact(**artifact)


@router.post(
    "/conversations/{conversation_id}/turns/{turn_id}/rerun",
    response_model=RerunTurnResponse,
)
async def rerun_conversation_turn(
    conversation_id: UUID,
    turn_id: UUID,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    outcome = await rerun_turn(
        history,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id=principal.user_id,
    )
    return RerunTurnResponse(**outcome)


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: UUID,
    request: RenameConversationRequest,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    ok = await history.rename_conversation(
        conversation_id=conversation_id,
        user_id=principal.user_id,
        title=request.title,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: UUID,
    principal: Principal = Depends(get_principal),
):
    history = get_history_service()
    ok = await history.delete_conversation(
        conversation_id=conversation_id, user_id=principal.user_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True}
