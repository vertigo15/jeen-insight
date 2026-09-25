"""Engine behaviour on synthetic series: anomalies are caught at roughly the
promised rate, forecasts beat (or honestly fall back to) the baseline, metrics
follow the WAPE/MASE rule, and the envelope validates."""

from __future__ import annotations

import numpy as np
import pandas as pd
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


def test_anomaly_detection_seasonal_method_forces_mstl_and_reports_it():
    idx, y = seasonal_series(n=130, grain="week", anomalies=(40, 88))
    env = _run("anomaly_detection", {"series": series_request(), "method": "seasonal"}, to_payload(idx, y))
    assert "MSTL" in env.method_used
    assert env.details.seasonal_periods == [52]
    assert env.facts["seasonal_detected"] is True and env.facts["seasonal_modelled"] is True
    assert {idx[40].date().isoformat(), idx[88].date().isoformat()} <= {f["ts"] for f in env.facts["flagged"]}
    assert any("detected" in c and "modelled" in c for c in env.caveats)


def test_anomaly_detection_trend_method_ignores_a_confirmed_season():
    idx, y = seasonal_series(n=130, grain="week", anomalies=(40,))
    env = _run("anomaly_detection", {"series": series_request(), "method": "trend"}, to_payload(idx, y))
    assert "LOWESS" in env.method_used
    # The season is still reported as detected, even though this model skips it.
    assert env.details.seasonal_periods == [52]
    assert env.facts["seasonal_detected"] is True and env.facts["seasonal_modelled"] is False
    assert any("detected" in c and "ignores it" in c for c in env.caveats)


def test_anomaly_detection_seasonal_method_uses_unconfirmed_candidate():
    # White noise long enough to test the 52-week candidate but with no real
    # season: an explicit seasonal choice still decomposes on the candidate.
    idx, y = white_noise(n=110, grain="week")
    env = _run("anomaly_detection", {"series": series_request(), "method": "seasonal"}, to_payload(idx, y))
    assert "MSTL" in env.method_used
    assert env.details.seasonal_periods == []  # nothing was confirmed
    assert env.facts["seasonal_detected"] is False and env.facts["seasonal_modelled"] is True
    assert any("candidate was used" in c for c in env.caveats)


def test_anomaly_detection_seasonal_method_falls_back_without_a_testable_period():
    # 30 weeks is below the 105 needed to test a 52-week season: no candidate,
    # so the forced seasonal request degrades to a trend and says why.
    idx, y = seasonal_series(n=30, grain="week", amplitude=0, anomalies=(12,), anomaly_factor=1.6)
    env = _run("anomaly_detection", {"series": series_request(), "method": "seasonal"}, to_payload(idx, y))
    assert "LOWESS" in env.method_used
    assert env.facts["seasonal_modelled"] is False
    assert any("no testable" in n for n in env.details.notes)


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
    # 130 weeks − 104 (two cycles) = 26 spare: 5 overlapping folds of h=8, origins 4 apart.
    assert env.facts["cv_windows"] == 5 and env.facts["cv_step"] == 4
    # The band is calibrated on the winner's 40 held-out residuals; coverage
    # is reported out-of-sample on the last two folds (2 × h = 16 points).
    assert env.facts["interval_method"] == "conformal_scaled"
    assert env.facts["calibration_residuals"] == 40
    assert env.validation.coverage_n == 16
    assert any("calibrated on 40 held-out residuals" in n for n in env.details.notes)
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
    assert env.method_used == "AutoETS"


def test_forecast_explicit_method_is_returned_even_when_the_baseline_scores_better():
    # A pure random walk: no model beats Naive in CV. `auto` returns Naive and says
    # so; a user who picked ETS gets ETS, with the comparison stated in the note.
    rng = np.random.default_rng(11)
    idx, _ = white_noise(n=60, grain="week")
    y = 1000 + np.cumsum(rng.normal(0, 30, 60))
    auto = _run("forecast", {"series": series_request(), "horizon": 4}, to_payload(idx, y))
    chosen = _run("forecast", {"series": series_request(), "method": "auto_ets", "horizon": 4}, to_payload(idx, y))
    assert chosen.method_used == "AutoETS"
    ets, naive = (next(c for c in chosen.details.candidates if c.name == n) for n in ("AutoETS", "Naive"))
    assert ets.selected and naive.is_baseline and not naive.selected
    if ets.value is not None and naive.value is not None and ets.value >= naive.value:
        assert any(n.startswith("AutoETS was requested and is shown although Naive scored") for n in chosen.details.notes)
        assert auto.method_used == "Naive"
    # The strip's metric is the chosen model's own score, not the baseline's.
    assert chosen.validation.value == ets.value


def test_forecast_auto_shortlist_includes_drift_and_it_wins_a_trend():
    # A near-linear climb: Naive is flat and wrong, Drift extends the slope.
    idx = pd.date_range("2006-08-01", periods=24, freq="MS")
    y = np.linspace(400_000, 1_900_000, 24) + np.random.default_rng(2).normal(0, 40_000, 24)
    env = _run("forecast", {"series": series_request(grain="month"), "horizon": 8}, to_payload(idx, y))
    names = [c.name for c in env.details.candidates]
    assert names[:2] == ["Naive", "Drift"] and {"AutoETS", "AutoARIMA"} <= set(names)
    drift, naive = (next(c for c in env.details.candidates if c.name == n) for n in ("Drift", "Naive"))
    assert drift.value < naive.value
    forecast = [r["forecast"] for r in env.rows if r["is_forecast"]]
    assert forecast[-1] > forecast[0] > 1_800_000  # keeps climbing, not a flat line at the last value
    explicit = _run("forecast", {"series": series_request(grain="month"), "method": "drift", "horizon": 8}, to_payload(idx, y))
    assert explicit.method_used == "Drift" and {c.name for c in explicit.details.candidates} == {"Naive", "Drift"}


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


def test_forecast_confirmed_season_without_room_validates_without_seasonal_terms():
    # 110 weeks confirm the 52-week season but two cycles (104) leave only 6
    # points: no fold can hold out h=8. Instead of an unvalidated baseline, the
    # shortlist is validated without seasonal terms (SeasonalNaive keeps it).
    idx, y = seasonal_series(n=110, grain="week")
    env = _run("forecast", {"series": series_request(), "window": 110, "horizon": 8}, to_payload(idx, y))
    assert env.details.seasonal_periods == [52]
    assert env.facts["cv_windows"] > 0
    assert env.facts["seasonal_terms_fitted"] is False
    assert any("validated without a seasonal term" in n for n in env.details.notes)
    names = {c.name for c in env.details.candidates}
    assert "SeasonalNaive" in names and "AutoETS" in names and "MSTL+AutoETS" not in names
    assert env.facts["selection"]["validated"] is True


def test_forecast_band_is_calibrated_and_falls_back_to_native_when_thin():
    # Enough folds: the band is rescaled on the winner's own residuals.
    idx, y = seasonal_series(n=48, grain="month")
    env = _run("forecast", {"series": series_request(grain="month"), "window": 48, "horizon": 6}, to_payload(idx, y))
    assert env.facts["interval_method"] == "conformal_scaled"
    assert env.facts["conformal_factor"] > 0 and env.facts["calibration_residuals"] >= 12
    assert env.validation.coverage is not None and env.validation.coverage_n in (6, 12)
    fc = [r for r in env.rows if r["is_forecast"]]
    assert all(r["lower"] <= r["forecast"] <= r["upper"] for r in fc)
    # A 95% band needs 19 residuals; a single fold of 4 cannot supply them: native band, and it says so.
    idx, y = seasonal_series(n=16, grain="week", amplitude=0)
    thin = _run("forecast", {"series": series_request(), "window": 16, "horizon": 4, "interval": 0.95}, to_payload(idx, y))
    assert thin.facts["interval_method"] == "native"
    assert any("Band not calibrated" in n for n in thin.details.notes)


def test_forecast_intermittent_history_adds_the_croston_family_instead_of_refusing():
    rng = np.random.default_rng(51)
    idx, _ = white_noise(n=60, grain="week")
    y = np.where(rng.uniform(size=60) < 0.6, 0.0, rng.uniform(20, 60, 60))
    out = execute_skill("forecast", {"series": series_request(), "window": 60, "horizon": 8}, to_payload(idx, y))
    assert out.status == "ok", out.error  # the guard no longer refuses a forecast
    env = out.envelope
    guard = next(g for g in env.guard_results if g.name == "intermittent")
    assert guard.passed and not guard.overridable and "added to the shortlist" in guard.detail
    names = {c.name for c in env.details.candidates}
    assert {"CrostonSBA", "CrostonClassic", "ADIDA", "IMAPA"} <= names
    assert any("intermittent demand" in n for n in env.details.notes)
    fc = [r for r in env.rows if r["is_forecast"]]
    assert len(fc) == 8 and all(r["forecast"] >= 0 for r in fc)
    if env.method_used in ("CrostonSBA", "CrostonClassic", "ADIDA", "IMAPA"):
        # Point-only winner: the band is the absolute conformal half-width.
        assert env.facts["interval_method"] in ("conformal_absolute", "native")
    # Other series skills still refuse intermittent histories.
    anomaly = execute_skill("anomaly_detection", {"series": series_request(), "window": 60}, to_payload(idx, y))
    assert anomaly.status == "guard_failed" and [g.name for g in anomaly.guard_results if not g.passed] == ["intermittent"]


def test_forecast_top2_ensemble_is_off_by_default():
    from src.analysis.engines import forecast as engine

    assert engine.ENABLE_TOP2_ENSEMBLE is False
    idx, y = seasonal_series(n=130, grain="week")
    env = _run("forecast", {"series": series_request(), "window": 130, "horizon": 8}, to_payload(idx, y))
    assert not env.method_used.startswith("Mean(")
    assert env.facts["selection"]["ensemble_members"] == []


def test_forecast_holiday_calendar_is_a_known_future_regressor():
    pytest.importorskip("holidays")
    idx, y = seasonal_series(n=240, grain="day", level=800, slope=0.2, amplitude=150, noise=30, seed=3)
    payload = to_payload(idx, np.maximum(y, 0))
    with_cal = _run("forecast", {"series": series_request(grain="day"), "window": 240, "horizon": 14, "holidays": "IL"}, payload)
    assert with_cal.facts["holidays"] == "IL" and with_cal.facts["holidays_used"] is True
    assert with_cal.details.params_used["holidays"] == "IL"
    assert any("fitted as a regressor" in n for n in with_cal.details.notes)
    assert len([r for r in with_cal.rows if r["is_forecast"]]) == 14
    # Unknown country: the forecast still runs, without it, and says why.
    unknown = _run("forecast", {"series": series_request(grain="day"), "window": 240, "horizon": 14, "holidays": "ZZ"}, payload)
    assert unknown.facts["holidays_used"] is False
    assert any("not a known country code" in n for n in unknown.details.notes)
    # Contract: a lowercase or long code is rejected before the engine.
    bad = execute_skill("forecast", {"series": series_request(grain="day"), "horizon": 14, "holidays": "israel"}, payload)
    assert bad.status == "error" and "invalid analysis request" in bad.error


# ── execute_skill error paths ─────────────────────────────────────────────────


def test_execute_skill_reports_invalid_requests_without_raising():
    out = execute_skill("clustering", {}, {"rows": []})
    assert out.status == "error" and "invalid analysis request" in out.error
    out = execute_skill("forecast", {"series": series_request(), "horizon": 0}, {"rows": []})
    assert out.status == "error"
    out = execute_skill("forecast", {"series": series_request()}, {"rows": []})
    assert out.status == "error" and "no rows" in out.error
