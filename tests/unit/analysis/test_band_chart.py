"""The band chart is built deterministically from the envelope's role-based
spec: roles are carried as ``jeenRole`` for the client's token mapping, the
interval is a stacked pair with true bounds exposed, and no LLM is involved."""

from __future__ import annotations

import pytest

from src.api.chart_builder import build_band_option, build_chart_option
from src.analysis.runner import execute_skill
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload

pytestmark = pytest.mark.filterwarnings("ignore")


def _anomaly_envelope():
    idx, y = seasonal_series(n=60, grain="week", amplitude=0, anomalies=(20,), anomaly_factor=1.8)
    out = execute_skill("anomaly_detection", {"series": series_request()}, to_payload(idx, y))
    assert out.ok
    return out.envelope


def _forecast_envelope():
    idx, y = seasonal_series(n=48, grain="month")
    out = execute_skill("forecast", {"series": series_request(grain="month"), "horizon": 6, "method": "auto_ets"}, to_payload(idx, y))
    assert out.ok
    return out.envelope


def test_anomaly_band_has_roles_interval_pair_and_flagged_points():
    env = _anomaly_envelope()
    spec = env.chart_spec.model_dump(mode="json")
    opt = build_chart_option(spec, {"columns": env.columns, "rows": env.rows})
    roles = [s.get("jeenRole") for s in opt["series"]]
    assert roles == ["interval_base", "interval", "interval_bound", "interval_bound", "expected", "actual", "flagged"]
    assert opt["xAxis"]["type"] == "time"
    base, delta = opt["series"][0], opt["series"][1]
    assert base["stack"] == delta["stack"] == "jeen-band"
    assert base["tooltip"] == {"show": False}
    # Width = upper - lower on every point.
    lows = {x: v for x, v in opt["series"][2]["data"]}
    highs = {x: v for x, v in opt["series"][3]["data"]}
    for (x, width) in delta["data"]:
        assert abs(width - (highs[x] - lows[x])) < 1e-9
    flagged = opt["series"][-1]
    assert flagged["type"] == "scatter" and len(flagged["data"]) == env.facts["n_flagged"] >= 1
    assert opt["legend"]["data"] == ["95% band", "Expected", "Actual", "Flagged"]
    assert opt["jeenBand"]["roles"] == ["actual", "expected", "interval", "flagged"]
    assert "jeenFormat" in opt


def test_forecast_band_joins_forecast_to_last_actual_and_marks_start():
    env = _forecast_envelope()
    spec = env.chart_spec.model_dump(mode="json")
    opt = build_band_option(spec, {"columns": env.columns, "rows": env.rows})
    by_role = {s["jeenRole"]: s for s in opt["series"]}
    actual, forecast = by_role["actual"], by_role["forecast"]
    assert forecast["data"][0] == actual["data"][-1], "forecast line starts at the last observed point"
    assert len(forecast["data"]) == 7  # 6 forecast points + the join
    assert forecast["lineStyle"]["type"] == "dashed"
    assert forecast["markLine"]["data"][0]["xAxis"] == spec["forecast_start"]
    assert by_role["interval"]["areaStyle"]["opacity"] == 1
    assert "flagged" not in by_role


def test_band_series_values_are_display_rounded_for_the_tooltip():
    """The model's long floats (e.g. 811,098.2338606511) are trimmed so the
    tooltip reads like the actual measure — 2 decimals for values >= 1."""
    env = _anomaly_envelope()
    spec = env.chart_spec.model_dump(mode="json")
    opt = build_band_option(spec, {"columns": env.columns, "rows": env.rows})
    checked = 0
    for s in opt["series"]:
        for point in s.get("data", []):
            y = point[-1] if isinstance(point, (list, tuple)) else point
            if not isinstance(y, (int, float)):
                continue
            expected = round(y, 2 if abs(y) >= 1 else 4)
            assert abs(y - expected) < 1e-9, f"{s.get('name')} value {y!r} not display-rounded"
            checked += 1
    assert checked > 0


def test_band_accepts_positional_rows_and_rejects_empty():
    env = _anomaly_envelope()
    spec = env.chart_spec.model_dump(mode="json")
    positional = [[r[c] for c in env.columns] for r in env.rows]
    opt = build_band_option(spec, {"columns": env.columns, "rows": positional})
    assert any(s["jeenRole"] == "actual" for s in opt["series"])
    with pytest.raises(ValueError):
        build_band_option(spec, {"columns": env.columns, "rows": []})
    with pytest.raises(ValueError):
        build_band_option({"chart_type": "band", "series": []}, {"columns": ["ts"], "rows": [{"ts": "2026-01-01"}]})
