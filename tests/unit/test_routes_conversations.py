"""Conversation restore/browse routes: identity, ownership, hydration shape,
on-open prune trigger, rerun dispatch and chart persistence guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api import background, conversation_retention
from src.api import state as api_state

CONV_ID = "11111111-1111-1111-1111-111111111111"
TURN_ID = "22222222-2222-2222-2222-222222222222"


def _summary(**overrides):
    base = {
        "id": CONV_ID,
        "title": "top customers",
        "source_key": "sales_db",
        "source_label": "Sales",
        "turn_count": 2,
        "saved_answer_count": 1,
        "last_question": "and by region?",
        "created_at": "2026-09-01T10:00:00+00:00",
        "last_activity_at": "2026-09-01T10:05:00+00:00",
    }
    base.update(overrides)
    return base


def _turn(**overrides):
    base = {
        "turn_id": TURN_ID,
        "sequence_number": 2,
        "question": "and by region?",
        "sql": "select region, sum(x) from t group by 1",
        "execution_status": "success",
        "result_kind": "table",
        "answer": None,
        "error": None,
        "metrics": {"execution_time_ms": 12},
        "findings": ["f"],
        "suggestions": None,
        "followups": None,
        "snapshot_status": "stored",
        "row_count": 3,
        "has_chart": True,
        "has_rerunnable_query": True,
        "created_at": "2026-09-01T10:05:00+00:00",
        "snapshot_at": "2026-09-01T10:05:01+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_retention():
    conversation_retention.reset_for_tests()
    background.reset_for_tests()
    yield
    conversation_retention.reset_for_tests()
    background.reset_for_tests()


def _history_with(fake_state, **methods):
    h = fake_state.history_service
    h.persistence_enabled = True
    for name, value in methods.items():
        setattr(h, name, value)
    fake_state.connection_service.list_connections = AsyncMock(
        return_value=[SimpleNamespace(source_key="sales_db")]
    )
    return h


# ── identity / ownership ────────────────────────────────────────────────────

def test_last_requires_authenticated_principal(anon_client, fake_state):
    resp = anon_client.get("/api/conversations/last?connection=sales_db")
    assert resp.status_code == 401


def test_last_returns_null_when_user_has_no_conversation(client, fake_state, monkeypatch):
    h = _history_with(fake_state, get_last_conversation=AsyncMock(return_value=None))
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", False)
    resp = client.get("/api/conversations/last?connection=sales_db")
    assert resp.status_code == 200
    assert resp.json() is None
    kwargs = h.get_last_conversation.await_args.kwargs
    assert kwargs == {"user_id": "user-a", "source_key": "sales_db"}


def test_last_returns_hydration_payload_without_rows(client, fake_state, monkeypatch):
    h = _history_with(
        fake_state,
        get_last_conversation=AsyncMock(return_value=_summary()),
        get_conversation_turns=AsyncMock(return_value=[_turn(), _turn(sequence_number=1, turn_id="33333333-3333-3333-3333-333333333333", question="top customers")]),
    )
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", False)
    resp = client.get("/api/conversations/last?connection=sales_db&limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation"]["id"] == CONV_ID
    assert body["conversation"]["connection_available"] is True
    assert body["conversation"]["saved_answer_count"] == 1
    assert [t["sequence_number"] for t in body["turns"]] == [2, 1]
    # Metadata-first: no rows, no chart config on the hydration payload.
    assert all("results" not in t and "chart_config" not in t for t in body["turns"])
    assert body["turns"][0]["execution_status"] == "success"
    assert body["next_cursor"] == 1  # page was full -> cursor to older turns
    kwargs = h.get_conversation_turns.await_args.kwargs
    assert kwargs["user_id"] == "user-a" and kwargs["limit"] == 2


def test_get_conversation_404_for_foreign_or_missing(client, fake_state):
    _history_with(fake_state, get_conversation=AsyncMock(return_value=None))
    resp = client.get(f"/api/conversations/{CONV_ID}")
    assert resp.status_code == 404


def test_connection_available_false_when_connection_is_gone(client, fake_state):
    h = _history_with(
        fake_state,
        get_conversation=AsyncMock(return_value=_summary(source_key="old_db")),
        get_conversation_turns=AsyncMock(return_value=[]),
    )
    resp = client.get(f"/api/conversations/{CONV_ID}")
    assert resp.status_code == 200
    assert resp.json()["conversation"]["connection_available"] is False
    assert resp.json()["next_cursor"] is None
    h.get_conversation.assert_awaited_once()


def test_artifact_scoped_to_principal_and_conversation(client, fake_state):
    h = _history_with(
        fake_state,
        get_turn_artifact=AsyncMock(
            return_value={
                "turn_id": TURN_ID,
                "results": {"columns": ["a"], "rows": [[1]], "row_count": 1},
                "chart_spec": {"chart_type": "bar"},
                "chart_config": {"series": []},
                "snapshot_status": "stored",
                "snapshot_at": None,
            }
        ),
    )
    resp = client.get(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/artifact")
    assert resp.status_code == 200
    assert resp.json()["results"]["rows"] == [[1]]
    kwargs = h.get_turn_artifact.await_args.kwargs
    assert kwargs["user_id"] == "user-a"
    assert str(kwargs["conversation_id"]) == CONV_ID
    assert str(kwargs["turn_id"]) == TURN_ID


def test_single_turn_metadata_is_owner_scoped(client, fake_state):
    h = _history_with(fake_state, get_conversation_turn=AsyncMock(return_value=_turn(is_favorite=True)))
    resp = client.get(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}")
    assert resp.status_code == 200
    assert resp.json()["turn_id"] == TURN_ID
    assert resp.json()["is_favorite"] is True
    assert h.get_conversation_turn.await_args.kwargs["user_id"] == "user-a"

    h.get_conversation_turn = AsyncMock(return_value=None)
    assert client.get(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}").status_code == 404


def test_list_requires_connection_or_all(client, fake_state):
    _history_with(fake_state, list_conversations=AsyncMock(return_value=[]))
    assert client.get("/api/conversations").status_code == 400
    ok = client.get("/api/conversations?all=true")
    assert ok.status_code == 200
    assert ok.json() == {"items": [], "next_cursor": None}
    kwargs = fake_state.history_service.list_conversations.await_args.kwargs
    assert kwargs["source_key"] is None and kwargs["user_id"] == "user-a"


def test_list_rejects_bad_cursor(client, fake_state):
    _history_with(fake_state, list_conversations=AsyncMock(return_value=[]))
    resp = client.get("/api/conversations?connection=sales_db&before=garbage")
    assert resp.status_code == 400


def test_rename_and_delete_are_user_scoped(client, fake_state):
    h = _history_with(
        fake_state,
        rename_conversation=AsyncMock(return_value=False),
        delete_conversation=AsyncMock(return_value={"status": "missing", "saved_answer_count": 0}),
    )
    assert client.patch(f"/api/conversations/{CONV_ID}", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/conversations/{CONV_ID}").status_code == 404
    assert h.rename_conversation.await_args.kwargs["user_id"] == "user-a"
    assert h.delete_conversation.await_args.kwargs["user_id"] == "user-a"
    assert h.delete_conversation.await_args.kwargs["delete_saved"] is False
    assert client.patch(f"/api/conversations/{CONV_ID}", json={"title": ""}).status_code == 422


def test_delete_requires_explicit_confirmation_for_saved_answers(client, fake_state):
    h = _history_with(
        fake_state,
        delete_conversation=AsyncMock(side_effect=[
            {"status": "blocked", "saved_answer_count": 2},
            {"status": "deleted", "saved_answer_count": 2},
        ]),
    )
    blocked = client.delete(f"/api/conversations/{CONV_ID}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "conversation_has_saved_answers",
        "saved_answer_count": 2,
    }

    deleted = client.delete(f"/api/conversations/{CONV_ID}?delete_saved=true")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_saved_answer_count"] == 2
    assert h.delete_conversation.await_args.kwargs["delete_saved"] is True


def test_favorite_answers_are_user_scoped_and_reopenable(client, fake_state):
    h = _history_with(
        fake_state,
        list_favorite_answers=AsyncMock(return_value=[{
            "conversation_id": CONV_ID,
            "turn_id": TURN_ID,
            "sequence_number": 2,
            "conversation_title": "top customers",
            "question": "and by region?",
            "answer": "North led",
            "result_kind": "table",
            "snapshot_status": "stored",
            "source_key": "sales_db",
            "source_label": "Sales",
            "created_at": "2026-09-01T10:05:00+00:00",
            "favorited_at": "2026-09-01T10:06:00+00:00",
        }]),
        set_answer_favorite=AsyncMock(side_effect=[True, False]),
    )
    h.favorite_schema_ready = True

    listed = client.get("/api/conversations/favorites?limit=25")
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["turn_id"] == TURN_ID and item["connection_available"] is True
    assert h.list_favorite_answers.await_args.kwargs == {
        "user_id": "user-a", "source_key": None, "limit": 25, "before": None,
    }
    page_one = client.get("/api/conversations/favorites?limit=1")
    cursor = page_one.json()["next_cursor"]
    assert cursor
    page_two = client.get(f"/api/conversations/favorites?limit=1&before={cursor}")
    assert page_two.status_code == 200
    assert h.list_favorite_answers.await_args.kwargs["before"] is not None
    assert client.get("/api/conversations/favorites?before=garbage").status_code == 400

    added = client.put(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/favorite")
    removed = client.delete(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/favorite")
    assert added.json()["is_favorite"] is True
    assert removed.json()["is_favorite"] is False
    assert h.set_answer_favorite.await_args_list[0].kwargs["user_id"] == "user-a"


def test_favorite_routes_fail_closed_when_schema_or_turn_is_missing(client, fake_state):
    h = _history_with(fake_state)
    h.favorite_schema_ready = False
    assert client.get("/api/conversations/favorites").status_code == 503
    assert client.put(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/favorite").status_code == 503

    h.favorite_schema_ready = True
    h.set_answer_favorite = AsyncMock(side_effect=[None, False])
    assert client.put(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/favorite").status_code == 404
    removed = client.delete(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/favorite")
    assert removed.status_code == 200
    assert removed.json()["is_favorite"] is False


# ── on-open prune trigger ───────────────────────────────────────────────────

def test_last_spawns_prune_with_protected_id_and_returns_first(client, fake_state, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    seen = {}

    async def slow_prune(**kwargs):
        seen.update(kwargs)
        started.set()
        await release.wait()
        return {"deleted_conversations": 0, "pruned_turns": 0, "duration_ms": 1}

    h = _history_with(
        fake_state,
        get_last_conversation=AsyncMock(return_value=_summary()),
        get_conversation_turns=AsyncMock(return_value=[]),
        prune_user_conversations=AsyncMock(side_effect=slow_prune),
    )
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", True)
    resp = client.get("/api/conversations/last?connection=sales_db")
    # The response returned while the prune coroutine is still blocked.
    assert resp.status_code == 200
    assert resp.json()["conversation"]["id"] == CONV_ID
    h.prune_user_conversations.assert_called_once()
    assert str(seen.get("protect_conversation_id")) == CONV_ID
    assert seen.get("user_id") == "user-a" and seen.get("source_key") == "sales_db"
    release.set()


def test_last_does_not_spawn_prune_when_disabled(client, fake_state, monkeypatch):
    h = _history_with(
        fake_state,
        get_last_conversation=AsyncMock(return_value=None),
        prune_user_conversations=AsyncMock(),
    )
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", False)
    client.get("/api/conversations/last?connection=sales_db")
    h.prune_user_conversations.assert_not_called()

    h.persistence_enabled = False
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", True)
    client.get("/api/conversations/last?connection=sales_db")
    h.prune_user_conversations.assert_not_called()


def test_prune_trigger_debounces_and_skips_when_saturated(monkeypatch):
    history = MagicMock()
    history.persistence_enabled = True
    history.prune_user_conversations = AsyncMock(return_value={})
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_ON_OPEN", True)
    monkeypatch.setattr(conversation_retention.settings, "CONVERSATION_RETENTION_MIN_INTERVAL_SECONDS", 600)

    async def run():
        first = conversation_retention.maybe_schedule_prune(
            history, user_id="u", source_key="s", protect_conversation_id=None
        )
        second = conversation_retention.maybe_schedule_prune(
            history, user_id="u", source_key="s", protect_conversation_id=None
        )
        await asyncio.sleep(0)
        await background.shutdown()
        return first, second

    first, second = asyncio.run(run())
    assert first is None  # spawned
    assert second == "debounced"

    # Saturation: a burst of three distinct users in ONE event-loop turn must
    # spawn two prunes and skip (not queue) the third. The slot is reserved
    # synchronously, so no await can slip in between check and reservation.
    conversation_retention.reset_for_tests()
    background.reset_for_tests()
    release = asyncio.Event()

    async def slow(**_kwargs):
        await release.wait()
        return {}

    history.prune_user_conversations = AsyncMock(side_effect=slow)

    async def burst():
        outcomes = [
            conversation_retention.maybe_schedule_prune(
                history, user_id=f"u{i}", source_key="s", protect_conversation_id=None
            )
            for i in range(3)
        ]
        in_flight_during = conversation_retention.in_flight()
        await asyncio.sleep(0)
        release.set()
        await background.shutdown()
        return outcomes, in_flight_during, conversation_retention.in_flight()

    outcomes, during, after = asyncio.run(burst())
    assert outcomes == [None, None, "saturated"]
    assert during == 2
    assert after == 0, "slots are released when the prune finishes"
    assert history.prune_user_conversations.call_count == 2


def test_background_shutdown_cancels_and_awaits_tasks():
    async def run():
        background.reset_for_tests()
        cancelled = asyncio.Event()

        async def forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = background.spawn(forever(), name="t")
        assert task is not None and background.pending_count() == 1
        await asyncio.sleep(0)  # let the task reach its first await
        await background.shutdown(timeout=2)
        assert cancelled.is_set()
        assert background.pending_count() == 0
        # After shutdown nothing new is accepted.
        assert background.spawn(asyncio.sleep(0), name="late") is None

    asyncio.run(run())


# ── rerun dispatch ──────────────────────────────────────────────────────────

def test_rerun_404_when_turn_not_owned(client, fake_state):
    _history_with(fake_state, get_turn_for_rerun=AsyncMock(return_value=None))
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 404


def test_rerun_409_when_connection_inactive(client, fake_state):
    _history_with(
        fake_state,
        get_turn_for_rerun=AsyncMock(
            return_value={"turn_id": TURN_ID, "sql": "select 1", "question": "q", "source_key": "sales_db", "source_label": "Sales"}
        ),
    )
    fake_state.connection_service.get_connection = AsyncMock(
        return_value=SimpleNamespace(is_power_bi=False, is_active=False, source_key="sales_db")
    )
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 409
    assert "connection_unavailable" in resp.text


def test_rerun_sql_path_promotes_snapshot_and_clears_chart(client, fake_state, monkeypatch):
    h = _history_with(
        fake_state,
        get_turn_for_rerun=AsyncMock(
            return_value={"turn_id": TURN_ID, "sql": "select 1", "question": "q", "source_key": "sales_db", "source_label": "Sales"}
        ),
        store_rerun_snapshot=AsyncMock(return_value=True),
    )
    runner = MagicMock()
    runner.run_sql = AsyncMock(return_value={"columns": ["a"], "rows": [{"a": 1}], "row_count": 1})
    fake_state.connection_service.get_connection = AsyncMock(
        return_value=SimpleNamespace(is_power_bi=False, is_active=True, source_key="sales_db")
    )
    fake_state.connection_service.get_runner = AsyncMock(return_value=runner)

    from src.api import conversation_rerun

    async def fake_runtime():
        return SimpleNamespace(max_result_rows=500, db_statement_timeout_ms=1000)

    import src.metadata.runtime_settings as rs
    monkeypatch.setattr(rs, "get_runtime_settings", fake_runtime)

    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"]["rows"] == [{"a": 1}]
    assert body["snapshot_status"] == "stored"
    assert body["chart_config"] is None and body["chart_spec"] is None
    runner.run_sql.assert_awaited_once()
    assert runner.run_sql.await_args.kwargs["max_rows"] == 500
    stored = h.store_rerun_snapshot.await_args.kwargs
    assert stored["snapshot_status"] == "stored" and stored["user_id"] == "user-a"
    assert conversation_rerun is not None


def test_rerun_maps_upstream_failures_to_safe_502_and_504(client, fake_state, monkeypatch):
    h = _history_with(
        fake_state,
        get_turn_for_rerun=AsyncMock(
            return_value={"turn_id": TURN_ID, "sql": "select 1", "question": "q", "source_key": "sales_db", "source_label": "Sales"}
        ),
        store_rerun_snapshot=AsyncMock(return_value=True),
    )
    fake_state.connection_service.get_connection = AsyncMock(
        return_value=SimpleNamespace(is_power_bi=False, is_active=True, source_key="sales_db")
    )
    import src.metadata.runtime_settings as rs

    async def fake_runtime():
        return SimpleNamespace(max_result_rows=500, db_statement_timeout_ms=1000)

    monkeypatch.setattr(rs, "get_runtime_settings", fake_runtime)

    # Runner cannot be built (driver/config problem): generic 502, no internals leaked.
    fake_state.connection_service.get_runner = AsyncMock(
        side_effect=RuntimeError("psycopg: password authentication failed for host 10.0.0.7")
    )
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 502
    assert "10.0.0.7" not in resp.text and "password" not in resp.text

    # Runner raises mid-execution: also a generic 502.
    runner = MagicMock()
    runner.run_sql = AsyncMock(side_effect=OSError("socket closed by 10.0.0.7"))
    fake_state.connection_service.get_runner = AsyncMock(return_value=runner)
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 502
    assert "10.0.0.7" not in resp.text

    # Sanitised timeout result from the protected runner: 504.
    runner.run_sql = AsyncMock(
        return_value={"error": "The query took too long", "error_type": "timeout", "columns": [], "rows": [], "row_count": 0}
    )
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 504
    h.store_rerun_snapshot.assert_not_called()

    # Infrastructure failures before execution (connection lookup, runtime
    # settings) are also mapped to a generic 502, never a 500.
    fake_state.connection_service.get_connection = AsyncMock(
        side_effect=RuntimeError("metadata db unreachable at 10.0.0.9")
    )
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 502 and "10.0.0.9" not in resp.text

    fake_state.connection_service.get_connection = AsyncMock(
        return_value=SimpleNamespace(is_power_bi=False, is_active=True, source_key="sales_db")
    )

    async def broken_runtime():
        raise RuntimeError("app_settings unavailable")

    monkeypatch.setattr(rs, "get_runtime_settings", broken_runtime)
    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 502 and "app_settings" not in resp.text


def test_rerun_dax_path_requires_grant(client, fake_state, monkeypatch):
    _history_with(
        fake_state,
        get_turn_for_rerun=AsyncMock(
            return_value={"turn_id": TURN_ID, "sql": "EVALUATE T", "question": "q", "source_key": "pbi", "source_label": "PBI"}
        ),
    )
    fake_state.connection_service.get_connection = AsyncMock(
        return_value=SimpleNamespace(
            is_power_bi=True, is_active=True, source_key="pbi",
            workspace_id="11111111-1111-1111-1111-111111111111",
            dataset_id="22222222-2222-2222-2222-222222222222",
        )
    )
    import src.metadata.runtime_settings as rs

    async def fake_runtime():
        return SimpleNamespace(max_result_rows=500, db_statement_timeout_ms=1000)

    monkeypatch.setattr(rs, "get_runtime_settings", fake_runtime)

    from src.connectors.powerbi_token import PowerBiTokenError

    provider = MagicMock()
    provider.get_token_for_auth_user = AsyncMock(side_effect=PowerBiTokenError("connect first"))
    monkeypatch.setattr(api_state, "powerbi_token_provider_factory", lambda: provider)

    resp = client.post(f"/api/conversations/{CONV_ID}/turns/{TURN_ID}/rerun")
    assert resp.status_code == 403
    assert "grant_required" in resp.text


# ── chart baseline persistence ──────────────────────────────────────────────

def test_generate_chart_persists_baseline_with_principal(client, fake_state, monkeypatch):
    from src.api.routes import charts

    h = _history_with(
        fake_state,
        query_belongs_to_user=AsyncMock(return_value=True),
        upsert_turn_chart=AsyncMock(return_value=True),
    )
    dataset = {"columns": ["region", "sales"], "rows": [["N", 1], ["S", 2]]}
    monkeypatch.setattr(charts.result_cache, "get", lambda **_kw: dataset)

    async def no_hints(*_a, **_k):
        return {}

    monkeypatch.setattr(charts, "_load_geo_hints", no_hints)
    agent = MagicMock()
    agent.llm.generate = AsyncMock(return_value={"content": '{"chart_type":"bar","x":"region","y":"sales"}'})
    fake_state.agent_registry.get_agent = AsyncMock(return_value=agent)

    resp = client.post(
        "/api/generate-chart",
        json={"connection": "sales_db", "user_id": "spoofed-user", "query_id": TURN_ID, "question": "sales by region"},
    )
    assert resp.status_code == 200, resp.text
    # Ownership and persistence both use the verified principal, not the body.
    assert h.query_belongs_to_user.await_args.kwargs["user_id"] == "user-a"
    h.upsert_turn_chart.assert_awaited_once()
    persisted = h.upsert_turn_chart.await_args.kwargs
    assert persisted["user_id"] == "user-a"
    assert str(persisted["turn_id"]) == TURN_ID
    assert persisted["chart_config"] == resp.json()["chart_config"]
    assert persisted["request_started_at"].tzinfo is not None


def test_generate_chart_skips_persistence_without_query_id_or_over_cap(client, fake_state, monkeypatch):
    from src.api.routes import charts

    h = _history_with(
        fake_state,
        query_belongs_to_user=AsyncMock(return_value=True),
        upsert_turn_chart=AsyncMock(return_value=True),
    )
    dataset = {"columns": ["region", "sales"], "rows": [["N", 1], ["S", 2]]}
    monkeypatch.setattr(charts.result_cache, "get", lambda **_kw: dataset)

    async def no_hints(*_a, **_k):
        return {}

    monkeypatch.setattr(charts, "_load_geo_hints", no_hints)
    agent = MagicMock()
    agent.llm.generate = AsyncMock(return_value={"content": '{"chart_type":"bar","x":"region","y":"sales"}'})
    fake_state.agent_registry.get_agent = AsyncMock(return_value=agent)

    # No query_id -> nothing to attach the chart to.
    resp = client.post("/api/generate-chart", json={"connection": "sales_db", "question": "x"})
    assert resp.status_code == 200, resp.text
    h.upsert_turn_chart.assert_not_called()

    # Over the byte cap -> chart returned but not persisted, and any older
    # stored baseline is dropped so it cannot be restored in its place.
    h.clear_turn_chart = AsyncMock(return_value=True)
    monkeypatch.setattr(charts.settings, "CONVERSATION_CHART_MAX_BYTES", 10)
    resp = client.post("/api/generate-chart", json={"connection": "sales_db", "question": "x", "query_id": TURN_ID})
    assert resp.status_code == 200, resp.text
    h.upsert_turn_chart.assert_not_called()
    h.clear_turn_chart.assert_awaited_once()
    assert str(h.clear_turn_chart.await_args.kwargs["turn_id"]) == TURN_ID
    assert h.clear_turn_chart.await_args.kwargs["user_id"] == "user-a"
    assert h.clear_turn_chart.await_args.kwargs["request_started_at"].tzinfo is not None


# ── save_to_memory capture ──────────────────────────────────────────────────

def test_save_to_memory_persists_allowlist_and_snapshot():
    from src.agent.langgraph_agent.nodes.output import make_save_to_memory

    history = MagicMock()
    history.persistence_enabled = True
    history.update_llm_response = AsyncMock()
    history.update_execution = AsyncMock()
    history.upsert_turn_artifact = AsyncMock(return_value=True)
    node = make_save_to_memory(history, "gpt")

    state = {
        "query_id": TURN_ID,
        "generated_sql": "select 1",
        "query_result": {"columns": ["a"], "rows": [{"a": 1}], "row_count": 1},
        "formatted_response": {
            "answer": "one row",
            "metrics": {"input_tokens": 1},
            "findings": ["f"],
            "prompt": {"system": "SECRET SCHEMA"},
            "node_prompts": {"sql_generator": "SECRET PROMPT"},
            "results": {"rows": [[1]]},
        },
    }
    asyncio.run(node(state))
    history.upsert_turn_artifact.assert_awaited_once()
    kwargs = history.upsert_turn_artifact.await_args.kwargs
    assert kwargs["result_kind"] == "table"
    assert kwargs["snapshot_status"] == "stored"
    assert kwargs["result_snapshot"]["rows"] == [{"a": 1}]
    assert kwargs["answer"] == "one row" and kwargs["findings"] == ["f"]
    assert "SECRET" not in repr(kwargs)


def test_save_to_memory_records_blocked_sql_as_error_not_success():
    """SQL was generated but validation/governance stopped it before execution:
    the user saw an error, so the turn must be stored (and restored) as one."""
    from src.agent.langgraph_agent.nodes.output import make_save_to_memory

    history = MagicMock()
    history.persistence_enabled = True
    history.update_llm_response = AsyncMock()
    history.update_execution = AsyncMock()
    history.upsert_turn_artifact = AsyncMock(return_value=True)
    node = make_save_to_memory(history, "gpt")
    asyncio.run(node({
        "query_id": TURN_ID,
        "generated_sql": "select * from private.users",
        "query_result": {},
        "formatted_response": {"error": "Blocked by governance: cross-schema reference", "answer": None},
    }))
    exec_kwargs = history.update_execution.await_args.kwargs
    assert exec_kwargs["execution_status"] == "error"
    assert "governance" in exec_kwargs["error_message"]
    art = history.upsert_turn_artifact.await_args.kwargs
    assert art["result_kind"] == "error"
    assert art["result_snapshot"] is None and art["snapshot_status"] == "not_applicable"


def test_save_to_memory_marks_text_turn_terminal_and_not_applicable():
    from src.agent.langgraph_agent.nodes.output import make_save_to_memory

    history = MagicMock()
    history.persistence_enabled = True
    history.update_llm_response = AsyncMock()
    history.update_execution = AsyncMock()
    history.upsert_turn_artifact = AsyncMock(return_value=True)
    node = make_save_to_memory(history, "gpt")
    asyncio.run(node({"query_id": TURN_ID, "formatted_response": {"answer": "Hello!"}}))
    exec_kwargs = history.update_execution.await_args.kwargs
    assert exec_kwargs["execution_status"] == "success"
    art = history.upsert_turn_artifact.await_args.kwargs
    assert art["result_kind"] == "text"
    assert art["snapshot_status"] == "not_applicable"
    assert art["result_snapshot"] is None


def test_save_to_memory_skips_artifact_when_persistence_disabled():
    from src.agent.langgraph_agent.nodes.output import make_save_to_memory

    history = MagicMock()
    history.persistence_enabled = False
    history.update_llm_response = AsyncMock()
    history.update_execution = AsyncMock()
    history.upsert_turn_artifact = AsyncMock()
    node = make_save_to_memory(history, "gpt")
    asyncio.run(node({"query_id": TURN_ID, "generated_sql": "select 1", "query_result": {"columns": ["a"], "rows": []}}))
    history.update_execution.assert_awaited_once()
    history.upsert_turn_artifact.assert_not_called()
