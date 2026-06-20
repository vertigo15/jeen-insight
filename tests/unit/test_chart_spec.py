"""Unit tests for the chart visualization-spec validator.

`_validate_chart_spec` is the safety net that turns a possibly-vague or wrong
LLM response into a spec the client can always render: a real x dimension, at
least one numeric measure, and enum values clamped to the allowed sets.
"""

from __future__ import annotations

from src.api.routes.charts import _validate_chart_spec

COLUMN_NAMES = ["region", "product", "revenue", "sold_at", "year", "month"]
NUMERIC_COLS = ["revenue", "year", "month"]
DATE_COLS = ["sold_at"]


def _call(spec, **overrides):
    return _validate_chart_spec(
        spec,
        column_names=COLUMN_NAMES,
        numeric_cols=NUMERIC_COLS,
        date_cols=DATE_COLS,
        forced_type=overrides.get("chart_type"),
        x_col=overrides.get("x_column"),
        y_col=overrides.get("y_column"),
        series_col=overrides.get("series_column"),
    )


def test_valid_spec_passes_through():
    spec = _call({
        "chart_type": "bar",
        "x": "region",
        "y": ["revenue"],
        "series": "product",
        "aggregate": "sum",
        "sort": "desc",
        "top_n": 15,
        "value_format": "currency",
    })
    assert spec["chart_type"] == "bar"
    assert spec["x"] == "region"
    assert spec["y"] == ["revenue"]
    assert spec["series"] == "product"
    assert spec["aggregate"] == "sum"
    assert spec["sort"] == "desc"
    assert spec["top_n"] == 15
    assert spec["value_format"] == "currency"


def test_canonicalizes_column_case():
    spec = _call({"x": "REGION", "y": ["Revenue"]})
    assert spec["x"] == "region"
    assert spec["y"] == ["revenue"]


def test_drops_unknown_and_non_numeric_columns():
    spec = _call({"x": "made_up", "y": ["region", "ghost"]})
    # Unknown x falls back to a date/category column; y falls back to a numeric.
    assert spec["x"] in {"sold_at", "region", "product"}
    assert spec["y"] == ["revenue"]


def test_invalid_enums_are_clamped():
    spec = _call({
        "x": "region",
        "y": ["revenue"],
        "chart_type": "spaghetti",
        "aggregate": "median",
        "sort": "sideways",
        "value_format": "klingon",
        "top_n": -4,
    })
    assert spec["chart_type"] == "bar"
    assert spec["aggregate"] == "sum"
    assert spec["sort"] == "none"
    assert spec["value_format"] == "number"
    assert spec["top_n"] is None


def test_empty_spec_gets_sensible_defaults():
    spec = _call({})
    assert spec["x"] in {"sold_at", "region", "product"}
    assert spec["y"] == ["revenue"]
    assert spec["chart_type"] == "bar"


def test_user_mapping_overrides_llm():
    spec = _call(
        {"chart_type": "pie", "x": "region", "y": ["revenue"], "series": "product"},
        chart_type="line", x_column="product", y_column="revenue", series_column="region",
    )
    assert spec["chart_type"] == "line"
    assert spec["x"] == "product"
    assert spec["series"] == "region"


def test_series_cannot_equal_x():
    spec = _call({"x": "region", "y": ["revenue"], "series": "region"})
    assert spec["series"] is None


def test_x_parts_kept_when_two_valid_columns():
    spec = _call({"x": "year", "y": ["revenue"], "x_parts": ["year", "month"]})
    assert spec["x_parts"] == ["year", "month"]


def test_x_parts_dropped_when_single_column():
    spec = _call({"x": "region", "y": ["revenue"], "x_parts": ["region"]})
    assert spec["x_parts"] is None


def test_x_parts_canonicalizes_case_and_drops_unknown():
    spec = _call({"x": "year", "y": ["revenue"], "x_parts": ["YEAR", "Month", "ghost"]})
    assert spec["x_parts"] == ["year", "month"]


def test_user_x_override_clears_x_parts():
    spec = _call(
        {"x": "year", "y": ["revenue"], "x_parts": ["year", "month"]},
        x_column="region",
    )
    assert spec["x_parts"] is None
    assert spec["x"] == "region"


def test_currency_symbol_kept_only_for_currency():
    spec = _call({
        "x": "region", "y": ["revenue"],
        "value_format": "currency", "currency_symbol": "€",
    })
    assert spec["currency_symbol"] == "€"


def test_currency_symbol_dropped_for_non_currency():
    spec = _call({
        "x": "region", "y": ["revenue"],
        "value_format": "number", "currency_symbol": "$",
    })
    assert spec["currency_symbol"] == ""


def test_currency_symbol_absent_defaults_empty():
    spec = _call({"x": "region", "y": ["revenue"], "value_format": "currency"})
    assert spec["currency_symbol"] == ""
