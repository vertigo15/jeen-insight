"""Unit tests for LLM error classification and the auto-fallback path in
``src.agent.llm_service``.

The chat-model factory, DB row fetch and health cache are all stubbed so the
fallback behaviour is exercised without a DB or live provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import llm_health
from src.agent import llm_service as svc_mod
from src.agent.llm_health import ModelHealth
from src.agent.llm_service import (
    LangChainLlmService,
    LLMUnavailableError,
    classify_llm_error,
)


class _FakeChat:
    def __init__(self, reply: str | None = None, error: Exception | None = None):
        self._reply = reply
        self._error = error

    def bind_tools(self, *args, **kwargs):
        return self

    def bind(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._reply)


def _service(active_chat) -> LangChainLlmService:
    return LangChainLlmService(
        pool=object(),
        model_name="bad",
        chat_model=active_chat,
        provider_name="azure_openai",
    )


# ----------------------------------------------------------------------
# classify_llm_error
# ----------------------------------------------------------------------
class TestClassifyLlmError:
    def test_auth_401(self):
        msg = classify_llm_error(
            Exception("Error code: 401 - Access denied due to invalid subscription key")
        )
        assert "api key is invalid or expired" in msg.lower()

    def test_not_found_404(self):
        assert "not found" in classify_llm_error(Exception("404 model not found")).lower()

    def test_rate_limit_429(self):
        assert "rate-limit" in classify_llm_error(Exception("429 too many requests")).lower()

    def test_timeout(self):
        assert "timed out" in classify_llm_error(Exception("request timed out")).lower()

    def test_unknown_passthrough(self):
        assert classify_llm_error(Exception("weird boom")) == "weird boom"


# ----------------------------------------------------------------------
# Auto-fallback
# ----------------------------------------------------------------------
class TestAutoFallback:
    async def test_falls_back_to_healthy_and_promotes(self, monkeypatch):
        svc = _service(_FakeChat(error=Exception("Error code: 401 invalid api key")))

        # One healthy candidate is known from the last probe.
        monkeypatch.setattr(
            llm_health, "cached_health",
            lambda: {"good": ModelHealth("good", "openai", "gpt", llm_health.PASS, "ok", 0.1)},
        )

        async def _fake_fetch(pool, name):
            return {"provider_name": "openai", "provider_model_identifier": "gpt", "model_name": name}

        monkeypatch.setattr(svc_mod, "_fetch_model_row", _fake_fetch)
        monkeypatch.setattr(svc_mod, "_build_chat_model", lambda row: _FakeChat(reply="HELLO"))

        ai = await svc._ainvoke_with_fallback(
            [],
            base=svc._chat_model,
            provider_name="azure_openai",
            model_name="bad",
            max_tokens=16,
            temperature=0.3,
            tools=None,
            promote=True,
        )
        assert ai.content == "HELLO"
        # The healthy model is promoted so subsequent calls skip the dead one.
        assert svc.get_deployment() == "good"

    async def test_raises_actionable_error_when_no_healthy_fallback(self, monkeypatch):
        svc = _service(_FakeChat(error=Exception("Error code: 401 invalid subscription key")))

        # Cache is non-empty (so no live probe) but contains no healthy model.
        monkeypatch.setattr(
            llm_health, "cached_health",
            lambda: {"bad": ModelHealth("bad", "azure_openai", "dep", llm_health.FAIL, "401", 0.1)},
        )

        with pytest.raises(LLMUnavailableError) as excinfo:
            await svc._ainvoke_with_fallback(
                [],
                base=svc._chat_model,
                provider_name="azure_openai",
                model_name="bad",
                max_tokens=16,
                temperature=0.3,
                tools=None,
                promote=True,
            )
        assert "api key is invalid or expired" in str(excinfo.value).lower()

    async def test_no_fallback_when_primary_succeeds(self, monkeypatch):
        svc = _service(_FakeChat(reply="DIRECT"))

        def _should_not_run(*a, **k):
            raise AssertionError("fallback must not be consulted when the primary succeeds")

        monkeypatch.setattr(llm_health, "cached_health", _should_not_run)

        ai = await svc._ainvoke_with_fallback(
            [],
            base=svc._chat_model,
            provider_name="azure_openai",
            model_name="bad",
            max_tokens=16,
            temperature=0.3,
            tools=None,
            promote=True,
        )
        assert ai.content == "DIRECT"
        assert svc.get_deployment() == "bad"  # unchanged
