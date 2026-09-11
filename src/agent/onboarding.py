"""Per-user onboarding (FTUE) state service for Jeen Insights.

Backed by a single table, `insights_user_onboarding` (one row per user_id):
  * welcome_seen_at        — welcome dialog shown once
  * tour_completed_at      — guided tour finished/skipped
  * checklist (jsonb)      — flat {item: bool} map for the getting-started card
  * checklist_dismissed_at — checklist card dismissed
  * nudge_dismissed_at     — post-first-answer nudge dismissed

The pool is shared with `MetadataLoader`/`ConnectionService`/`ConversationHistoryService`
(all point at METADATA_DB_*). Reads/writes are fail-soft: on any DB error (e.g. the
`021_user_onboarding` migration has not run yet) they return sensible defaults so the
workspace still boots.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import asyncpg

logger = logging.getLogger(__name__)


def _empty_state(user_id: str) -> Dict[str, Any]:
    """Default onboarding row shape for a brand-new / unavailable user."""
    return {
        "user_id": user_id,
        "welcome_seen_at": None,
        "tour_completed_at": None,
        "checklist": {},
        "checklist_dismissed_at": None,
        "nudge_dismissed_at": None,
        "updated_at": None,
    }


def _row_to_dict(row: asyncpg.Record) -> Dict[str, Any]:
    data = dict(row)
    checklist = data.get("checklist")
    # asyncpg returns jsonb as str unless a codec is registered; normalise to dict.
    if isinstance(checklist, str):
        try:
            data["checklist"] = json.loads(checklist)
        except (ValueError, TypeError):
            data["checklist"] = {}
    elif checklist is None:
        data["checklist"] = {}
    # Timestamps -> ISO strings so the JSON response is stable.
    for key in (
        "welcome_seen_at",
        "tour_completed_at",
        "checklist_dismissed_at",
        "nudge_dismissed_at",
        "updated_at",
    ):
        value = data.get(key)
        if value is not None and hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


class OnboardingService:
    """Reads/writes the `insights_user_onboarding` table (one row per user)."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_or_create(self, user_id: str) -> Dict[str, Any]:
        """Return the user's onboarding row, creating an empty one on first read."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO insights_user_onboarding (user_id)
                    VALUES ($1)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id,
                )
                row = await conn.fetchrow(
                    "SELECT * FROM insights_user_onboarding WHERE user_id = $1",
                    user_id,
                )
                return _row_to_dict(row) if row else _empty_state(user_id)
        except Exception:
            logger.exception("Failed to read/create onboarding state; returning defaults")
            return _empty_state(user_id)

    async def merge(
        self,
        user_id: str,
        *,
        welcome_seen: Optional[bool] = None,
        tour_completed: Optional[bool] = None,
        checklist_dismissed: Optional[bool] = None,
        nudge_dismissed: Optional[bool] = None,
        checklist: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Apply a partial update. Boolean flags stamp NOW() into their column
        (only when true); `checklist` is shallow-merged into the existing jsonb.
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO insights_user_onboarding (
                        user_id,
                        welcome_seen_at,
                        tour_completed_at,
                        checklist_dismissed_at,
                        nudge_dismissed_at,
                        checklist
                    )
                    VALUES (
                        $1,
                        CASE WHEN $2::bool THEN NOW() END,
                        CASE WHEN $3::bool THEN NOW() END,
                        CASE WHEN $4::bool THEN NOW() END,
                        CASE WHEN $5::bool THEN NOW() END,
                        COALESCE($6::jsonb, '{}'::jsonb)
                    )
                    ON CONFLICT (user_id) DO UPDATE SET
                        welcome_seen_at =
                            CASE WHEN $2::bool THEN NOW()
                                 ELSE insights_user_onboarding.welcome_seen_at END,
                        tour_completed_at =
                            CASE WHEN $3::bool THEN NOW()
                                 ELSE insights_user_onboarding.tour_completed_at END,
                        checklist_dismissed_at =
                            CASE WHEN $4::bool THEN NOW()
                                 ELSE insights_user_onboarding.checklist_dismissed_at END,
                        nudge_dismissed_at =
                            CASE WHEN $5::bool THEN NOW()
                                 ELSE insights_user_onboarding.nudge_dismissed_at END,
                        checklist =
                            insights_user_onboarding.checklist || COALESCE($6::jsonb, '{}'::jsonb),
                        updated_at = NOW()
                    RETURNING *
                    """,
                    user_id,
                    welcome_seen,
                    tour_completed,
                    checklist_dismissed,
                    nudge_dismissed,
                    json.dumps(checklist) if checklist is not None else None,
                )
                return _row_to_dict(row) if row else _empty_state(user_id)
        except Exception:
            logger.exception("Failed to merge onboarding state; returning defaults")
            return _empty_state(user_id)
