from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.api import state as api_state
from src.api.routes import charts


def _dataset():
    return {
        "columns": ["month", "revenue", "profit"],
        "rows": [
            ["Jan", 10, 4],
            ["Feb", 20, 7],
        ],
    }


def _payload(**overrides):
    payload = {
        "connection": "sales_db",
        "query_id": "8db89749-d716-46fd-a0d9-174275d0f733",
        "chart_kind": "sql",
        "chart_spec": {
            "chart_type": "bar",
            "x": "month",
            "y": ["revenue"],
            "series": None,
            "aggregate": "sum",
            "sort": "none",
            "title": "Revenue by month",
        },
        "operations": [{"op": "set_binding", "y": "profit"}],
    }
    payload.update(overrides)
    return payload


def _allow_owner(fake_state):
    fake_state.history_service.query_belongs_to_user = AsyncMock(return_value=True)


def test_rebuild_warm_cache_succeeds_without_llm_or_execution_services(
    client, fake_state, monkeypatch
):
    _allow_owner(fake_state)
    cache_get = MagicMock(return_value=_dataset())
    monkeypatch.setattr(charts.result_cache, "get", cache_get)

    resolve_agent = AsyncMock(
        side_effect=AssertionError("rebuild must not resolve an agent")
    )
    monkeypatch.setattr(charts, "resolve_agent", resolve_agent)
    llm = SimpleNamespace(
        generate=AsyncMock(side_effect=AssertionError("rebuild must not call an LLM"))
    )
    monkeypatch.setattr(api_state, "llm_service", llm)
    analysis_runner = MagicMock()
    registry_service = MagicMock()
    monkeypatch.setattr(api_state, "analysis_runner", analysis_runner)
    monkeypatch.setattr(api_state, "registry_service", registry_service)

    response = client.post("/api/edit-chart/rebuild", json=_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"chart_config", "chart_spec"}
    assert body["chart_spec"]["y"] == ["profit"]
    assert body["chart_config"]["series"][0]["data"] == [4.0, 7.0]
    assert body["chart_config"]["xAxis"]["data"] == ["Jan", "Feb"]
    cache_get.assert_called_once_with(
        user_id="user-a",
        connection="sales_db",
        query_id=_payload()["query_id"],
    )
    resolve_agent.assert_not_awaited()
    llm.generate.assert_not_awaited()
    assert not analysis_runner.mock_calls
    assert not registry_service.mock_calls
    timing = response.headers["server-timing"]
    for phase in ("cache", "profile", "build", "validate", "total"):
        assert f"{phase};dur=" in timing


def test_rebuild_checks_owner_before_cache(client, fake_state, monkeypatch):
    fake_state.history_service.query_belongs_to_user = AsyncMock(return_value=False)
    cache_get = MagicMock(side_effect=AssertionError("cache read before ownership"))
    monkeypatch.setattr(charts.result_cache, "get", cache_get)

    response = client.post("/api/edit-chart/rebuild", json=_payload())

    assert response.status_code == 404
    cache_get.assert_not_called()
    fake_state.history_service.query_belongs_to_user.assert_awaited_once()


def test_rebuild_cold_cache_returns_409_then_accepts_fallback(
    client, fake_state, monkeypatch
):
    _allow_owner(fake_state)
    cache_get = MagicMock(return_value=None)
    monkeypatch.setattr(charts.result_cache, "get", cache_get)

    miss = client.post("/api/edit-chart/rebuild", json=_payload())

    assert miss.status_code == 409
    assert miss.json()["detail"] == "cache_miss"
    assert "cache;dur=" in miss.headers["server-timing"]

    fallback = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(
            column_names=_dataset()["columns"],
            all_data=_dataset()["rows"],
        ),
    )

    assert fallback.status_code == 200, fallback.text
    assert fallback.json()["chart_spec"]["y"] == ["profit"]
    assert fallback.json()["chart_config"]["series"][0]["data"] == [4.0, 7.0]


def test_rebuild_switches_chart_type_from_cached_rows(
    client, fake_state, monkeypatch
):
    _allow_owner(fake_state)
    monkeypatch.setattr(charts.result_cache, "get", MagicMock(return_value=_dataset()))

    pie = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(operations=[{"op": "set_chart_type", "chart_type": "pie"}]),
    )

    assert pie.status_code == 200, pie.text
    body = pie.json()
    assert body["chart_spec"]["chart_type"] == "pie"
    assert body["chart_spec"]["y"] == ["revenue"]
    series = body["chart_config"]["series"][0]
    assert series["type"] == "pie"
    assert [item["name"] for item in series["data"]] == ["Jan", "Feb"]
    assert [item["value"] for item in series["data"]] == [10.0, 20.0]

    # Type + binding in one rebuild: the requested type wins and the new measure applies.
    horizontal = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(
            operations=[
                {"op": "set_chart_type", "chart_type": "horizontal_bar"},
                {"op": "set_binding", "y": "profit"},
            ]
        ),
    )

    assert horizontal.status_code == 200, horizontal.text
    assert horizontal.json()["chart_spec"]["chart_type"] == "horizontal_bar"
    assert horizontal.json()["chart_spec"]["y"] == ["profit"]
    assert horizontal.json()["chart_config"]["yAxis"]["data"] == ["Jan", "Feb"]

    # A binding-only rebuild keeps the chart's current type.
    binding_only = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(
            chart_spec={**_payload()["chart_spec"], "chart_type": "line"},
            operations=[{"op": "set_binding", "y": "profit"}],
        ),
    )
    assert binding_only.status_code == 200, binding_only.text
    assert binding_only.json()["chart_spec"]["chart_type"] == "line"


def test_rebuild_rejects_unknown_chart_type(client, fake_state, monkeypatch):
    cache_get = MagicMock(return_value=_dataset())
    monkeypatch.setattr(charts.result_cache, "get", cache_get)

    response = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(operations=[{"op": "set_chart_type", "chart_type": "osm_map"}]),
    )

    assert response.status_code == 422
    cache_get.assert_not_called()


def test_rebuild_rejects_non_binding_and_ml_operations(
    client, fake_state, monkeypatch
):
    cache_get = MagicMock(return_value=_dataset())
    monkeypatch.setattr(charts.result_cache, "get", cache_get)

    non_binding = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(operations=[{"op": "set_color", "target": "all", "color": "#fff"}]),
    )
    ml_kind = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(chart_kind="ml_band"),
    )

    assert non_binding.status_code == 422
    assert ml_kind.status_code == 422
    cache_get.assert_not_called()


def test_rebuild_rejects_unknown_or_non_numeric_binding(
    client, fake_state, monkeypatch
):
    _allow_owner(fake_state)
    monkeypatch.setattr(charts.result_cache, "get", MagicMock(return_value=_dataset()))

    unknown = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(operations=[{"op": "set_binding", "y": "invented"}]),
    )
    non_numeric = client.post(
        "/api/edit-chart/rebuild",
        json=_payload(operations=[{"op": "set_binding", "y": "month"}]),
    )

    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "unknown_measure"
    assert non_numeric.status_code == 422
    assert non_numeric.json()["detail"]["code"] == "unknown_measure"
