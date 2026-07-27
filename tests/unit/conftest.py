"""Shared fixtures for unit tests.

We deliberately avoid the FastAPI lifespan in unit tests:
- Constructing `src.api.app` triggers `create_app()` (just route wiring).
- `TestClient(app)` used WITHOUT a `with` block does NOT execute the
  lifespan, so no DB pool, no Azure OpenAI client, no AgentRegistry are
  required. Tests that need services inject fakes into `src.api.state`
  via the `fake_state` fixture.

Internal auth: FastAPI is fronted by ``InternalAuthMiddleware`` (default-deny).
Every non-exempt route needs a valid internal token minted by Flask, and the
verified :class:`Principal` — not the request body — is the source of identity.
The default ``client`` fixture therefore carries a signed token so route tests
exercise real handler logic; use ``anon_client`` to assert the 401 path.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Required env vars for `src.config.Settings` to import without errors.
# Real values are irrelevant: nothing in unit tests connects to anything.
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.invalid")
os.environ.setdefault("METADATA_DB_HOST", "test")
os.environ.setdefault("METADATA_DB_NAME", "test")
os.environ.setdefault("METADATA_DB_USER", "test")
os.environ.setdefault("METADATA_DB_PASSWORD", "test")
# A fixed, strong (non-placeholder) signing secret so the same key mints and
# verifies internal tokens across the test process.
os.environ.setdefault("INTERNAL_API_SECRET", "unit-test-internal-secret-0123456789abcdef")

from fastapi.testclient import TestClient  # noqa: E402

from src.api import app  # noqa: E402
from src.api import state as api_state  # noqa: E402


def _mint_internal_token(**claims) -> str:
    """Mint a valid internal token for tests (default: admin ``user-a``)."""
    from src.security.internal_auth import issue_internal_token

    base = {
        "user_id": "user-a",
        "role": "admin",
        "name": "Test User",
        "email": "user-a@test.local",
    }
    base.update(claims)
    return issue_internal_token(base)


@pytest.fixture
def make_internal_token():
    """Factory to mint internal tokens with custom principal claims."""
    return _mint_internal_token


@pytest.fixture
def client() -> TestClient:
    """Authenticated, lifespan-free TestClient (Principal = admin ``user-a``).

    Services come from `fake_state`. Identity is taken from the signed token,
    matching the production internal-auth boundary.
    """
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {_mint_internal_token()}"})
    return c


@pytest.fixture
def anon_client() -> TestClient:
    """Unauthenticated TestClient — for asserting the default-deny 401 path."""
    return TestClient(app)


@pytest.fixture
def fake_state(monkeypatch):
    """Replace `src.api.state.<service>` handles with MagicMocks for the test.

    Returns a small object exposing the four mocks so tests can stub return
    values fluently:
        fake_state.connection_service.list_connections.return_value = ...
    """

    class _FakeState:
        connection_service = MagicMock(name="ConnectionService")
        metadata_loader = MagicMock(name="MetadataLoader")
        history_service = MagicMock(name="HistoryService")
        agent_registry = MagicMock(name="AgentRegistry")

    fakes = _FakeState()
    # ``resolve_agent`` first awaits ``get_connection`` to decide SQL vs. Power BI
    # dispatch. Default the double to a non-Power BI connection so the SQL path
    # (the untouched default) works; PBI-specific tests override this explicitly.
    fakes.connection_service.get_connection = AsyncMock(
        return_value=SimpleNamespace(is_power_bi=False, source_key="sales_db")
    )
    monkeypatch.setattr(api_state, "connection_service", fakes.connection_service)
    monkeypatch.setattr(api_state, "metadata_loader", fakes.metadata_loader)
    monkeypatch.setattr(api_state, "history_service", fakes.history_service)
    monkeypatch.setattr(api_state, "agent_registry", fakes.agent_registry)
    return fakes


@pytest.fixture
def empty_state(monkeypatch):
    """Force every service handle to None to exercise the 503 path."""
    monkeypatch.setattr(api_state, "connection_service", None)
    monkeypatch.setattr(api_state, "metadata_loader", None)
    monkeypatch.setattr(api_state, "history_service", None)
    monkeypatch.setattr(api_state, "agent_registry", None)
