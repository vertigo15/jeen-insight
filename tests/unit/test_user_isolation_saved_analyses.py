from __future__ import annotations

from unittest.mock import AsyncMock

from src.api.result_cache import ResultCache


def test_result_cache_requires_user_scope():
    cache = ResultCache(max_entries=10, ttl_seconds=60, per_user_max=5)
    dataset = {"columns": ["x"], "rows": [[1]]}

    cache.put(user_id=None, connection="sales", query_id="q1", dataset=dataset)
    assert cache.get(user_id=None, connection="sales", query_id="q1") is None

    cache.put(user_id="u1", connection="sales", query_id="q1", dataset=dataset)
    assert cache.get(user_id="u1", connection="sales", query_id="q1") == dataset
    assert cache.get(user_id="u2", connection="sales", query_id="q1") is None


def test_history_feedback_requires_user_id(client, fake_state):
    resp = client.post(
        "/api/feedback",
        json={"query_id": "11111111-1111-1111-1111-111111111111", "feedback": "thumbs_up"},
    )
    assert resp.status_code == 401


def test_history_feedback_scopes_update_to_user(client, fake_state):
    fake_state.history_service.record_feedback = AsyncMock(return_value=False)
    resp = client.post(
        "/api/feedback",
        json={
            "query_id": "11111111-1111-1111-1111-111111111111",
            "user_id": "user-a",
            "feedback": "thumbs_up",
        },
    )
    assert resp.status_code == 404
    fake_state.history_service.record_feedback.assert_awaited_once()
    assert fake_state.history_service.record_feedback.await_args.kwargs["user_id"] == "user-a"


def test_saved_analyses_list_requires_user(client, fake_state):
    resp = client.get("/api/saved-analyses?connection=sales")
    assert resp.status_code == 422  # missing required user_id query param


def test_saved_analyses_save_and_restore_are_user_scoped(client, fake_state):
    fake_state.history_service.save_analysis = AsyncMock(
        return_value="22222222-2222-2222-2222-222222222222"
    )
    payload = {
        "connection": "sales",
        "user_id": "user-a",
        "name": "December growth",
        "question": "which products grew in december",
        "sql": "select 1",
        "results": {"columns": ["x"], "rows": [[1]]},
        "chart_config": {"series": [{"type": "bar", "data": [1]}]},
        "insights": {"summary": "ok", "findings": [], "suggestions": []},
    }
    resp = client.post("/api/saved-analyses", json=payload)
    assert resp.status_code == 200
    fake_state.history_service.save_analysis.assert_awaited_once()
    assert fake_state.history_service.save_analysis.await_args.kwargs["user_id"] == "user-a"

    fake_state.history_service.get_saved_analysis = AsyncMock(return_value=None)
    missing = client.get(
        "/api/saved-analyses/22222222-2222-2222-2222-222222222222?user_id=user-b"
    )
    assert missing.status_code == 404
    fake_state.history_service.get_saved_analysis.assert_awaited_once()
    assert fake_state.history_service.get_saved_analysis.await_args.kwargs["user_id"] == "user-b"


def test_chart_generation_rejects_foreign_query_id(client, fake_state):
    fake_state.history_service.query_belongs_to_user = AsyncMock(return_value=False)
    resp = client.post(
        "/api/generate-chart",
        json={
            "connection": "sales",
            "user_id": "user-a",
            "query_id": "11111111-1111-1111-1111-111111111111",
            "question": "show sales",
            "chart_type": "auto",
        },
    )
    assert resp.status_code == 404
    fake_state.history_service.query_belongs_to_user.assert_awaited_once()


def test_insights_rejects_foreign_query_id(client, fake_state):
    fake_state.history_service.query_belongs_to_user = AsyncMock(return_value=False)
    resp = client.post(
        "/api/generate-insights",
        json={
            "connection": "sales",
            "user_id": "user-a",
            "query_id": "11111111-1111-1111-1111-111111111111",
            "question": "show sales",
            "dataset": {"columns": ["x"], "rows": [[1]]},
        },
    )
    assert resp.status_code == 404
    fake_state.history_service.query_belongs_to_user.assert_awaited_once()
