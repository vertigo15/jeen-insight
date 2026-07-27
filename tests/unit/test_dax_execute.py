"""Unit tests for pbi_execute_query + result_integrity_check.

The token provider and Power BI client are faked, so these tests exercise the
node's control flow only: transport handling (401 refresh + replay), the
needs-connect / fatal short-circuits, success mapping, and the one-shot
empty-result diagnostic budget.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.agent.langgraph_agent_dax.nodes.dax_execute as ex
from src.agent.langgraph_agent_dax.nodes.dax_execute import (
    make_pbi_execute_query,
    result_integrity_check,
)
from src.connectors.powerbi_token import PowerBiTokenError


class _Provider:
    def __init__(self, token="tok", error=None):
        self._token = token
        self._error = error
        self.calls = []

    async def get_token_for_auth_user(self, auth_user_id, *, force_refresh=False):
        self.calls.append(force_refresh)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(access_token=self._token)


class _Client:
    """Fake PowerBiDaxClient: returns queued results in order."""

    def __init__(self, results, raise_on_init=None):
        self._results = list(results)
        self.exec_calls = 0

    async def execute_dax(self, dax, token, *, max_rows=None):
        self.exec_calls += 1
        return self._results.pop(0) if self._results else {"error": "no result", "error_type": "service"}


def _state(**overrides):
    st = {
        "generated_dax": "EVALUATE Sales",
        "workspace_id": "ws",
        "dataset_id": "ds",
        "user_id": "42",
    }
    st.update(overrides)
    return st


def _wire(monkeypatch, provider, client):
    monkeypatch.setattr(ex, "_token_provider", lambda: provider)
    monkeypatch.setattr(ex, "PowerBiDaxClient", lambda **kw: client)


class TestExecute:
    async def test_no_provider_is_hard_terminal(self, monkeypatch):
        monkeypatch.setattr(ex, "_token_provider", lambda: None)
        out = await make_pbi_execute_query()(_state())
        assert out["dax_terminal"] is True
        assert out["needs_connect"] is False  # hard config error, not a connect prompt
        assert out["answer"]

    async def test_token_error_needs_connect(self, monkeypatch):
        provider = _Provider(error=PowerBiTokenError("connect me", needs_connect=True))
        _wire(monkeypatch, provider, _Client([]))
        out = await make_pbi_execute_query()(_state())
        assert out["dax_terminal"] is True
        assert out["needs_connect"] is True

    async def test_success_maps_result(self, monkeypatch):
        provider = _Provider()
        client = _Client([
            {"columns": ["Region"], "rows": [{"Region": "EU"}], "row_count": 1, "is_partial": False}
        ])
        _wire(monkeypatch, provider, client)
        out = await make_pbi_execute_query()(_state())
        assert out["exec_error"] is None
        assert out["query_result"]["row_count"] == 1
        assert out["query_result"]["columns"] == ["Region"]
        assert out["http_status"] == 200
        assert client.exec_calls == 1

    async def test_401_refreshes_and_replays(self, monkeypatch):
        provider = _Provider()
        client = _Client([
            {"error": "unauthorized", "error_type": "auth"},
            {"columns": ["X"], "rows": [{"X": 1}], "row_count": 1},
        ])
        _wire(monkeypatch, provider, client)
        out = await make_pbi_execute_query()(_state())
        assert out["exec_error"] is None
        assert out["query_result"]["row_count"] == 1
        assert client.exec_calls == 2
        assert provider.calls == [False, True]  # second call forced a refresh

    async def test_delegated_provider_is_used_when_legacy_tokens_exist(self, monkeypatch):
        """DAX execution must never bypass the signed-in user's Power BI grant."""
        delegated = _Provider()
        client = _Client([
            {"columns": ["X"], "rows": [{"X": 1}], "row_count": 1}
        ])
        monkeypatch.setattr(ex.settings, "POWERBI_TEST_ACCESS_TOKEN", "legacy-token", raising=False)
        _wire(monkeypatch, delegated, client)

        out = await make_pbi_execute_query()(_state())
        assert out["exec_error"] is None
        assert out["query_result"]["row_count"] == 1
        assert delegated.calls == [False]

    async def test_execution_error_surfaces_to_feedback(self, monkeypatch):
        provider = _Provider()
        client = _Client([
            {"error": "Cannot find column 'X'", "error_type": "execution_error", "http_status": 400}
        ])
        _wire(monkeypatch, provider, client)
        out = await make_pbi_execute_query()(_state())
        assert out["exec_error"] == "Cannot find column 'X'"
        assert out["error_context"].startswith("Power BI DAX error")
        assert out.get("dax_terminal") is not True
        assert client.exec_calls == 1

    async def test_misconfigured_client_is_fatal(self, monkeypatch):
        provider = _Provider()
        monkeypatch.setattr(ex, "_token_provider", lambda: provider)

        def _boom(**kw):
            raise ValueError("missing dataset id")

        monkeypatch.setattr(ex, "PowerBiDaxClient", _boom)
        out = await make_pbi_execute_query()(_state())
        assert out["dax_terminal"] is True
        assert "misconfigured" in out["error"]


class TestIntegrity:
    def test_empty_triggers_one_diagnostic(self):
        out = result_integrity_check(
            {"query_result": {"columns": [], "rows": [], "row_count": 0}}
        )
        assert out["is_empty"] is True
        assert out["integrity_action"] == "empty_diagnostic"
        assert out["empty_diagnostics"] == 1
        assert out["dax_error_category"] == "EMPTY_OR_BLANK"

    def test_empty_after_budget_accepts(self):
        out = result_integrity_check(
            {"query_result": {"rows": [], "row_count": 0}, "empty_diagnostics": 1}
        )
        assert out["is_empty"] is True
        assert out["integrity_action"] is None

    def test_nonempty_no_action(self):
        out = result_integrity_check(
            {"query_result": {"columns": ["a"], "rows": [{"a": 1}], "row_count": 1}}
        )
        assert out["is_empty"] is False
        assert out["integrity_action"] is None
        assert out["returned_row_count"] == 1
