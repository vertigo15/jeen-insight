"""Server-authorized action endpoints (propose + execute).

The model/UI proposes a named action against an opaque result handle (a snapshot
id). The server collects recipients, re-validates every policy, renders the
payload from the server-held snapshot, and executes exactly once.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.dependencies import (
    ensure_identity,
    get_action_gate,
    get_principal,
    require_connectors_enabled,
)
from src.connectors.action_gate import ActionError
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/actions",
    tags=["connectors-actions"],
    dependencies=[Depends(require_connectors_enabled)],
)


class ProposeRequest(BaseModel):
    connector_id: str
    action: str
    result_handle: str  # snapshot id


@router.post("/propose")
async def propose(
    body: ProposeRequest,
    principal: Principal = Depends(get_principal),
    gate=Depends(get_action_gate),
) -> Dict[str, Any]:
    identity = await ensure_identity(principal)
    try:
        return await gate.propose(
            owner_user_id=principal.user_id,
            identity_id=identity["id"],
            connector_id=body.connector_id,
            action=body.action,
            snapshot_id=body.result_handle,
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class PreviewRequest(BaseModel):
    nonce: str
    recipients: List[str] = []
    subject: Optional[str] = None
    note: Optional[str] = None


@router.post("/{proposal_id}/preview")
async def preview(
    proposal_id: str,
    body: PreviewRequest,
    principal: Principal = Depends(get_principal),
    gate=Depends(get_action_gate),
) -> Dict[str, Any]:
    """Return the server-derived confirmation summary (no side effects)."""
    try:
        return await gate.preview(
            proposal_id=proposal_id,
            nonce=body.nonce,
            owner_user_id=principal.user_id,
            params={
                "recipients": body.recipients,
                "subject": body.subject,
                "note": body.note,
            },
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class ExecuteRequest(BaseModel):
    nonce: str
    recipients: List[str] = []
    subject: Optional[str] = None
    note: Optional[str] = None
    confirmed: bool = False


@router.post("/{proposal_id}/execute")
async def execute(
    proposal_id: str,
    body: ExecuteRequest,
    principal: Principal = Depends(get_principal),
    gate=Depends(get_action_gate),
) -> Dict[str, Any]:
    # Require the explicit confirm step (client must POST /preview, show the
    # server-derived summary + external-recipient warning, then confirm).
    if not body.confirmed:
        raise HTTPException(
            status_code=428,
            detail="Confirmation required: preview the action and confirm before sending.",
        )
    try:
        return await gate.execute(
            proposal_id=proposal_id,
            nonce=body.nonce,
            owner_user_id=principal.user_id,
            actor_email=principal.email,
            params={
                "recipients": body.recipients,
                "subject": body.subject,
                "note": body.note,
            },
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
