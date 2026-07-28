"""Unit tests for PowerBiDaxClient (src/connectors/powerbi.py).

Covers the verified ``executeQueries`` constraints: single result table, HTTP-200
nested errors, partial/limit rejection, column-key normalization
(``Table[Column]`` / ``[Alias]``), and HTTP status → error_type mapping. The
egress transport is mocked so no network is touched.
"""

from __future__ import annotations

import pytest

from src.connectors import egress
from src.connectors.powerbi import PowerBiDaxClient


class _FakeResp:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _mock_egress(monkeypatch, resp):
    async def _fake_request(method, url, **kwargs):
        _fake_request.calls.append((method, url, kwargs))
        return resp
    _fake_request.calls = []
    monkeypatch.setattr(egress, "request", _fake_request)
    return _fake_request


def _client():
    return PowerBiDaxClient(workspace_id="ws-1", dataset_id="ds-1")


class TestClientConstruction:
    def test_requires_ids(self):
        with pytest.raises(ValueError):
            PowerBiDaxClient(workspace_id="", dataset_id="ds")
        with pytest.raises(ValueError):
            PowerBiDaxClient(workspace_id="ws", dataset_id="")

    def test_execute_url_shape(self):
        c = _client()
        assert c.execute_url.endswith(
            "/v1.0/myorg/groups/ws-1/datasets/ds-1/executeQueries"
        )


class TestSuccessMapping:
    async def test_maps_rows_and_friendly_column_names(self, monkeypatch):
        body = {"results": [{"tables": [{"rows": [
            {"Sales[Amount]": 10, "[Total]": 5},
            {"Sales[Amount]": 20, "[Total]": 7},
        ]}]}]}
        _mock_egress(monkeypatch, _FakeResp(200, body))
        out = await _client().execute_dax("EVALUATE Sales", "tok", max_rows=1000)
        assert out.get("error") is None
        assert out["columns"] == ["Amount", "Total"]
        assert out["rows"][0] == {"Amount": 10, "Total": 5}
        assert out["row_count"] == 2

    async def test_colliding_display_names_keep_full_keys(self, monkeypatch):
        body = {"results": [{"tables": [{"rows": [{"A[X]": 1, "B[X]": 2}]}]}]}
        _mock_egress(monkeypatch, _FakeResp(200, body))
        out = await _client().execute_dax("EVALUATE X", "tok")
        assert set(out["columns"]) == {"A[X]", "B[X]"}

    async def test_truncation_flag_when_row_count_hits_cap(self, monkeypatch):
        body = {"results": [{"tables": [{"rows": [{"[V]": i} for i in range(3)]}]}]}
        _mock_egress(monkeypatch, _FakeResp(200, body))
        out = await _client().execute_dax("EVALUATE X", "tok", max_rows=3)
        assert out["row_count"] == 3
        assert out["is_partial"] is True


class TestErrorTaxonomy:
    async def test_read_only_block_never_calls_network(self, monkeypatch):
        spy = _mock_egress(monkeypatch, _FakeResp(200, {}))
        out = await _client().execute_dax("SELECT 1", "tok")
        assert out["error_type"] == "read_only_blocked"
        assert spy.calls == []

    async def test_missing_token_is_auth_error(self, monkeypatch):
        spy = _mock_egress(monkeypatch, _FakeResp(200, {}))
        out = await _client().execute_dax("EVALUATE Sales", "")
        assert out["error_type"] == "auth"
        assert spy.calls == []

    async def test_multiple_tables_is_partial_result(self, monkeypatch):
        body = {"results": [{"tables": [{"rows": []}, {"rows": []}]}]}
        _mock_egress(monkeypatch, _FakeResp(200, body))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "partial_result"
        assert out["is_partial"] is True

    async def test_nested_200_limit_error(self, monkeypatch):
        body = {"results": [{"error": {"code": "X", "message": "More than 100000 rows"}}]}
        _mock_egress(monkeypatch, _FakeResp(200, body))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "limit_exceeded"
        assert out["http_status"] == 200

    async def test_top_level_200_error_is_execution_error(self, monkeypatch):
        body = {"error": {"code": "Y", "message": "Something went wrong"}}
        _mock_egress(monkeypatch, _FakeResp(200, body))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "execution_error"
        assert out["pbi_error_code"] == "Y"

    async def test_http_400_is_bad_request(self, monkeypatch):
        body = {"error": {"code": "BadRequest", "message": "DAX syntax error"}}
        _mock_egress(monkeypatch, _FakeResp(400, body))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "bad_request"
        assert out["http_status"] == 400
        assert out["pbi_error_code"] == "BadRequest"

    async def test_http_401_is_auth(self, monkeypatch):
        _mock_egress(monkeypatch, _FakeResp(401, {}))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "auth"

    async def test_http_403_is_forbidden(self, monkeypatch):
        _mock_egress(monkeypatch, _FakeResp(403, {}))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "forbidden"

    async def test_http_429_carries_retry_after(self, monkeypatch):
        _mock_egress(monkeypatch, _FakeResp(429, {}, {"Retry-After": "3"}))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "throttled"
        assert out["retry_after"] == 3.0

    async def test_http_500_is_service(self, monkeypatch):
        _mock_egress(monkeypatch, _FakeResp(503, {}))
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "service"

    async def test_response_too_large_maps_to_limit(self, monkeypatch):
        async def _boom(method, url, **kwargs):
            raise egress.ResponseTooLarge("too big")
        monkeypatch.setattr(egress, "request", _boom)
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "limit_exceeded"

    async def test_transport_failure_maps_to_transport(self, monkeypatch):
        async def _boom(method, url, **kwargs):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(egress, "request", _boom)
        out = await _client().execute_dax("EVALUATE Sales", "tok")
        assert out["error_type"] == "transport"
