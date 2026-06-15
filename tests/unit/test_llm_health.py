"""Unit tests for `src.agent.llm_health` — model credential probing.

These exercise the probe logic in isolation by stubbing the chat-model factory,
so no DB or live provider is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import llm_health
from src.agent.llm_health import (
    FAIL,
    PASS,
    SKIP,
    ModelHealth,
    _is_chat_model,
    probe_row,
)


class _FakeChat:
    """Minimal stand-in for a LangChain chat model."""

    def __init__(self, reply: str = "OK", error: Exception | None = None):
        self._reply = reply
        self._error = error

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._reply)


# ----------------------------------------------------------------------
# _is_chat_model
# ----------------------------------------------------------------------
class TestIsChatModel:
    @pytest.mark.parametrize("kind", ["completion", "Completion", "chat", "", None, "some-new-type"])
    def test_chatish_and_unknown_types_are_probed(self, kind):
        assert _is_chat_model(kind) is True

    @pytest.mark.parametrize("kind", ["embedding", "rerank", "transcription", "TTS", "image", "vision"])
    def test_non_chat_types_are_skipped(self, kind):
        assert _is_chat_model(kind) is False


# ----------------------------------------------------------------------
# ModelHealth.healthy tri-state
# ----------------------------------------------------------------------
class TestHealthyTriState:
    def test_pass_is_true(self):
        assert ModelHealth("m", "p", "i", PASS, "ok", 0.1).healthy is True

    def test_fail_is_false(self):
        assert ModelHealth("m", "p", "i", FAIL, "boom", 0.1).healthy is False

    def test_skip_is_none(self):
        # None keeps skipped (non-chat) models neutral in the UI, not red.
        assert ModelHealth("m", "p", "i", SKIP, "n/a", 0.0).healthy is None


# ----------------------------------------------------------------------
# probe_row
# ----------------------------------------------------------------------
class TestProbeRow:
    async def test_non_chat_skipped_without_building(self, monkeypatch):
        built = {"flag": False}

        def _should_not_build(row):
            built["flag"] = True
            raise AssertionError("non-chat models must not be built/probed")

        monkeypatch.setattr(llm_health, "_build_chat_model", _should_not_build)
        h = await probe_row(
            {"model_name": "emb", "provider_name": "azure_openai", "model_type": "embedding"}
        )
        assert h.status == SKIP
        assert h.healthy is None
        assert built["flag"] is False

    async def test_pass(self, monkeypatch):
        monkeypatch.setattr(llm_health, "_build_chat_model", lambda row: _FakeChat(reply="OK"))
        h = await probe_row(
            {"model_name": "gpt", "provider_name": "azure_openai", "model_type": "completion"}
        )
        assert h.status == PASS
        assert h.healthy is True
        assert "OK" in h.detail

    async def test_invoke_failure_is_fail(self, monkeypatch):
        monkeypatch.setattr(
            llm_health, "_build_chat_model",
            lambda row: _FakeChat(error=Exception("Error code: 401 - Access denied")),
        )
        h = await probe_row(
            {"model_name": "gpt", "provider_name": "azure_openai", "model_type": "completion"}
        )
        assert h.status == FAIL
        assert h.healthy is False

    async def test_build_failure_is_fail(self, monkeypatch):
        def _raise(row):
            raise ValueError("bad config")

        monkeypatch.setattr(llm_health, "_build_chat_model", _raise)
        h = await probe_row(
            {"model_name": "x", "provider_name": "openai", "model_type": "completion"}
        )
        assert h.status == FAIL

    async def test_missing_driver_is_skip(self, monkeypatch):
        def _raise(row):
            raise ModuleNotFoundError("No module named 'langchain_aws'")

        monkeypatch.setattr(llm_health, "_build_chat_model", _raise)
        h = await probe_row(
            {"model_name": "bedrock", "provider_name": "amazon_bedrock", "model_type": "completion"}
        )
        assert h.status == SKIP
        assert h.healthy is None
