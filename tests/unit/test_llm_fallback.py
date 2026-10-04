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

    def test_response_usage_keeps_cache_and_reasoning_details(self):
        response = svc_mod._from_lc_response(SimpleNamespace(
            content="{}",
            response_metadata={"finish_reason": "stop"},
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "input_token_details": {"cache_read": 60},
                "output_token_details": {"reasoning": 12},
            },
            tool_calls=[],
        ))
        assert response["usage"]["cached_prompt_tokens"] == 60
        assert response["usage"]["reasoning_tokens"] == 12


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

    async def test_max_fallbacks_caps_provider_attempts(self, monkeypatch):
        svc = _service(_FakeChat(error=Exception("primary failed")))
        monkeypatch.setattr(
            llm_health,
            "cached_health",
            lambda: {
                "first": ModelHealth("first", "openai", "first", llm_health.PASS, "ok", 0.1),
                "second": ModelHealth("second", "openai", "second", llm_health.PASS, "ok", 0.1),
            },
        )

        async def _fake_fetch(pool, name):
            return {
                "provider_name": "openai",
                "provider_model_identifier": name,
                "model_name": name,
            }

        attempted = []

        def _fake_build(row):
            attempted.append(row["model_name"])
            return _FakeChat(
                reply="SECOND" if row["model_name"] == "second" else None,
                error=None if row["model_name"] == "second" else Exception("first failed"),
            )

        monkeypatch.setattr(svc_mod, "_fetch_model_row", _fake_fetch)
        monkeypatch.setattr(svc_mod, "_build_chat_model", _fake_build)

        with pytest.raises(LLMUnavailableError):
            await svc._ainvoke_with_fallback(
                [],
                base=svc._chat_model,
                provider_name="azure_openai",
                model_name="bad",
                max_tokens=16,
                temperature=0.3,
                tools=None,
                promote=False,
                max_fallbacks=1,
            )
        assert attempted == ["first"]


# ----------------------------------------------------------------------
# Which failures are worth a fallback
# ----------------------------------------------------------------------
class _StatusError(Exception):
    def __init__(self, status_code: int, message: str = "provider error"):
        super().__init__(message)
        self.status_code = status_code


class TestShouldFallBack:
    @pytest.mark.parametrize("status", [401, 403, 404, 408, 429, 500, 502, 503])
    def test_credential_capacity_and_outage_statuses_fall_back(self, status):
        assert svc_mod.should_fall_back(_StatusError(status)) is True

    @pytest.mark.parametrize("status", [400, 413, 422])
    def test_request_faults_do_not_fall_back(self, status):
        assert svc_mod.should_fall_back(_StatusError(status)) is False

    def test_status_is_read_from_the_error_text(self):
        assert svc_mod.should_fall_back(Exception("Error code: 400 - bad request")) is False
        assert svc_mod.should_fall_back(Exception("Error code: 429 - slow down")) is True

    def test_content_filter_and_context_length_do_not_fall_back(self):
        assert svc_mod.should_fall_back(Exception("content_filter triggered")) is False
        assert svc_mod.should_fall_back(Exception("maximum context length exceeded")) is False

    def test_timeouts_and_connection_errors_fall_back(self):
        import asyncio

        assert svc_mod.should_fall_back(asyncio.TimeoutError()) is True
        assert svc_mod.should_fall_back(ConnectionError("reset")) is True

    def test_programming_errors_do_not_fall_back(self):
        assert svc_mod.should_fall_back(KeyError("endpoint")) is False
        assert svc_mod.should_fall_back(TypeError("bad arg")) is False

    def test_unknown_provider_errors_still_fall_back(self):
        assert svc_mod.should_fall_back(Exception("weird boom")) is True


def _invoke_kwargs(svc):
    return dict(
        base=svc._chat_model,
        provider_name="azure_openai",
        model_name="bad",
        max_tokens=16,
        temperature=0.3,
        tools=None,
        promote=False,
    )


class TestFallbackEligibility:
    async def test_a_bad_request_is_not_retried_on_other_models(self, monkeypatch):
        svc = _service(_FakeChat(error=_StatusError(400, "Error code: 400 - invalid request")))

        def _must_not_probe():
            raise AssertionError("no fallback candidates should be consulted for a 400")

        monkeypatch.setattr(llm_health, "cached_health", _must_not_probe)

        with pytest.raises(LLMUnavailableError) as excinfo:
            await svc._ainvoke_with_fallback([], **_invoke_kwargs(svc))
        assert "rejected the request" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, _StatusError)

    async def test_an_auth_failure_still_falls_back(self, monkeypatch):
        svc = _service(_FakeChat(error=_StatusError(401, "Error code: 401")))
        monkeypatch.setattr(
            llm_health, "cached_health",
            lambda: {"good": ModelHealth("good", "openai", "gpt", llm_health.PASS, "ok", 0.1)},
        )

        async def _fake_fetch(pool, name):
            return {"provider_name": "openai", "provider_model_identifier": "gpt", "model_name": name}

        monkeypatch.setattr(svc_mod, "_fetch_model_row", _fake_fetch)
        monkeypatch.setattr(svc_mod, "_build_chat_model", lambda row: _FakeChat(reply="OK"))

        ai = await svc._ainvoke_with_fallback([], **_invoke_kwargs(svc))
        assert ai.content == "OK"


class TestClientCache:
    def _wire(self, monkeypatch, *, failing: set[str] = frozenset()):
        monkeypatch.setattr(
            llm_health, "cached_health",
            lambda: {"good": ModelHealth("good", "openai", "gpt", llm_health.PASS, "ok", 0.1)},
        )
        fetches: list[str] = []
        builds: list[str] = []

        async def _fake_fetch(pool, name):
            fetches.append(name)
            return {"provider_name": "openai", "provider_model_identifier": name, "model_name": name}

        def _fake_build(row):
            builds.append(row["model_name"])
            if row["model_name"] in failing:
                return _FakeChat(error=Exception("candidate down"))
            return _FakeChat(reply="OK")

        monkeypatch.setattr(svc_mod, "_fetch_model_row", _fake_fetch)
        monkeypatch.setattr(svc_mod, "_build_chat_model", _fake_build)
        return fetches, builds

    async def test_a_fallback_client_is_built_once_and_reused(self, monkeypatch):
        svc = _service(_FakeChat(error=_StatusError(429, "Error code: 429")))
        fetches, builds = self._wire(monkeypatch)

        for _ in range(3):
            ai = await svc._ainvoke_with_fallback([], **_invoke_kwargs(svc))
            assert ai.content == "OK"
        assert fetches == ["good"]
        assert builds == ["good"]

    async def test_a_failing_client_is_evicted_and_rebuilt(self, monkeypatch):
        svc = _service(_FakeChat(error=_StatusError(429, "Error code: 429")))
        fetches, builds = self._wire(monkeypatch, failing={"good"})

        for _ in range(2):
            with pytest.raises(LLMUnavailableError):
                await svc._ainvoke_with_fallback([], **_invoke_kwargs(svc))
        # Evicted after the first failure, so the second attempt rebuilt it.
        assert builds == ["good", "good"]

    async def test_cache_entries_expire(self, monkeypatch):
        cache = svc_mod._ChatClientCache(ttl_seconds=10)
        clock = {"now": 1000.0}
        monkeypatch.setattr(svc_mod.time, "monotonic", lambda: clock["now"])
        cache.put(("name", "m"), "client")
        assert cache.get(("name", "m")) == "client"
        clock["now"] += 11
        assert cache.get(("name", "m")) is None

    async def test_set_model_replaces_the_cached_client(self, monkeypatch):
        svc = _service(_FakeChat(reply="OLD"))
        fetches, builds = self._wire(monkeypatch)
        svc._clients.put(("name", "good"), (_FakeChat(reply="STALE"), "openai"))

        await svc.set_model("good")
        base, _provider = svc._clients.get(("name", "good"))
        assert base is svc._chat_model
