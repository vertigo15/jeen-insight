"""Engine behaviour on synthetic series: anomalies are caught at roughly the
promised rate, forecasts beat (or honestly fall back to) the baseline, metrics
follow the WAPE/MASE rule, and the envelope validates."""

from __future__ import annotations

import numpy as np
import pytest

from src.analysis.contracts import ResultEnvelope
from src.analysis.runner import execute_skill
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload, white_noise

pytestmark = pytest.mark.filterwarnings("ignore")


def _run(skill, params, payload, **kw):
    out = execute_skill(skill, params, payload, **kw)
    assert out.status == "ok", (out.status, out.error, [g.detail for g in out.guard_results if not g.passed])
    assert isinstance(out.envelope, ResultEnvelope)
    return out.envelope


# ── Anomaly detection ─────────────────────────────────────────────────────────


def test_anomaly_detection_flags_injected_spikes_on_seasonal_history():
    idx, y = seasonal_series(n=130, grain="week", anomalies=(40, 88))
    env = _run("anomaly_detection", {"series": series_request()}, to_payload(idx, y))
    flagged = {f["ts"] for f in env.facts["flagged"]}
    assert {idx[40].date().isoformat(), idx[88].date().isoformat()} <= flagged
    # Roughly the promised rate: 2 injected + ~5% noise on 130 points.
    assert env.facts["n_flagged"] <= 2 + int(0.10 * 130)
    assert env.details.seasonal_periods == [52]
    assert env.validation.metric == "WAPE" and env.validation.band == "good"
    assert env.validation.coverage is not None and env.validation.coverage_n == 130
    assert env.headline.endswith("fall outside the expected range.")
    assert env.columns == ["ts", "actual", "expected", "lower", "upper", "score", "is_anomaly", "observed"]
    assert len(env.rows) == 130 and sum(r["is_anomaly"] for r in env.rows) == env.facts["n_flagged"]
    assert env.egress.tier == "A" and env.egress.rows_sent_to_model == 130
    assert env.chart_spec.chart_type == "band"
    roles = [s.role for s in env.chart_spec.series]
    assert roles == ["actual", "expected", "interval", "flagged"]


def test_anomaly_detection_clean_series_flags_about_one_in_twenty():
    idx, y = seasonal_series(n=130, grain="week", seed=3)
    env = _run("anomaly_detection", {"series": series_request(), "sensitivity": 0.95}, to_payload(idx, y))
    rate = env.facts["n_flagged"] / env.facts["n_points"]
    assert rate <= 0.12
    assert env.facts["expected_coverage"] == 0.95
    stricter = _run("anomaly_detection", {"series": series_request(), "sensitivity": 0.99}, to_payload(idx, y))
    assert stricter.facts["n_flagged"] <= env.facts["n_flagged"]


def test_anomaly_detection_without_seasonality_uses_trend_and_says_so():
    idx, y = seasonal_series(n=30, grain="week", amplitude=0, anomalies=(12,), anomaly_factor=1.6)
    env = _run("anomaly_detection", {"series": series_request()}, to_payload(idx, y))
    assert env.details.seasonal_periods == []
    assert "LOWESS" in env.method_used
    assert any("No seasonal pattern" in c for c in env.caveats)
    assert idx[12].date().isoformat() in {f["ts"] for f in env.facts["flagged"]}


def test_anomaly_detection_sigma3_is_labelled_non_robust():
    idx, y = seasonal_series(n=60, grain="week", amplitude=0, anomalies=(20,), anomaly_factor=2.0)
    env = _run("anomaly_detection", {"series": series_request(), "method": "sigma3"}, to_payload(idx, y))
    assert env.method_used.startswith("3-sigma")
    assert any("3-sigma" in n for n in env.details.notes)
    assert env.details.params_used["k"] == 3.0


def test_anomaly_detection_series_with_negatives_uses_no_wape():
    idx, y = seasonal_series(n=40, grain="week", level=0, amplitude=500, slope=0)
    assert (y < 0).any()
    env = _run("anomaly_detection", {"series": series_request()}, to_payload(idx, y))
    assert env.validation.metric == "MASE" and env.validation.value is None
    assert "zeros or negatives" in env.validation.basis


# ── Forecast ──────────────────────────────────────────────────────────────────


def test_forecast_beats_seasonal_naive_on_seasonal_history():
    idx, y = seasonal_series(n=130, grain="week")
    env = _run("forecast", {"series": series_request(), "horizon": 8}, to_payload(idx, y))
    baseline = next(c for c in env.details.candidates if c.is_baseline)
    winner = next(c for c in env.details.candidates if c.selected)
    assert baseline.name == "SeasonalNaive"
    assert winner.value is not None and baseline.value is not None and winner.value < baseline.value
    assert env.method_used == winner.name and env.method_used != "SeasonalNaive"
    assert env.validation.metric == "WAPE" and "rolling-origin CV" in env.validation.basis
    assert env.validation.coverage_n == 24  # 3 windows × h=8
    forecast_rows = [r for r in env.rows if r["is_forecast"]]
    assert len(forecast_rows) == 8 and all(r["lower"] <= r["forecast"] <= r["upper"] for r in forecast_rows)
    assert env.chart_spec.forecast_start == forecast_rows[0]["ts"]
    assert env.facts["forecast_end"]["ts"] == forecast_rows[-1]["ts"]
    assert any("direction, not a number" in c for c in env.caveats)
    assert env.headline.startswith("SUM(Profit) is projected at")


def test_forecast_falls_back_to_baseline_and_says_so():
    # A pure random walk: nothing should reliably beat Naive; when the shortlist
    # does not, the engine returns the baseline and explains why.
    rng = np.random.default_rng(11)
    idx, _ = white_noise(n=60, grain="week")
    y = 1000 + np.cumsum(rng.normal(0, 30, 60))
    env = _run("forecast", {"series": series_request(), "method": "seasonal_naive", "horizon": 4}, to_payload(idx, y))
    assert env.method_used == "Naive"
    assert len(env.details.candidates) == 1 and env.details.candidates[0].is_baseline


def test_forecast_explicit_method_is_respected():
    idx, y = seasonal_series(n=48, grain="month")
    env = _run("forecast", {"series": series_request(grain="month"), "method": "auto_ets", "horizon": 6}, to_payload(idx, y))
    names = {c.name for c in env.details.candidates}
    assert names == {"SeasonalNaive", "AutoETS"}
    assert env.method_used in names


def test_forecast_horizon_guard_refuses_and_override_marks_low_confidence():
    idx, y = seasonal_series(n=62, grain="day", amplitude=0)
    payload = to_payload(idx, y)
    params = {"series": series_request(grain="day"), "horizon": 90}
    refused = execute_skill("forecast", params, payload)
    assert refused.status == "guard_failed"
    bad = [g for g in refused.guard_results if not g.passed]
    assert [g.name for g in bad] == ["max_horizon"]
    assert bad[0].exits[0].params_patch == {"horizon": 20} and bad[0].exits[0].recommended

    forced = execute_skill("forecast", params, payload, override_guards=True)
    assert forced.status == "ok" and forced.envelope.low_confidence is True
    assert any("overridden" in c for c in forced.envelope.caveats)
    assert forced.envelope.artifact_view()["low_confidence"] is True


def test_forecast_non_additive_with_gap_keeps_running_when_within_limit():
    idx, y = seasonal_series(n=48, grain="month")
    payload = to_payload(idx, y, drop=(10,))
    env = _run("forecast", {"series": series_request(grain="month", agg="avg"), "horizon": 6}, payload)
    assert env.provenance.periods_filled == 1 and "left empty" in env.provenance.missing_policy


# ── execute_skill error paths ─────────────────────────────────────────────────


def test_execute_skill_reports_invalid_requests_without_raising():
    out = execute_skill("clustering", {}, {"rows": []})
    assert out.status == "error" and "invalid analysis request" in out.error
    out = execute_skill("forecast", {"series": series_request(), "horizon": 0}, {"rows": []})
    assert out.status == "error"
    out = execute_skill("forecast", {"series": series_request()}, {"rows": []})
    assert out.status == "error" and "no rows" in out.error
