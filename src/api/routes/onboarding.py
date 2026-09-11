"""Per-user onboarding (FTUE) state: welcome dialog, guided tour, checklist, nudge.

Identity is taken from the verified internal-token :class:`Principal` (stamped by
the Flask proxy from the signed UI session), never from the request body/query —
``OnboardingPatch.user_id`` is advisory only. ``require_user_id`` still fails closed
on an empty / ``"default"`` principal so anonymous callers cannot touch user data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import (
    get_onboarding_service,
    get_principal,
    require_user_id,
)
from src.api.models import OnboardingPatch
from src.security.internal_auth import Principal

router = APIRouter(prefix="/api", tags=["onboarding"])


@router.get("/user/onboarding")
async def get_onboarding(principal: Principal = Depends(get_principal)):
    user_id = require_user_id(principal.user_id)
    service = get_onboarding_service()
    return await service.get_or_create(user_id)


@router.patch("/user/onboarding")
async def patch_onboarding(
    patch: OnboardingPatch,
    principal: Principal = Depends(get_principal),
):
    user_id = require_user_id(principal.user_id)
    service = get_onboarding_service()
    return await service.merge(
        user_id,
        welcome_seen=patch.welcome_seen,
        tour_completed=patch.tour_completed,
        checklist_dismissed=patch.checklist_dismissed,
        nudge_dismissed=patch.nudge_dismissed,
        checklist=patch.checklist,
    )
