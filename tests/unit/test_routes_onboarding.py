"""Tests for `src.api.routes.onboarding` (per-user FTUE state).

Identity is taken from the verified internal-token Principal, so these exercise
the real handler logic through the ``client`` fixture (Principal = ``user-a``)
and assert both fail-closed paths: the middleware-boundary 401 (``anon_client``)
and ``require_user_id`` rejecting a ``"default"`` principal.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fastapi.testclient import TestClient

from src.api import app
from src.api import state as api_state


@pytest.fixture
def onboarding_service(monkeypatch):
    """Inject an OnboardingService double with async get_or_create/merge."""
    svc = SimpleNamespace(
        get_or_create=AsyncMock(),
        merge=AsyncMock(),
    )
    monkeypatch.setattr(api_state, "onboarding_service", svc)
    return svc


def _empty_row(user_id: str = "user-a") -> dict:
    return {
        "user_id": user_id,
        "welcome_seen_at": None,
        "tour_completed_at": None,
        "checklist": {},
        "checklist_dismissed_at": None,
        "nudge_dismissed_at": None,
        "updated_at": None,
    }


def test_get_onboarding_creates_and_returns_row(client, onboarding_service):
    onboarding_service.get_or_create.return_value = _empty_row()

    resp = client.get("/api/user/onboarding")

    assert resp.status_code == 200
    assert resp.json() == _empty_row()
    onboarding_service.get_or_create.assert_awaited_once_with("user-a")


def test_patch_onboarding_merges_flags(client, onboarding_service):
    merged = _empty_row()
    merged["welcome_seen_at"] = "2026-09-11T00:00:00+00:00"
    merged["checklist"] = {"pick_connection": True}
    onboarding_service.merge.return_value = merged

    resp = client.patch(
        "/api/user/onboarding",
        json={
            "user_id": "user-a",
            "welcome_seen": True,
            "checklist": {"pick_connection": True},
        },
    )

    assert resp.status_code == 200
    assert resp.json() == merged
    onboarding_service.merge.assert_awaited_once_with(
        "user-a",
        welcome_seen=True,
        tour_completed=None,
        checklist_dismissed=None,
        nudge_dismissed=None,
        checklist={"pick_connection": True},
    )


def test_get_onboarding_requires_authenticated_user(anon_client, onboarding_service):
    # No internal token -> InternalAuthMiddleware default-denies before the handler.
    resp = anon_client.get("/api/user/onboarding")
    assert resp.status_code == 401
    onboarding_service.get_or_create.assert_not_awaited()


def test_get_onboarding_rejects_default_principal(make_internal_token, onboarding_service):
    # A valid token whose user_id is the sentinel "default" fails require_user_id.
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {make_internal_token(user_id='default')}"})

    resp = c.get("/api/user/onboarding")

    assert resp.status_code == 401
    onboarding_service.get_or_create.assert_not_awaited()


def test_patch_onboarding_missing_user_id_is_422(client, onboarding_service):
    # user_id is a required field on OnboardingPatch: omitting it is a schema
    # error (422), distinct from the 401 fail-closed path.
    resp = client.patch("/api/user/onboarding", json={"welcome_seen": True})
    assert resp.status_code == 422
    onboarding_service.merge.assert_not_awaited()
