"""/api/admin/analytics/*: admin-only, 503 before migration 036, parameter
validation, response shaping and the audited feedback read. Offline: the
repository is an AsyncMock installed on ``src.api.state``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.api import state as api_state

QID = "11111111-1111-1111-1111-111111111111"
OVERVIEW = {
    "days": 30, "start": "2026-08-26T00:00:00+00:00", "end": "2026-09-25T00:00:00+00:00",
    "dau": 2, "wau": 5, "mau": 9,
    "current": {"questions": 40, "active_users": 5, "successes": 36, "errors": 4, "success_rate": 0.9,
                "thumbs_up": 6, "thumbs_down": 1, "total_tokens": 12345},
    "previous": {"questions": 30, "active_users": 4},
}


@pytest.fixture
def repo(monkeypatch):
    fake = MagicMock(name="UsageAnalyticsRepository")
    fake.overview = AsyncMock(return_value=OVERVIEW)
    fake.timeseries = AsyncMock(return_value=[{"day": "2026-09-25", "active_users": 1, "questions": 2}])
    fake.top_users = AsyncMock(return_value=[{"user_id": "7", "name": "Dana", "questions": 12}])
    fake.top_connections = AsyncMock(return_value=[{"source_key": "sales_db", "questions": 20}])
    fake.feedback_feed = AsyncMock(return_value={"items": [
        {"id": 42, "user_id": "7", "thumb": "thumbs_down", "message": "wrong", "question": "q"},
    ], "next_before": 42})
    fake.analysis_usage = AsyncMock(return_value=[{"skill": "forecast", "runs": 3}])
    fake.error_breakdown = AsyncMock(return_value={
        "by_type": [{"error_type": "timeout", "source_key": "s", "failures": 2}],
        "top_failing_questions": [{"question": "q", "failures": 2}],
    })
    fake.execution_runs = AsyncMock(return_value={"items": [{
        "id": 42, "query_id": QID, "user_id": "7", "source_key": "sales_db",
        "outcome": "success", "question": "Revenue?", "query_language": "sql",
        "total_tokens": 12, "detail_available": True,
    }], "next_before": 42})
    fake.execution_detail = AsyncMock(return_value={
        "id": 42, "query_id": QID, "user_id": "7", "source_key": "sales_db",
        "outcome": "success", "question": "Revenue?", "answer": "42",
        "query_language": "sql", "generated_query": "SELECT 42",
        "detail_available": True,
        "metrics": {"total_tokens": 12},
        "node_trace": [{"node": "sql_generator", "elapsed_ms": 5, "type": "llm"}],
    })
    monkeypatch.setattr(api_state, "usage_analytics", fake)
    monkeypatch.setattr(api_state, "audit_service", None)
    return fake


def _viewer(make_internal_token) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {make_internal_token(role='viewer')}"})
    return c


# ── access control ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "overview", "timeseries", "top-users", "top-connections", "feedback", "analysis", "errors", "runs",
])
def test_every_report_requires_authentication(anon_client, repo, path):
    assert anon_client.get(f"/api/admin/analytics/{path}").status_code == 401


@pytest.mark.parametrize("path", [
    "overview", "timeseries", "top-users", "top-connections", "feedback", "analysis", "errors", "runs",
])
def test_every_report_is_admin_only(make_internal_token, repo, path):
    assert _viewer(make_internal_token).get(f"/api/admin/analytics/{path}").status_code == 403
    repo.overview.assert_not_awaited()
    repo.feedback_feed.assert_not_awaited()


def test_editor_is_not_admin(make_internal_token, repo):
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {make_internal_token(role='editor')}"})
    assert c.get("/api/admin/analytics/overview").status_code == 403


def test_503_before_migration_036(client, monkeypatch):
    monkeypatch.setattr(api_state, "usage_analytics", None)
    resp = client.get("/api/admin/analytics/overview")
    assert resp.status_code == 503
    assert "036" in resp.json()["detail"]


# ── parameters ───────────────────────────────────────────────────────────

def test_days_defaults_to_30_and_accepts_only_7_30_90(client, repo):
    assert client.get("/api/admin/analytics/overview").status_code == 200
    assert repo.overview.await_args.args == (30,)
    assert client.get("/api/admin/analytics/overview?days=7").status_code == 200
    assert repo.overview.await_args.args == (7,)
    assert client.get("/api/admin/analytics/overview?days=31").status_code == 422
    assert client.get("/api/admin/analytics/overview?days=abc").status_code == 422


def test_limits_are_bounded(client, repo):
    assert client.get("/api/admin/analytics/top-users?limit=0").status_code == 422
    assert client.get("/api/admin/analytics/top-users?limit=101").status_code == 422
    assert client.get("/api/admin/analytics/feedback?limit=201").status_code == 422
    assert client.get("/api/admin/analytics/errors?limit=51").status_code == 422
    assert client.get("/api/admin/analytics/top-users?limit=100").status_code == 200
    assert repo.top_users.await_args.args == (30, 100)


def test_feedback_filters_are_validated_and_forwarded(client, repo):
    assert client.get("/api/admin/analytics/feedback?thumb=meh").status_code == 422
    assert client.get("/api/admin/analytics/feedback?type=praise").status_code == 422
    resp = client.get("/api/admin/analytics/feedback?days=90&thumb=thumbs_down&type=report_bug&connection=sales_db&limit=10&before=99")
    assert resp.status_code == 200
    assert repo.feedback_feed.await_args.args == (90,)
    assert repo.feedback_feed.await_args.kwargs == {
        "thumb": "thumbs_down", "feedback_type": "report_bug", "connection": "sales_db", "limit": 10, "before_id": 99,
    }
    body = resp.json()
    assert body["days"] == 90 and body["next_before"] == 42 and body["items"][0]["message"] == "wrong"


def test_run_filters_cursor_and_limit_are_validated_and_forwarded(client, repo):
    assert client.get("/api/admin/analytics/runs?outcome=unknown").status_code == 422
    assert client.get("/api/admin/analytics/runs?limit=201").status_code == 422
    assert client.get("/api/admin/analytics/runs?before=0").status_code == 422
    resp = client.get(
        "/api/admin/analytics/runs?days=7&outcome=success&connection=sales_db&limit=10&before=99"
    )
    assert resp.status_code == 200
    assert repo.execution_runs.await_args.args == (7,)
    assert repo.execution_runs.await_args.kwargs == {
        "outcome": "success", "connection": "sales_db", "limit": 10, "before_id": 99,
    }
    assert resp.json()["next_before"] == 42


def test_run_detail_is_admin_only_and_returns_404_when_missing(
    make_internal_token, anon_client, client, repo,
):
    assert anon_client.get(f"/api/admin/analytics/runs/{QID}").status_code == 401
    assert _viewer(make_internal_token).get(f"/api/admin/analytics/runs/{QID}").status_code == 403
    body = client.get(f"/api/admin/analytics/runs/{QID}").json()
    assert body["answer"] == "42" and body["generated_query"] == "SELECT 42"
    repo.execution_detail = AsyncMock(return_value=None)
    assert client.get(f"/api/admin/analytics/runs/{QID}").status_code == 404


# ── shaping ──────────────────────────────────────────────────────────────

def test_overview_shape(client, repo):
    body = client.get("/api/admin/analytics/overview").json()
    assert body["dau"] == 2 and body["current"]["success_rate"] == 0.9
    # Missing fields default to zero rather than leaking as absent keys.
    assert body["previous"]["thumbs_up"] == 0 and body["current"]["refused"] == 0


def test_list_reports_wrap_items_with_the_window(client, repo):
    assert client.get("/api/admin/analytics/timeseries?days=7").json() == {
        "days": 7, "points": [{"day": "2026-09-25", "active_users": 1, "questions": 2, "errors": 0,
                               "thumbs_up": 0, "thumbs_down": 0, "logins": 0}],
    }
    users = client.get("/api/admin/analytics/top-users").json()
    assert users["days"] == 30 and users["items"][0]["name"] == "Dana"
    conns = client.get("/api/admin/analytics/top-connections").json()
    assert conns["items"][0]["source_key"] == "sales_db"
    skills = client.get("/api/admin/analytics/analysis").json()
    assert skills["items"][0] == {"skill": "forecast", "runs": 3, "ok": 0, "guard_failed": 0, "errors": 0,
                                  "avg_execution_ms": None, "distinct_users": 0, "thumbs_up": 0, "thumbs_down": 0}
    errors = client.get("/api/admin/analytics/errors").json()
    assert errors["by_type"][0]["error_type"] == "timeout" and errors["top_failing_questions"][0]["failures"] == 2


def test_repository_failure_is_a_503_not_a_500(client, repo):
    repo.timeseries = AsyncMock(side_effect=RuntimeError("statement timeout"))
    resp = client.get("/api/admin/analytics/timeseries")
    assert resp.status_code == 503
    assert "timeseries" in resp.json()["detail"]


# ── audit ────────────────────────────────────────────────────────────────

def test_feedback_reads_are_audited_without_bodies(client, repo, monkeypatch):
    audit = MagicMock()
    audit.log = AsyncMock()
    monkeypatch.setattr(api_state, "audit_service", audit)
    client.get("/api/admin/analytics/feedback?thumb=thumbs_down&connection=sales_db")
    audit.log.assert_awaited_once()
    kwargs = audit.log.await_args.kwargs
    assert kwargs["event_type"] == "analytics.read"
    assert kwargs["actor_user_id"] == "user-a" and kwargs["outcome"] == "success"
    assert kwargs["detail"]["report"] == "feedback" and kwargs["detail"]["thumb"] == "thumbs_down"
    assert kwargs["detail"]["returned"] == 1
    assert "wrong" not in repr(kwargs)                             # never the message bodies
    # Other reports are not audited (no free text involved).
    client.get("/api/admin/analytics/overview")
    audit.log.assert_awaited_once()


def test_execution_reads_are_audited_without_question_answer_or_query(client, repo, monkeypatch):
    audit = MagicMock()
    audit.log = AsyncMock()
    monkeypatch.setattr(api_state, "audit_service", audit)

    assert client.get("/api/admin/analytics/runs?outcome=success").status_code == 200
    assert client.get(f"/api/admin/analytics/runs/{QID}").status_code == 200
    assert audit.log.await_count == 2
    list_detail = audit.log.await_args_list[0].kwargs["detail"]
    item_detail = audit.log.await_args_list[1].kwargs["detail"]
    assert list_detail["report"] == "execution_runs" and list_detail["returned"] == 1
    assert item_detail == {"report": "execution_detail", "query_id": QID, "returned": 1}
    rendered = repr(audit.log.await_args_list)
    assert "Revenue?" not in rendered and "SELECT 42" not in rendered and "'42'" not in rendered
