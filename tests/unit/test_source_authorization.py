"""Regression tests for per-source authorization at the API boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api import dependencies
from src.api import lifespan
from src.security.internal_auth import Principal

class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class _GrantPool:
    def __init__(self, rows):
        self.conn = SimpleNamespace(fetch=AsyncMock(return_value=rows))

    def acquire(self):
        return _AsyncContext(self.conn)


def _viewer(client, make_internal_token):
    client.headers.update(
        {"Authorization": f"Bearer {make_internal_token(user_id='user-b', role='viewer')}"}
    )


def test_query_denies_ungranted_source_before_agent_resolution(
    client, fake_state, make_internal_token, monkeypatch
):
    _viewer(client, make_internal_token)

    async def get_pool():
        return _GrantPool([])

    monkeypatch.setattr("src.metadata.get_metadata_pool", get_pool)
    fake_state.agent_registry.get_agent = AsyncMock()

    response = client.post(
        "/api/query",
        json={"question": "show revenue", "connection": "finance"},
    )

    assert response.status_code == 403
    fake_state.agent_registry.get_agent.assert_not_awaited()


def test_connections_only_lists_sources_granted_to_principal(
    client, fake_state, make_internal_token, monkeypatch
):
    _viewer(client, make_internal_token)
    fake_state.connection_service.list_connections = AsyncMock(
        return_value=[
            SimpleNamespace(source_key="sales", to_public_dict=lambda: {"source_key": "sales"}),
            SimpleNamespace(source_key="finance", to_public_dict=lambda: {"source_key": "finance"}),
        ]
    )

    async def get_pool():
        return _GrantPool([{"source_key": "sales", "subject_type": "user", "subject_id": "user-b"}])

    monkeypatch.setattr("src.metadata.get_metadata_pool", get_pool)

    response = client.get("/api/connections")

    assert response.status_code == 200
    assert response.json()["connections"] == [{"source_key": "sales"}]


def test_history_rejects_direct_api_impersonation(client, fake_state):
    fake_state.history_service.record_feedback = AsyncMock(return_value=True)

    response = client.post(
        "/api/feedback",
        json={
            "query_id": "11111111-1111-1111-1111-111111111111",
            "user_id": "user-b",
            "feedback": "thumbs_up",
        },
    )

    assert response.status_code == 403
    fake_state.history_service.record_feedback.assert_not_awaited()


def test_profile_requires_authorized_connection(client, fake_state):
    response = client.post(
        "/api/generate-profile",
        json={"user_id": "user-a", "dataset": {"columns": ["x"], "rows": [[1]]}},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_group_source_grant_requires_fresh_complete_membership(monkeypatch):
    async def get_pool():
        return _GrantPool([{"source_key": "finance", "subject_type": "group", "subject_id": "g1"}])

    membership = SimpleNamespace(
        get_membership=AsyncMock(return_value={"fresh": True, "complete": True, "group_ids": ["g1"]})
    )
    monkeypatch.setattr("src.metadata.get_metadata_pool", get_pool)
    monkeypatch.setattr(dependencies, "ensure_identity", AsyncMock(return_value={"id": "identity-1"}))
    monkeypatch.setattr(dependencies, "get_identity_service", lambda: membership)

    allowed = await dependencies.authorized_source_keys(
        Principal(
            user_id="user-b",
            role="viewer",
            tenant_id="tenant",
            object_id="object",
            groups=("g1",),
            groups_complete=True,
        )
    )
    assert allowed == {"finance"}

    incomplete = await dependencies.authorized_source_keys(
        Principal(
            user_id="user-b",
            role="viewer",
            tenant_id="tenant",
            object_id="object",
            groups=("g1",),
            groups_complete=False,
        )
    )
    assert incomplete == set()
    assert dependencies.ensure_identity.await_count == 1


@pytest.mark.asyncio
async def test_source_access_bootstrap_runs_once():
    class BootstrapConn:
        def __init__(self):
            self.marker = None
            self.executed = []

        def transaction(self):
            return _AsyncContext(self)

        async def fetchval(self, *_args):
            return self.marker

        async def execute(self, query, *args):
            self.executed.append((query, args))
            if "source_access_bootstrapped" in query:
                self.marker = "true"
            return "INSERT 0 2"

    conn = BootstrapConn()
    await lifespan._bootstrap_source_access(conn)
    await lifespan._bootstrap_source_access(conn)

    assert sum("insights_source_access" in query for query, _ in conn.executed) == 1
    grant_query = next(query for query, _ in conn.executed if "insights_source_access" in query)
    assert "'viewer'" in grant_query and "'editor'" in grant_query
