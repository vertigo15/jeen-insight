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


def _merge_params(explicit: Optional[Dict[str, Any]], legacy: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer the generic ``params`` object; fall back to the legacy email fields.

    The gate re-validates everything server-side via the typed action policy, so
    this is only about accepting either request shape from clients.
    """
    if explicit:
        return dict(explicit)
    return {k: v for k, v in legacy.items() if v is not None}


class ProposeRequest(BaseModel):
    connector_id: str
    action: str
    # Optional: only actions whose typed policy requires a snapshot need a handle.
    result_handle: Optional[str] = None
    # Generic, action-typed draft params (validated server-side at preview).
    params: Dict[str, Any] = {}


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
            params=body.params,
            origin="user",  # this public route is always user-initiated
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class PreviewRequest(BaseModel):
    nonce: str
    # Generic, action-typed params. Legacy email fields kept for back-compat.
    params: Optional[Dict[str, Any]] = None
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
    """Validate + persist the server-approved params and return the confirmation
    summary (no side effects). Execute later runs ONLY these approved params."""
    try:
        return await gate.preview(
            proposal_id=proposal_id,
            nonce=body.nonce,
            owner_user_id=principal.user_id,
            params=_merge_params(
                body.params,
                {"recipients": body.recipients, "subject": body.subject, "note": body.note},
            ),
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class ExecuteRequest(BaseModel):
    nonce: str
    confirmed: bool = False
    # Accepted for back-compat but ignored: execute runs only the params approved
    # at preview (stored server-side), never client-submitted params.
    params: Optional[Dict[str, Any]] = None
    recipients: List[str] = []
    subject: Optional[str] = None
    note: Optional[str] = None


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
            params={},  # ignored server-side; approved params come from preview
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class ContinueRequest(BaseModel):
    artifact_id: str
    question: str = ""
    session_id: Optional[str] = None


@router.post("/{proposal_id}/continue")
async def continue_read_action(
    proposal_id: str,
    body: ContinueRequest,
    principal: Principal = Depends(get_principal),
) -> Dict[str, Any]:
    """Response-only continuation for a read tool: load the encrypted artifact
    (single-consume, owner+session bound) and compose the final answer with tools
    DISABLED. The untrusted tool data can never authorize an action."""
    from src.agent.read_continuation import continue_read
    from src.api import state as api_state

    tool_results = api_state.tool_result_service
    llm = api_state.llm_service
    if tool_results is None or llm is None:
        raise HTTPException(status_code=503, detail="Read continuation is unavailable")
    try:
        return await continue_read(
            proposal_id=proposal_id,
            artifact_id=body.artifact_id,
            question=body.question,
            owner_user_id=principal.user_id,
            session_id=body.session_id,
            tool_results=tool_results,
            llm=llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
