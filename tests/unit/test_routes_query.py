"""Tests for `src.api.routes.query` validation paths.

We intentionally don't go past the validation layer here — the agent itself
is exercised by integration tests, not unit tests.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock


def test_query_rejects_empty_question(client, fake_state):
    resp = client.post(
        "/api/query",
        json={"question": "   ", "connection": "sales_db"},
    )
    assert resp.status_code == 400
    assert "Question cannot be empty" in resp.json()["detail"]


def test_query_returns_503_when_registry_missing(client, empty_state):
    resp = client.post(
        "/api/query",
        json={
            "question": "show all customers",
            "connection": "sales_db",
            "user_context": {"user_id": "user-a"},
        },
    )
    assert resp.status_code == 503


def test_query_requires_authenticated_user(anon_client, fake_state):
    resp = anon_client.post(
        "/api/query",
        json={"question": "show all customers", "connection": "sales_db"},
    )
    assert resp.status_code == 401


def test_query_rejects_foreign_session_id(client, fake_state):
    fake_state.history_service.conversation_belongs_to_user = AsyncMock(return_value=False)
    resp = client.post(
        "/api/query",
        json={
            "question": "show all customers",
            "connection": "sales_db",
            "session_id": "11111111-1111-1111-1111-111111111111",
            "user_context": {"user_id": "user-a"},
        },
    )
    assert resp.status_code == 404
    fake_state.history_service.conversation_belongs_to_user.assert_awaited_once()
    # The check is connection-scoped: a conversation from connection A cannot
    # be continued against connection B even by its owner.
    kwargs = fake_state.history_service.conversation_belongs_to_user.await_args.kwargs
    assert kwargs["user_id"] == "user-a"
    assert kwargs["source_key"] == "sales_db"


def test_query_cache_uses_verified_user_id(client, fake_state, monkeypatch):
    fake_state.agent_registry.get_agent = AsyncMock()
    fake_agent = MagicMock()
    fake_agent.process_question = AsyncMock(
        return_value={
            "question": "show all customers",
            "query_id": "22222222-2222-2222-2222-222222222222",
            "session_id": "33333333-3333-3333-3333-333333333333",
            "sql": "select 1",
            "results": {"columns": ["x"], "rows": [[1]]},
            "answer": None,
            "prompt": None,
            "error": None,
            "metrics": None,
            "trace": [],
        }
    )
    fake_state.agent_registry.get_agent.return_value = fake_agent
    cache_put = MagicMock()
    monkeypatch.setattr("src.api.routes.query.result_cache.put", cache_put)

    resp = client.post(
        "/api/query",
        json={
            "question": "show all customers",
            "connection": "sales_db",
            "user_context": {"user_id": "user-a"},
        },
    )

    assert resp.status_code == 200
    assert cache_put.call_args.kwargs["user_id"] == "user-a"


def test_connect_signal_survives_the_response_model(client, fake_state):
    """The DAX agent's connect prompt must reach the UI.

    `QueryResponse(**result)` silently drops fields the model doesn't declare,
    which previously made the "Connect Power BI" prompt unreachable.
    """
    fake_state.agent_registry.get_agent = AsyncMock()
    fake_agent = MagicMock()
    fake_agent.process_question = AsyncMock(
        return_value={
            "question": "total sales by region",
            "sql": "",
            "results": None,
            "answer": None,
            "error": "Connect your Power BI account to run this query.",
            "needs_connect": True,
            "connect_provider": "power-bi",
        }
    )
    fake_state.agent_registry.get_agent.return_value = fake_agent

    resp = client.post(
        "/api/query",
        json={
            "question": "total sales by region",
            "connection": "pbi_sales",
            "user_context": {"user_id": "user-a"},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_connect"] is True
    assert body["connect_provider"] == "power-bi"


def test_query_stream_emits_real_nodes_then_same_result(client, fake_state, monkeypatch):
    fake_state.agent_registry.get_agent = AsyncMock()
    monkeypatch.setattr("src.api.routes.query._maybe_snapshot", AsyncMock())
    monkeypatch.setattr("src.api.routes.query._maybe_propose_tool", AsyncMock(return_value=None))
    fake_agent = MagicMock()
    result = {
        "question": "show all customers",
        "query_id": "22222222-2222-2222-2222-222222222222",
        "session_id": "33333333-3333-3333-3333-333333333333",
        "sql": "select 1",
        "results": {"columns": ["x"], "rows": [[1]]},
        "answer": "One row.",
        "prompt": None,
        "error": None,
        "metrics": {"execution_time_ms": 8},
        "trace": [{"node": "execute_query", "elapsed_ms": 8}],
        "findings": ["The result contains one row."],
        "suggestions": ["Compare it with last month."],
        "followups": ["What changed?"],
    }

    async def process_question(**kwargs):
        callback = kwargs["progress_callback"]
        callback(
            {
                "node": "execute_query",
                "status": "node_started",
                "icon": "▶",
                "type": "db",
            }
        )
        callback(
            {
                "node": "execute_query",
                "status": "node_finished",
                "icon": "▶",
                "type": "db",
                "elapsed_ms": 8,
            }
        )
        return result

    fake_agent.process_question = AsyncMock(side_effect=process_question)
    fake_state.agent_registry.get_agent.return_value = fake_agent

    started = time.monotonic()
    streamed = client.post(
        "/api/query/stream",
        json={"question": "show all customers", "connection": "sales_db"},
    )
    elapsed = time.monotonic() - started

    assert streamed.status_code == 200
    assert elapsed < 3, "final result waited for the SSE heartbeat timeout"
    assert streamed.headers["content-type"].startswith("text/event-stream")
    body = streamed.text
    assert body.index("event: open") < body.index("event: node") < body.index("event: result")
    assert '"status":"node_started"' in body
    assert '"status":"node_finished"' in body
    assert '"findings":["The result contains one row."]' in body
    assert '"followups":["What changed?"]' in body


def test_query_stream_emits_partial_rows_before_result(client, fake_state, monkeypatch):
    """The graph hands the accepted rows to the stream before the narration
    LLM call; the route caches them (so /api/generate-chart can start) and
    forwards a `partial` event between the node events and the `result`."""
    fake_state.agent_registry.get_agent = AsyncMock()
    monkeypatch.setattr("src.api.routes.query._maybe_snapshot", AsyncMock())
    monkeypatch.setattr("src.api.routes.query._maybe_propose_tool", AsyncMock(return_value=None))
    cache_put = MagicMock()
    monkeypatch.setattr("src.api.routes.query.result_cache.put", cache_put)
    query_id = "22222222-2222-2222-2222-222222222222"
    dataset = {"columns": ["x"], "rows": [[1], [2]]}
    result = {
        "question": "show all customers",
        "query_id": query_id,
        "session_id": "33333333-3333-3333-3333-333333333333",
        "sql": "select 1",
        "results": dataset,
        "answer": "Two rows.",
        "error": None,
        "metrics": {"execution_time_ms": 8},
        "trace": [],
        "findings": ["Two rows came back."],
    }
    order = []

    async def process_question(**kwargs):
        progress = kwargs["progress_callback"]
        partial = kwargs["partial_callback"]
        progress({"node": "trivial_result_check", "status": "node_started", "icon": "·", "type": "logic"})
        partial({
            "query_id": query_id,
            "session_id": result["session_id"],
            "sql": "select 1",
            "results": dataset,
            "revision": 0,
            "provisional": True,
        })
        order.append(("cache_calls_at_partial", cache_put.call_count))
        progress({"node": "trivial_result_check", "status": "node_finished", "icon": "·", "type": "logic", "elapsed_ms": 1})
        return result

    fake_agent = MagicMock()
    fake_agent.process_question = AsyncMock(side_effect=process_question)
    fake_state.agent_registry.get_agent.return_value = fake_agent

    streamed = client.post(
        "/api/query/stream",
        json={"question": "show all customers", "connection": "sales_db"},
    )

    assert streamed.status_code == 200
    body = streamed.text
    started = body.index('"status":"node_started"')
    partial_at = body.index("event: partial")
    finished = body.index('"status":"node_finished"')
    assert started < partial_at < finished < body.index("event: result")
    assert '"provisional":true' in body
    assert '"revision":0' in body
    # Rows were cached synchronously inside the callback, before the event left.
    assert order == [("cache_calls_at_partial", 1)]
    first = cache_put.call_args_list[0].kwargs
    assert first["user_id"] == "user-a"
    assert first["connection"] == "sales_db"
    assert first["query_id"] == query_id
    assert first["dataset"] is dataset
    # The final put reuses the same key, so it simply overwrites.
    assert cache_put.call_count == 2
    assert cache_put.call_args_list[1].kwargs["query_id"] == query_id
    # The narrative still arrives with the result.
    assert '"findings":["Two rows came back."]' in body


def test_query_json_route_passes_no_partial_callback(client, fake_state):
    fake_state.agent_registry.get_agent = AsyncMock()
    fake_agent = MagicMock()
    fake_agent.process_question = AsyncMock(
        return_value={
            "question": "q", "sql": "select 1", "results": {"columns": ["x"], "rows": [[1]]},
            "error": None, "trace": [],
        }
    )
    fake_state.agent_registry.get_agent.return_value = fake_agent

    resp = client.post(
        "/api/query",
        json={"question": "show all customers", "connection": "sales_db"},
    )

    assert resp.status_code == 200
    assert fake_agent.process_question.await_args.kwargs["partial_callback"] is None


def test_query_stream_requires_authenticated_user(anon_client, fake_state):
    response = anon_client.post(
        "/api/query/stream",
        json={"question": "show all customers", "connection": "sales_db"},
    )
    assert response.status_code == 401


def test_query_stream_emits_dataset_before_slow_enrichment(
    client, fake_state, monkeypatch
):
    fake_state.agent_registry.get_agent = AsyncMock()
    fake_agent = MagicMock()
    fake_agent.process_question = AsyncMock(
        return_value={
            "question": "show customers",
            "query_id": "22222222-2222-2222-2222-222222222222",
            "sql": "select 1",
            "results": {"columns": ["x"], "rows": [[1]]},
            "error": None,
            "trace": [],
        }
    )
    fake_state.agent_registry.get_agent.return_value = fake_agent

    async def slow_snapshot(result, **_kwargs):
        await asyncio.sleep(0.05)
        result["result_handle"] = "snapshot-handle"

    monkeypatch.setattr("src.api.routes.query._maybe_snapshot", slow_snapshot)
    monkeypatch.setattr(
        "src.api.routes.query._maybe_propose_tool",
        AsyncMock(return_value=None),
    )

    response = client.post(
        "/api/query/stream",
        json={"question": "show customers", "connection": "sales_db"},
    )

    assert response.status_code == 200
    assert response.text.index("event: result") < response.text.index(
        "event: enrichment"
    )
    assert '"result_handle":"snapshot-handle"' in response.text


def test_tables_requires_connection(client, fake_state):
    # FastAPI returns 422 on a missing required query param.
    resp = client.get("/api/tables")
    assert resp.status_code == 422


# ── /api/tables-rich ──────────────────────────────────────────────────────────

def test_tables_rich_requires_connection(client, fake_state):
    """Missing `connection` query param → 422."""
    resp = client.get("/api/tables-rich")
    assert resp.status_code == 422


def test_tables_rich_returns_503_when_loader_missing(client, empty_state):
    """MetadataLoader not initialised → 503."""
    resp = client.get("/api/tables-rich?connection=sales_db")
    assert resp.status_code == 503


def test_tables_rich_returns_table_list(client, fake_state):
    """Happy path: returns [{name, description, col_count}] from the loader."""
    from unittest.mock import AsyncMock

    expected = [
        {"name": "orders",   "description": "Customer orders", "col_count": 12},
        {"name": "products", "description": None,              "col_count": 5},
    ]
    fake_state.metadata_loader.load_tables_rich = AsyncMock(return_value=expected)

    resp = client.get("/api/tables-rich?connection=sales_db")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tables"] == expected
    fake_state.metadata_loader.load_tables_rich.assert_called_once_with("sales_db")


def test_tables_rich_returns_empty_list(client, fake_state):
    """Catalog with no tables returns an empty list (not an error)."""
    from unittest.mock import AsyncMock

    fake_state.metadata_loader.load_tables_rich = AsyncMock(return_value=[])

    resp = client.get("/api/tables-rich?connection=empty_db")

    assert resp.status_code == 200
    assert resp.json() == {"tables": []}
