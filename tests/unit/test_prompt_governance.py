"""Focused tests for versioned, settings-managed production prompts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.prompt_cache import PromptCache
from src.api import lifespan
from src.api.routes import settings
from src.security.internal_auth import Principal


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class _PromptConn:
    def __init__(self):
        self.fetches = 0
        self.executed = []
        self.current = {
            "prompt_place": "fused_router",
            "version": 3,
            "content": "old content",
            "model_id": 8,
            "model_name": "fast",
            "is_custom": True,
        }

    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, *_args):
        self.fetches += 1
        return self.current

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 0 1"


class _PromptPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


class _DbPromptCache:
    def __init__(self, content: str):
        self.content = content

    async def get_content(self, _name):
        return self.content

    async def get_model_override(self, _name):
        return None


class _FailingPromptCache:
    async def get_content(self, _name):
        raise RuntimeError("database unavailable")

    async def get_model_override(self, _name):
        raise RuntimeError("database unavailable")


def test_registry_covers_every_packaged_prompt_and_contract():
    names = {entry["name"] for entry in settings.PROMPT_REGISTRY}
    assert names == set(settings.PROMPT_REQUIRED_PLACEHOLDERS)
    assert settings.PROMPT_MODEL_OWNERS <= names
    loader = PromptLoader()

    settings_ui = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "static"
        / "settings"
        / "settingsPage.js"
    ).read_text(encoding="utf-8")
    for entry in settings.PROMPT_REGISTRY:
        content = Path(entry["path"]).read_text(encoding="utf-8")
        settings._validate_prompt_content(entry["name"], content)
        assert f"prompt:{entry['name']}" in settings_ui
        if Path(entry["path"]).suffix == ".md":
            assert loader.get(entry["name"]) == content


def test_placeholder_validation_rejects_missing_and_unknown_values():
    with pytest.raises(HTTPException) as exc:
        settings._validate_prompt_content(
            "fused_router",
            "Question: {question}; unrelated: {not_supplied}",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["missing"] == ["conversation_summary", "source_description"]
    assert exc.value.detail["unknown"] == ["not_supplied"]


def test_placeholder_validation_rejects_malformed_format_syntax():
    with pytest.raises(HTTPException) as exc:
        settings._validate_prompt_content("fused_eval_analytics_system", "bad { brace")

    assert exc.value.status_code == 422
    assert exc.value.detail["format_error"]


def test_placeholder_validation_rejects_whitespace_only_content():
    with pytest.raises(HTTPException) as exc:
        settings._validate_prompt_content("fused_eval_analytics_system", " \n\t ")

    assert exc.value.status_code == 422
    assert "whitespace" in exc.value.detail


@pytest.mark.asyncio
async def test_settings_ignores_legacy_whitespace_only_active_prompt(monkeypatch):
    stale_row = {
        "content": " \n\t ",
        "is_custom": True,
        "version": 99,
        "model_id": 8,
        "model_name": "stale-model",
    }
    monkeypatch.setattr(settings, "_db_list_prompts", AsyncMock(return_value={"fused_router": stale_row}))
    monkeypatch.setattr(settings, "_db_get_prompt", AsyncMock(return_value=stale_row))

    metadata = await settings.list_prompts()
    listed = next(prompt for prompt in metadata if prompt.name == "fused_router")
    detail = await settings.get_prompt("fused_router")

    assert listed.is_custom is False
    assert listed.version == 1
    assert detail.is_custom is False
    assert detail.content == Path(settings._entry_for("fused_router")["path"]).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_prompt_loader_prefers_db_content_and_falls_back_to_disk():
    loader = PromptLoader()
    loader.attach_cache(_DbPromptCache("custom {question}"))
    assert await loader.arender("fused_router", question="revenue") == "custom revenue"

    loader.attach_cache(_FailingPromptCache())
    rendered = await loader.arender(
        "fused_router",
        question="revenue",
        conversation_summary="none",
        source_description="sales",
    )
    assert "revenue" in rendered
    assert "sales" in rendered


@pytest.mark.asyncio
async def test_prompt_cache_reuses_and_invalidates_db_entry():
    conn = _PromptConn()
    llm = MagicMock()
    llm.build_model_override_for_model_id = AsyncMock(return_value={"model": "fast"})
    cache = PromptCache(_PromptPool(conn), llm)

    assert await cache.get_content("fused_router") == "old content"
    assert await cache.get_content("fused_router") == "old content"
    assert conn.fetches == 1
    cache.invalidate("fused_router")
    await cache.get_content("fused_router")
    assert conn.fetches == 2


@pytest.mark.asyncio
async def test_prompt_cache_rejects_empty_active_content():
    conn = _PromptConn()
    conn.current["content"] = "   "
    cache = PromptCache(_PromptPool(conn), MagicMock())

    with pytest.raises(KeyError, match="No usable active prompt"):
        await cache.get_content("fused_router")


@pytest.mark.asyncio
async def test_save_creates_new_version_and_actor_audit(monkeypatch):
    conn = _PromptConn()

    async def fake_pool():
        return _PromptPool(conn)

    monkeypatch.setattr("src.metadata.get_metadata_pool", fake_pool)
    actor = Principal(user_id="42", role="admin", email="admin@example.test")
    version = await settings._db_save_prompt(
        "fused_router",
        "new content",
        actor=actor,
        details={"content_length": 11},
    )

    assert version == 4
    insert = next(args for query, args in conn.executed if "INSERT INTO insights_prompts" in query)
    assert insert == ("fused_router", "new content", 4, True, 8)
    audit = next(args for query, args in conn.executed if "INSERT INTO insights_prompt_audit" in query)
    assert audit[:5] == ("fused_router", "save", "42", "admin@example.test", 4)


@pytest.mark.asyncio
async def test_concurrent_prompt_save_returns_conflict(monkeypatch):
    unique_error = type("UniqueViolationError", (Exception,), {})

    class ConflictConn(_PromptConn):
        async def execute(self, query, *args):
            if "INSERT INTO insights_prompts" in query:
                raise unique_error("active row already exists")
            return await super().execute(query, *args)

    conn = ConflictConn()

    async def fake_pool():
        return _PromptPool(conn)

    monkeypatch.setattr("src.metadata.get_metadata_pool", fake_pool)
    with pytest.raises(HTTPException) as exc:
        await settings._db_save_prompt("fused_router", "new content")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_model_change_rejects_legacy_unusable_prompt_content(monkeypatch):
    conn = _PromptConn()
    conn.current["content"] = " \t "

    async def fake_pool():
        return _PromptPool(conn)

    monkeypatch.setattr("src.metadata.get_metadata_pool", fake_pool)
    with pytest.raises(HTTPException) as exc:
        await settings._db_save_prompt("fused_router", model_id=42)
    assert exc.value.status_code == 409
    assert "Reset or save valid content" in exc.value.detail


@pytest.mark.asyncio
async def test_restore_copies_historical_model_override(monkeypatch):
    source = {
        "content": (
            "Question: {question}; history: {conversation_summary}; "
            "source: {source_description}"
        ),
        "model_id": 9,
        "is_custom": False,
        "version": 2,
    }

    class RestoreConn:
        async def fetchrow(self, *_args):
            return source

    class RestorePool:
        def acquire(self):
            return _AsyncContext(RestoreConn())

    async def fake_pool():
        return RestorePool()

    save = AsyncMock(return_value=4)
    monkeypatch.setattr("src.metadata.get_metadata_pool", fake_pool)
    monkeypatch.setattr(settings, "_db_save_prompt", save)
    monkeypatch.setattr(
        settings,
        "_db_get_prompt",
        AsyncMock(return_value={"model_id": 9, "model_name": "historical"}),
    )
    monkeypatch.setattr(settings, "_invalidate_cache", lambda _name: None)

    actor = Principal(user_id="42", role="admin", email="admin@example.test")
    result = await settings.restore_prompt_version("fused_router", 123, actor)

    assert result.version == 4
    assert result.model_id == 9
    assert result.is_custom is False
    assert save.await_args.kwargs["model_id"] == 9
    assert save.await_args.kwargs["is_custom"] is False
    assert save.await_args.kwargs["action"] == "restore"


@pytest.mark.asyncio
async def test_seed_registers_every_prompt_for_new_database():
    class SeedConn:
        def __init__(self):
            self.executed = []

        def transaction(self):
            return _AsyncContext(self)

        async def fetchrow(self, *_args):
            return None

        async def execute(self, query, *args):
            self.executed.append((query, args))

    conn = SeedConn()
    await lifespan._seed_prompts(conn)
    seeded_places = [
        args[0]
        for query, args in conn.executed
        if "INSERT INTO insights_prompts" in query
    ]
    assert seeded_places == [entry["name"] for entry in settings.PROMPT_REGISTRY]


@pytest.mark.asyncio
async def test_seed_tolerates_concurrent_first_insert():
    class ConcurrentSeedConn:
        def transaction(self):
            return _AsyncContext(self)

        async def fetchrow(self, *_args):
            return None

        async def execute(self, *_args):
            return "INSERT 0 0"

    await lifespan._seed_prompts(ConcurrentSeedConn())


def test_prompt_edit_requires_admin_role(client, make_internal_token, monkeypatch):
    save = AsyncMock(return_value=2)
    monkeypatch.setattr(settings, "_db_save_prompt", save)
    viewer_token = make_internal_token(role="viewer")
    client.headers.update({"Authorization": f"Bearer {viewer_token}"})
    content = Path(settings._entry_for("fused_router")["path"]).read_text(encoding="utf-8")

    response = client.put("/api/settings/prompts/fused_router", json={"content": content})

    assert response.status_code == 403
    save.assert_not_awaited()


def test_prompt_edit_rejects_unrenderable_template(client, monkeypatch):
    save = AsyncMock(return_value=2)
    monkeypatch.setattr(settings, "_db_save_prompt", save)

    response = client.put(
        "/api/settings/prompts/fused_router",
        json={
            "content": (
                "Question: {question}; history: {conversation_summary}; "
                "source: {source_description}; bad {"
            )
        },
    )

    assert response.status_code == 422
    save.assert_not_awaited()


def test_companion_prompt_rejects_model_override(client):
    response = client.put(
        "/api/settings/prompts/fused_eval_analytics_user/model",
        json={"model_name": "fast-model"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("delete", "/api/settings/prompts/fused_router", None),
        ("put", "/api/settings/prompts/fused_router/model", {"model_name": None}),
        ("post", "/api/settings/prompts/fused_router/restore/1", None),
        ("get", "/api/settings/prompts/fused_router/audit", None),
        ("get", "/api/settings/prompts/fused_router/versions", None),
        ("get", "/api/settings/prompt-contexts", None),
        ("get", "/api/settings/prompts/fused_router/resolved?connection=sales", None),
    ],
)
def test_prompt_governance_endpoints_require_admin(
    client,
    make_internal_token,
    method,
    path,
    payload,
):
    client.headers.update({"Authorization": f"Bearer {make_internal_token(role='viewer')}"})
    response = client.request(method.upper(), path, json=payload)
    assert response.status_code == 403


