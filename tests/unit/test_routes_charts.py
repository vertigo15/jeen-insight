"""Tests for `src.api.routes.charts` validation paths.

The full LLM round-trip is covered by smoke tests against the running
container; here we only verify the cheap pre-LLM validation checks so a
fresh contributor breaking input validation gets a fast signal.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

from src.api.routes import charts


def _valid_columns():
    return [{"name": "x", "type": "string"}, {"name": "y", "type": "number"}]


def _valid_payload(**overrides):
    payload = {
        "connection": "sales_db",
        "instruction": "add data labels",
        "current_config": {
            "xAxis": {"data": ["A", "B"]},
            "yAxis": {"type": "value"},
            "series": [{"type": "bar", "data": [1, 2]}],
        },
        "columns": _valid_columns(),
        "column_names": ["x", "y"],
        "sample_data": [["A", 1], ["B", 2]],
    }
    payload.update(overrides)
    return payload


def test_edit_chart_rejects_empty_instruction(client, fake_state):
    resp = client.post(
        "/api/edit-chart", json=_valid_payload(instruction="   ")
    )
    assert resp.status_code == 400
    assert "instruction" in resp.json()["detail"]


def test_edit_chart_rejects_missing_current_config(client, fake_state):
    resp = client.post(
        "/api/edit-chart", json=_valid_payload(current_config={})
    )
    assert resp.status_code == 400
    assert "current_config" in resp.json()["detail"]


def test_edit_chart_returns_503_when_registry_missing(client, empty_state):
    resp = client.post("/api/edit-chart", json=_valid_payload())
    assert resp.status_code == 503


def _agent_response(fake_state, payload):
    agent = type("Agent", (), {})()
    agent.llm = type("Llm", (), {})()
    agent.llm.generate = AsyncMock(return_value={"content": json.dumps(payload)})
    fake_state.agent_registry.get_agent = AsyncMock(return_value=agent)
    return agent


def test_standard_edit_is_session_only_and_returns_current_spec(client, fake_state):
    current = _valid_payload()["current_config"]
    candidate = json.loads(json.dumps(current))
    candidate["series"][0]["itemStyle"] = {"color": "#4455aa"}
    _agent_response(fake_state, {
        "chart_config": candidate,
        "chart_type": "bar",
        "notes": "Changed the color.",
        "out_of_scope": False,
    })
    fake_state.history_service.persistence_enabled = True
    fake_state.history_service.query_belongs_to_user = AsyncMock(return_value=True)
    fake_state.history_service.upsert_turn_chart = AsyncMock(return_value=True)
    spec = {
        "chart_type": "bar", "x": "x", "y": ["y"], "series": None,
        "aggregate": "sum", "sort": "none",
    }

    response = client.post(
        "/api/edit-chart",
        json=_valid_payload(chart_spec=spec, query_id=str(uuid4())),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["out_of_scope"] is False
    assert body["chart_config"]["series"][0]["itemStyle"]["color"] == "#4455aa"
    assert body["chart_spec"] == spec
    fake_state.history_service.upsert_turn_chart.assert_not_called()


def test_flat_point_map_style_edit_preserves_map_semantics(client, fake_state):
    current = {
        "geo": {"map": "world", "roam": True},
        "jeenMap": {"mode": "points", "mapName": "world"},
        "series": [{
            "name": "revenue",
            "type": "scatter",
            "coordinateSystem": "geo",
            "data": [[34.8, 32.1, 10]],
        }],
    }
    candidate = json.loads(json.dumps(current))
    candidate["series"][0]["symbolSize"] = 16
    _agent_response(fake_state, {
        "chart_config": candidate,
        "chart_type": "map",
        "out_of_scope": False,
    })
    payload = _valid_payload(
        current_config=current,
        chart_spec={"chart_type": "map", "location": "x", "value": "y"},
    )

    response = client.post("/api/edit-chart", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["out_of_scope"] is False
    assert body["chart_type"] == "map"
    assert body["chart_config"]["series"][0]["symbolSize"] == 16


def test_osm_edit_is_session_only(client, fake_state):
    _agent_response(fake_state, {
        "spec_patch": {},
        "view_commands": [{"op": "fit_extent"}],
        "out_of_scope": False,
    })
    fake_state.history_service.persistence_enabled = True
    fake_state.history_service.query_belongs_to_user = AsyncMock(return_value=True)
    fake_state.history_service.upsert_turn_chart = AsyncMock(return_value=True)
    payload = _valid_payload(
        current_config={"jeenOsmMap": {"overlays": []}},
        chart_spec={
            "chart_type": "osm_map", "x": "x", "y": ["y"],
            "location": "x", "value": "y",
        },
        query_id=str(uuid4()),
    )

    response = client.post("/api/edit-chart", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["view_commands"] == [{"op": "fit_extent"}]
    fake_state.history_service.upsert_turn_chart.assert_not_called()


def test_semantic_edit_rebuilds_config_and_coherent_spec(
    client, fake_state, monkeypatch
):
    _agent_response(fake_state, {
        "chart_config": _valid_payload()["current_config"],
        "chart_type": "line",
        "spec_patch": {"chart_type": "line", "sort": "desc"},
        "out_of_scope": False,
    })
    dataset = {"columns": ["x", "y"], "rows": [["A", 1], ["B", 2]]}
    monkeypatch.setattr(charts.result_cache, "get", lambda **_kwargs: dataset)
    spec = {
        "chart_type": "bar", "x": "x", "y": ["y"], "series": None,
        "aggregate": "sum", "sort": "none", "title": "Y by X",
    }

    response = client.post("/api/edit-chart", json=_valid_payload(chart_spec=spec))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["out_of_scope"] is False
    assert body["rebuild_required"] is True
    assert body["chart_spec"]["chart_type"] == "line"
    assert body["chart_spec"]["sort"] == "desc"
    assert body["chart_config"]["series"][0]["type"] == "line"
    assert body["chart_config"]["xAxis"]["data"] == ["B", "A"]


def test_semantic_edit_requests_full_rows_after_cache_miss(
    client, fake_state, monkeypatch
):
    current = _valid_payload()["current_config"]
    _agent_response(fake_state, {
        "chart_config": current,
        "spec_patch": {"chart_type": "line"},
        "out_of_scope": False,
    })
    monkeypatch.setattr(charts.result_cache, "get", lambda **_kwargs: None)
    spec = {"chart_type": "bar", "x": "x", "y": ["y"]}

    response = client.post("/api/edit-chart", json=_valid_payload(chart_spec=spec))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "cache_miss"


def test_semantic_edit_rejects_unknown_binding(client, fake_state, monkeypatch):
    current = _valid_payload()["current_config"]
    _agent_response(fake_state, {
        "chart_config": current,
        "spec_patch": {"y": ["invented_revenue"]},
        "out_of_scope": False,
    })
    monkeypatch.setattr(
        charts.result_cache,
        "get",
        lambda **_kwargs: {"columns": ["x", "y"], "rows": [["A", 1], ["B", 2]]},
    )

    response = client.post(
        "/api/edit-chart",
        json=_valid_payload(
            chart_spec={"chart_type": "bar", "x": "x", "y": ["y"]}
        ),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["out_of_scope"] is True
    assert body["error"]["code"] == "unknown_measure"
    assert body["chart_config"] == current


def test_invalid_model_config_returns_unchanged_chart_with_error(client, fake_state):
    current = _valid_payload()["current_config"]
    candidate = json.loads(json.dumps(current))
    candidate["graphic"] = [{"type": "image", "style": {"image": "https://bad.invalid/x"}}]
    _agent_response(fake_state, {
        "chart_config": candidate,
        "chart_type": "bar",
        "out_of_scope": False,
    })

    response = client.post(
        "/api/edit-chart",
        json=_valid_payload(chart_spec={"chart_type": "bar", "x": "x", "y": ["y"]}),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["out_of_scope"] is True
    assert body["error"]["code"] == "unsafe_surface"
    assert body["chart_config"] == current


# ── chart baseline persistence vs. the streaming race ─────────────────────────

def _history_stub(upsert_results):
    history = type("History", (), {})()
    history.persistence_enabled = True
    history.upsert_turn_chart = AsyncMock(side_effect=list(upsert_results))
    history.clear_turn_chart = AsyncMock(return_value=True)
    return history


def test_persist_chart_baseline_retries_until_the_turn_artifact_exists(monkeypatch):
    """With progressive answers the browser asks for the chart before the
    graph's save_to_memory has written the turn artifact, so the first UPDATE
    matches no row. The chart must still be attached once the row appears."""
    import asyncio

    history = _history_stub([False, False, True])
    monkeypatch.setattr(charts, "get_history_service", lambda: history)
    monkeypatch.setattr(charts, "_CHART_PERSIST_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0))

    async def run():
        await charts._persist_chart_baseline(
            query_id=str(uuid4()), user_id="u", chart_spec={"chart_type": "bar"},
            chart_config={"series": []},
        )
        # The retry runs detached from the request; let it finish.
        for _ in range(20):
            if not charts._CHART_PERSIST_RETRIES:
                break
            await asyncio.sleep(0)
        await asyncio.gather(*charts._CHART_PERSIST_RETRIES)

    asyncio.run(run())
    assert history.upsert_turn_chart.await_count == 3
    assert not charts._CHART_PERSIST_RETRIES


def test_persist_chart_baseline_gives_up_after_the_backoff_window(monkeypatch):
    import asyncio

    history = _history_stub([False] * 10)
    monkeypatch.setattr(charts, "get_history_service", lambda: history)
    monkeypatch.setattr(charts, "_CHART_PERSIST_RETRY_DELAYS", (0.0, 0.0))

    async def run():
        await charts._persist_chart_baseline(
            query_id=str(uuid4()), user_id="u", chart_spec={"chart_type": "bar"},
            chart_config={"series": []},
        )
        await asyncio.gather(*charts._CHART_PERSIST_RETRIES)

    asyncio.run(run())
    # 1 immediate attempt + one per backoff step, then stop.
    assert history.upsert_turn_chart.await_count == 3


def test_persist_chart_baseline_does_not_retry_when_stored_first_time(monkeypatch):
    import asyncio

    history = _history_stub([True])
    monkeypatch.setattr(charts, "get_history_service", lambda: history)

    asyncio.run(charts._persist_chart_baseline(
        query_id=str(uuid4()), user_id="u", chart_spec={"chart_type": "bar"},
        chart_config={"series": []},
    ))
    assert history.upsert_turn_chart.await_count == 1
    assert not charts._CHART_PERSIST_RETRIES
