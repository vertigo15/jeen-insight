"""Admin analytics: usage, users, connections, feedback and ML skills.

Every route is admin-only (``require_admin``) and reads exclusively from the
durable usage ledger ``insights_usage_events`` (migration 036) through
:class:`UsageAnalyticsRepository`, so the numbers survive conversation
retention. 503 until the migration is applied.

Metric definitions live on the response models in ``src/api/models.py`` and
in ``src/analytics/usage_ledger.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api import state
from src.api.dependencies import get_usage_analytics, require_admin
from src.api.models import (
    AnalyticsErrors,
    AnalyticsFeedbackFeed,
    AnalyticsOverview,
    AnalyticsSkills,
    AnalyticsTimeseries,
    AnalyticsTopConnections,
    AnalyticsTopUsers,
)
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/analytics",
    tags=["admin-analytics"],
    dependencies=[Depends(require_admin)],
)

Days = Literal[7, 30, 90]
DEFAULT_DAYS: Days = 30


def _days(days: Optional[int]) -> int:
    if days is None:
        return DEFAULT_DAYS
    if days not in (7, 30, 90):
        raise HTTPException(status_code=422, detail="days must be one of 7, 30, 90")
    return int(days)


async def _report(coro_factory, what: str):
    """Run one repository read; a timed-out or failing aggregate is a 503, not a 500."""
    try:
        return await coro_factory()
    except Exception:  # noqa: BLE001
        logger.exception("admin analytics: %s failed", what)
        raise HTTPException(status_code=503, detail=f"Analytics report '{what}' is unavailable right now")


@router.get("/overview", response_model=AnalyticsOverview)
async def overview(
    days: Optional[int] = Query(None, description="Window in days: 7, 30 or 90"),
    repo=Depends(get_usage_analytics),
):
    d = _days(days)
    data = await _report(lambda: repo.overview(d), "overview")
    return AnalyticsOverview(**data)


@router.get("/timeseries", response_model=AnalyticsTimeseries)
async def timeseries(
    days: Optional[int] = Query(None),
    repo=Depends(get_usage_analytics),
):
    d = _days(days)
    points = await _report(lambda: repo.timeseries(d), "timeseries")
    return AnalyticsTimeseries(days=d, points=points)


@router.get("/top-users", response_model=AnalyticsTopUsers)
async def top_users(
    days: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    repo=Depends(get_usage_analytics),
):
    d = _days(days)
    items = await _report(lambda: repo.top_users(d, limit), "top-users")
    return AnalyticsTopUsers(days=d, items=items)


@router.get("/top-connections", response_model=AnalyticsTopConnections)
async def top_connections(
    days: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    repo=Depends(get_usage_analytics),
):
    d = _days(days)
    items = await _report(lambda: repo.top_connections(d, limit), "top-connections")
    return AnalyticsTopConnections(days=d, items=items)


@router.get("/feedback", response_model=AnalyticsFeedbackFeed)
async def feedback_feed(
    days: Optional[int] = Query(None),
    thumb: Optional[Literal["thumbs_up", "thumbs_down", "cleared"]] = Query(None),
    type: Optional[Literal["general", "report_bug", "ui_bug", "other"]] = Query(None),  # noqa: A002
    connection: Optional[str] = Query(None, max_length=255),
    limit: int = Query(50, ge=1, le=200),
    before: Optional[int] = Query(None, ge=1, description="Keyset cursor: the id of the last row seen"),
    principal: Principal = Depends(require_admin),
    repo=Depends(get_usage_analytics),
):
    """Newest-first feed of feedback events (thumbs, ratings, comments).

    Reads of free-text feedback are audited (who, when, which filters — never
    the bodies) through the connector audit log.
    """
    d = _days(days)
    data = await _report(
        lambda: repo.feedback_feed(
            d, thumb=thumb, feedback_type=type, connection=connection, limit=limit, before_id=before,
        ),
        "feedback",
    )
    await _audit_read(
        principal,
        {"report": "feedback", "days": d, "thumb": thumb, "type": type,
         "connection": connection, "limit": limit, "before": before, "returned": len(data["items"])},
    )
    return AnalyticsFeedbackFeed(days=d, items=data["items"], next_before=data.get("next_before"))


@router.get("/analysis", response_model=AnalyticsSkills)
async def analysis_usage(
    days: Optional[int] = Query(None),
    repo=Depends(get_usage_analytics),
):
    d = _days(days)
    items = await _report(lambda: repo.analysis_usage(d), "analysis")
    return AnalyticsSkills(days=d, items=items)


@router.get("/errors", response_model=AnalyticsErrors)
async def errors(
    days: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=50),
    repo=Depends(get_usage_analytics),
):
    d = _days(days)
    data = await _report(lambda: repo.error_breakdown(d, limit), "errors")
    return AnalyticsErrors(days=d, **data)


async def _audit_read(principal: Principal, detail: Dict[str, Any]) -> None:
    audit = state.audit_service
    if audit is None:
        return
    try:
        await audit.log(
            event_type="analytics.read",
            actor_user_id=str(principal.user_id or "") or None,
            actor_email=getattr(principal, "email", None) or None,
            outcome="success",
            detail={k: v for k, v in detail.items() if v is not None},
        )
    except Exception:  # noqa: BLE001
        logger.debug("analytics read audit failed", exc_info=True)
