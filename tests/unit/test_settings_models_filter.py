"""/api/settings/models — only chat-capable models are offered or accepted.

``admin_models`` is shared with Schema Modeler and also holds embedding,
rerank, transcription and image models. None of those can generate SQL, so the
settings surface must neither list them nor let them become the global model or
a per-prompt override. These tests stub the DB helpers; no database is used.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from fastapi.testclient import TestClient

from src.agent import llm_health
from src.api import app
from src.api.routes import settings as settings_routes

_ROWS = [
    {
        "id": 1, "name": "gpt-5.1", "display_name": "GPT 5.1", "description": "chat",
        "type": "completion", "is_enabled": True, "deployment_name": "gpt-5-1",
        "has_credentials": True, "is_db_default": True,
    },
    {
        "id": 2, "name": "text-embedding-3-small", "display_name": "Text Embedding 3 Small",
        "description": "embeddings", "type": "embedding", "is_enabled": True,
        "deployment_name": "emb", "has_credentials": True, "is_db_default": True,
    },
    {
        "id": 3, "name": "whisper-1", "display_name": "Whisper 1", "description": "stt",
        "type": "transcription", "is_enabled": True, "deployment_name": "whisper",
        "has_credentials": False, "is_db_default": False,
    },
    {
        # Legacy row with no type recorded: treated as chat so a real model is
        # never hidden by an incomplete catalogue.
        "id": 4, "name": "legacy-untyped", "display_name": "Legacy", "description": "",
        "type": None, "is_enabled": True, "deployment_name": "legacy",
        "has_credentials": True, "is_db_default": False,
    },
]


@pytest.fixture
def stubbed_models(monkeypatch):
    monkeypatch.setattr(settings_routes, "_list_models_from_db", AsyncMock(return_value=[dict(r) for r in _ROWS]))
    monkeypatch.setattr(settings_routes, "_get_active_from_db", AsyncMock(return_value="gpt-5.1"))
    monkeypatch.setattr(llm_health, "cached_health", lambda: {})


def test_list_models_hides_non_chat_models(client, stubbed_models):
    r = client.get("/api/settings/models")
    assert r.status_code == 200
    names = [m["name"] for m in r.json()]
    assert names == ["gpt-5.1", "legacy-untyped"]


def test_list_models_leaves_a_single_default_badge(client, stubbed_models):
    # The embedding row is also flagged default in admin_models_providers; once
    # it is filtered out only the chat default remains.
    defaults = [m["name"] for m in client.get("/api/settings/models").json() if m["is_default"]]
    assert defaults == ["gpt-5.1"]


def test_set_active_model_rejects_non_chat_model(client, stubbed_models, monkeypatch):
    monkeypatch.setattr(settings_routes, "_set_active_in_db", AsyncMock())
    r = client.put("/api/settings/models/active", json={"name": "text-embedding-3-small"})
    assert r.status_code == 404
    settings_routes._set_active_in_db.assert_not_awaited()


def test_set_active_model_accepts_chat_model(client, stubbed_models, monkeypatch):
    set_active = AsyncMock()
    monkeypatch.setattr(settings_routes, "_set_active_in_db", set_active)
    r = client.put("/api/settings/models/active", json={"name": "gpt-5.1"})
    assert r.status_code == 200
    set_active.assert_awaited_once_with("gpt-5.1")


def test_models_list_is_admin_only(make_internal_token, stubbed_models):
    viewer = TestClient(app)
    viewer.headers.update({"Authorization": f"Bearer {make_internal_token(role='viewer')}"})
    assert viewer.get("/api/settings/models").status_code == 403
    assert viewer.get("/api/settings/prompts").status_code == 403
    assert viewer.get("/api/settings/runtime").status_code == 403


def test_app_info_stays_open_to_every_role(make_internal_token):
    viewer = TestClient(app)
    viewer.headers.update({"Authorization": f"Bearer {make_internal_token(role='viewer')}"})
    assert viewer.get("/api/settings/app-info").status_code == 200
