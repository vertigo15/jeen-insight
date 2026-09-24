"""Table-driven coverage for the chart-edit trust boundary."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from src.api.chart_edit_validation import validate_chart_edit
from src.api.llm_json import extract_json_object
from src.api.models import EditChartRequest


def _bar():
    return {
        "xAxis": {"type": "category", "data": ["A", "B"]},
        "yAxis": {"type": "value"},
        "series": [{"name": "sales", "type": "bar", "data": [1, 2]}],
    }


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda c: c.update({"graphic": []}), "unsafe_surface"),
        (lambda c: c["series"][0].update({"type": "custom"}), "unsupported_chart_type"),
        (lambda c: c["series"][0].update({"data": [1, 99]}), "data_changed"),
        (
            lambda c: c["series"][0].update(
                {"label": {"formatter": "<img src=x onerror=alert(1)>"}}
            ),
            "unsafe_formatter",
        ),
        (
            lambda c: c["series"][0].update({"symbol": "image://https://bad.invalid/x"}),
            "unsafe_string",
        ),
        (
            lambda c: c.update({"tooltip": {"extraCssText": "position:fixed;inset:0"}}),
            "unsafe_surface",
        ),
        (
            lambda c: c["series"][0].update({"label": {"rich": {"x": {"color": "red"}}}}),
            "unsafe_surface",
        ),
        (lambda c: c.update({"jeenMap": {"mapName": "world"}}), "unsafe_surface"),
    ],
)
def test_rejects_unsafe_or_data_changing_configs(mutate, code):
    current = _bar()
    candidate = _bar()
    mutate(candidate)
    result = validate_chart_edit(current, candidate)
    assert result.ok is False
    assert result.code == code


def test_accepts_style_only_edit():
    current = _bar()
    candidate = _bar()
    candidate["series"][0]["itemStyle"] = {"color": "#5B4FE9", "opacity": 0.8}
    candidate["series"][0]["label"] = {"show": True, "formatter": "{c}"}
    assert validate_chart_edit(current, candidate).ok is True


def test_accepts_aligned_reordering_but_rejects_misalignment():
    current = _bar()
    reordered = _bar()
    reordered["xAxis"]["data"] = ["B", "A"]
    reordered["series"][0]["data"] = [2, 1]
    assert validate_chart_edit(current, reordered).ok is True

    reordered["series"][0]["data"] = [1, 2]
    result = validate_chart_edit(current, reordered)
    assert result.ok is False and result.code == "data_changed"


def test_accepts_data_preserving_bar_to_pie_conversion():
    candidate = {
        "series": [{
            "name": "sales",
            "type": "pie",
            "data": [{"name": "A", "value": 1}, {"name": "B", "value": 2}],
        }]
    }
    assert validate_chart_edit(_bar(), candidate).ok is True


def test_existing_flat_map_bindings_are_immutable_but_style_can_change():
    current = {
        "geo": {"map": "world", "roam": True},
        "jeenMap": {"mapName": "world", "locationColumn": "country"},
        "series": [{
            "name": "revenue",
            "type": "map",
            "map": "world",
            "data": [{"name": "A", "value": 1}, {"name": "B", "value": 2}],
        }],
    }
    candidate = {
        **current,
        "series": [{
            **current["series"][0],
            "itemStyle": {"areaColor": "#5B4FE9"},
        }],
    }
    assert validate_chart_edit(current, candidate).ok is True

    candidate["geo"] = {"map": "other", "roam": True}
    result = validate_chart_edit(current, candidate)
    assert result.ok is False and result.code == "unsafe_surface"


def test_geo_scatter_style_edit_does_not_require_cartesian_axes():
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
    candidate = {
        **current,
        "series": [{
            **current["series"][0],
            "symbolSize": 14,
        }],
    }
    assert validate_chart_edit(current, candidate).ok is True


def test_rejects_category_series_length_mismatch():
    candidate = _bar()
    candidate["series"][0]["data"] = [1]
    result = validate_chart_edit(_bar(), candidate)
    assert result.ok is False
    assert result.code == "category_series_mismatch"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_chart_model_json_rejects_non_finite_constants(constant):
    assert (
        extract_json_object(
            f'{{"chart_config": {{"value": {constant}}}}}',
            reject_non_finite=True,
        )
        is None
    )


def test_request_model_rejects_non_finite_config():
    payload = {
        "connection": "sales",
        "instruction": "style it",
        "current_config": _bar(),
        "columns": [],
        "column_names": [],
        "sample_data": [],
    }
    payload["current_config"]["series"][0]["data"][0] = math.inf
    with pytest.raises(ValidationError):
        EditChartRequest(**payload)


def test_request_model_bounds_prompt_context_and_fallback_rows():
    payload = {
        "connection": "sales",
        "instruction": "style it",
        "current_config": _bar(),
        "columns": [],
        "column_names": [],
        "sample_data": [],
    }
    with pytest.raises(ValidationError):
        EditChartRequest(**{**payload, "column_names": ["x"] * 257})
    with pytest.raises(ValidationError):
        EditChartRequest(**{
            **payload,
            "columns": [{"name": "x" * 257, "type": "string"}],
        })
    with pytest.raises(ValidationError):
        EditChartRequest(**{**payload, "sample_data": [["x" * 300_000]]})
    with pytest.raises(ValidationError):
        EditChartRequest(**{**payload, "all_data": [[1]] * 10_001})
