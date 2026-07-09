"""Tests for `src.api.routes.query` validation paths.

We intentionally don't go past the validation layer here — the agent itself
is exercised by integration tests, not unit tests.
"""

from __future__ import annotations

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


def test_query_requires_authenticated_user(client, fake_state):
    resp = client.post(
        "/api/query",
        json={"question": "show all customers", "connection": "sales_db"},
    )
    assert resp.status_code == 401


def test_query_rejects_foreign_session_id(client, fake_state):
    fake_state.history_service.session_belongs_to_user = AsyncMock(return_value=False)
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
    fake_state.history_service.session_belongs_to_user.assert_awaited_once()


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
