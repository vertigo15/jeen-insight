"""Unit tests for the constrained OpenStreetMap chart-edit path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.api.models import ColumnInfo, EditChartRequest
from src.api.routes import charts


class _Llm:
    async def generate(self, **_kwargs):
        return {
            "content": """
            {
              "spec_patch": {
                "map_palette": "green",
                "data_layer_mode": "clusters",
                "value": "__row_count__",
                "value2": null
              },
              "view_commands": [
                {"op": "set_basemap", "layer_id": "standard"},
                {"op": "set_overlays", "layer_ids": ["ports-harbors", "not-allowed"]},
                {"op": "set_user_data_visible", "visible": true},
                {"op": "focus_place", "query": "Port of Haifa"}
              ],
              "notes": "Clustered the data and opened the port reference layer.",
              "out_of_scope": false
            }
            """,
        }


def _request():
    config = {
        "jeenOsmMap": {
            "overlays": [{"points": []}],
        },
    }
    return EditChartRequest(
        connection="sales_db",
        instruction="show clusters, use green, and show the ports around Haifa",
        current_config=config,
        columns=[
            ColumnInfo(name="city", type="string"),
            ColumnInfo(name="latitude", type="number"),
            ColumnInfo(name="longitude", type="number"),
            ColumnInfo(name="sales", type="number"),
            ColumnInfo(name="orders", type="number"),
        ],
        column_names=["city", "latitude", "longitude", "sales", "orders"],
        sample_data=[["Haifa", 32.8, 35.0, 10, 2]],
        chart_spec={
            "chart_type": "osm_map",
            "x": "city",
            "y": ["sales", "orders"],
            "location": "city",
            "latitude": "latitude",
            "longitude": "longitude",
            "value": "sales",
            "value2": "orders",
            "aggregate": "sum",
            "map_palette": "blue",
            "data_layer_mode": "auto",
        },
        query_id="query-1",
    )


def test_map_edit_rebuilds_only_from_validated_spec_and_view_commands(monkeypatch):
    request = _request()
    dataset = {
        "columns": request.column_names,
        "rows": [
            ["Haifa", 32.8, 35.0, 10, 2],
            ["Tel Aviv", 32.1, 34.8, 20, 4],
        ],
    }

    async def _no_op(*_args, **_kwargs):
        return None

    async def _empty_hints(*_args, **_kwargs):
        return {}

    async def _resolved(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(charts, "require_user_id", lambda _user_id: "user-a")
    monkeypatch.setattr(charts, "_verify_query_owner", _no_op)
    monkeypatch.setattr(charts.result_cache, "get", lambda **_kwargs: dataset)
    monkeypatch.setattr(charts, "_load_geo_hints", _empty_hints)
    monkeypatch.setattr(charts, "resolve_osm_locations", _resolved)
    monkeypatch.setattr(charts, "osm_maps_enabled", lambda: True)

    response = asyncio.run(
        charts._edit_osm_map_chart(request, request.instruction, SimpleNamespace(llm=_Llm()))
    )

    assert response.out_of_scope is False
    assert response.rebuild_required is True
    assert response.chart_spec["map_palette"] == "green"
    assert response.chart_spec["data_layer_mode"] == "clusters"
    assert response.chart_spec["value"] == "__row_count__"
    assert response.chart_spec["value2"] is None
    assert response.chart_spec["aggregate"] == "count"
    assert response.chart_config["jeenOsmMap"]["overlays"][0]["palette"] == "green"
    assert response.chart_config["jeenOsmMap"]["dataLayerMode"] == "clusters"
    assert response.view_commands == [
        {"op": "set_basemap", "layer_id": "standard"},
        {"op": "set_overlays", "layer_ids": ["ports-harbors"]},
        {"op": "set_user_data_visible", "visible": True},
        {"op": "focus_place", "query": "Port of Haifa"},
    ]


def test_map_edit_command_filter_does_not_accept_unknown_layer_or_action():
    assert charts._validate_map_view_commands([
        {"op": "set_basemap", "layer_id": "untrusted"},
        {"op": "set_overlays", "layer_ids": ["untrusted"]},
        {"op": "open_url", "url": "https://example.test"},
    ]) == [{"op": "set_overlays", "layer_ids": []}]


def test_map_edit_command_filter_allows_opening_and_closing_layers():
    assert charts._validate_map_view_commands([
        {"op": "toggle_layers", "open": True},
        {"op": "toggle_layers", "open": False},
        {"op": "toggle_layers", "open": "yes"},
    ]) == [
        {"op": "toggle_layers", "open": True},
        {"op": "toggle_layers", "open": False},
    ]
