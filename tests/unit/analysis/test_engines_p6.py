"""P6 engines on synthetic data: changepoint, seasonality, correlation,
contribution, multi-series, clustering and driver analysis — plus the guards
that gate the new families."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.guards import cardinality, feature_count, series_count, slices
from src.analysis.runner import execute_skill
from tests.unit.analysis.synthetic import series_request

pytestmark = pytest.mark.filterwarnings("ignore")

REQ = series_request()


def _weekly(n=130, seed=7, amp=20_000, shift_at=None, slope=300, step=40_000):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    y = 100_000 + slope * t + amp * np.sin(2 * np.pi * t / 52) + rng.normal(0, 3000, n)
    if shift_at:
        y[shift_at:] += step
    idx = pd.date_range("2024-01-01", periods=n, freq="W-MON")
    return idx, y


def _rows(idx, y, **extra_cols):
    return {"columns": ["ts", "value", *extra_cols], "rows": [
        {"ts": d.date().isoformat(), "value": float(v), **{k: float(col[i]) for k, col in extra_cols.items()}}
        for i, (d, v) in enumerate(zip(idx, y))
    ]}


def _ok(skill, params, payload, **kw):
    out = execute_skill(skill, params, payload, **kw)
    assert out.ok, (out.status, out.error, [g.detail for g in out.guard_results if not g.passed])
    return out.envelope


# ── the two-cycle boundary is one boundary for every seasonal engine ─────────


@pytest.mark.parametrize("n", [104, 105])
@pytest.mark.parametrize("skill", ["seasonality", "anomaly_detection", "changepoint", "forecast"])
def test_exactly_two_cycles_never_errors_and_the_rule_is_shared(skill, n):
    """statsmodels' STL rejects a period of exactly n/2. Every engine must agree:
    104 weekly points → no period 52 (fit without seasonality); 105 → period 52."""
    from src.analysis.series import candidate_periods, min_points_for_period

    assert min_points_for_period(52) == 105
    assert candidate_periods("week", 104) == [] and candidate_periods("week", 105) == [52]
    idx, y = _weekly(n=n)
    params = {"series": REQ, **({"horizon": 4} if skill == "forecast" else {})}
    env = _ok(skill, params, _rows(idx, y))
    has_season = 52 in (env.details.seasonal_periods or [])
    assert has_season == (n >= 105), (skill, n, env.method_used)


# ── changepoint ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", ["step_no_season", "two_steps", "small_step", "step_seasonal", "flat", "ramp_only"])
def test_changepoint_finds_steps_and_ignores_ramps(case):
    if case == "step_no_season":
        idx, y = _weekly(shift_at=60, amp=0); expected = [60]
    elif case == "two_steps":
        idx, y = _weekly(shift_at=40, amp=0); y[90:] -= 60_000; expected = [40, 90]
    elif case == "small_step":
        idx, y = _weekly(shift_at=70, amp=0, step=8_000); expected = [70]
    elif case == "step_seasonal":
        idx, y = _weekly(shift_at=80); expected = [80]
    elif case == "flat":
        idx, y = _weekly(amp=0, slope=0); expected = []
    else:
        idx, y = _weekly(amp=0); expected = []  # a steady ramp is one regime
    env = _ok("changepoint", {"series": REQ}, _rows(idx, y))
    found = [list(idx).index(pd.Timestamp(c["ts"])) for c in env.facts["changepoints"]]
    assert found == expected
    assert env.chart_spec.chart_type == "band"
    assert env.columns[:3] == ["ts", "actual", "trend"]
    if expected:
        cp = env.facts["changepoints"][0]
        assert cp["direction"] in ("up", "down") and 0 <= cp["confidence"] <= 1
        assert "shifted" in env.headline or "level shifts" in env.headline
    else:
        assert env.headline.startswith("No level shift")


# ── seasonality ───────────────────────────────────────────────────────────────


def test_seasonality_reports_strength_peak_and_trend_growth():
    rng = np.random.default_rng(7)
    t = np.arange(60)
    y = 100_000 + 500 * t + 20_000 * np.sin(2 * np.pi * (t - 3) / 12) + rng.normal(0, 2000, 60)
    idx = pd.date_range("2021-01-01", periods=60, freq="MS")
    env = _ok("seasonality", {"series": series_request(grain="month")}, _rows(idx, y))
    assert env.details.seasonal_periods == [12]
    assert env.facts["strength"] > 0.5
    assert env.facts["peak"]["label"] in ("June", "July")  # sin peaks at t-3 = 3 → month index 6
    assert env.facts["trend_growth_per_year"] > 0
    assert env.chart_spec.chart_type == "line" and env.chart_spec.y_columns == ["actual", "trend", "seasonal"]
    assert env.columns == ["ts", "actual", "trend", "seasonal", "residual", "observed"]
    assert "cycle" in env.headline


def test_seasonality_says_so_when_there_is_none():
    idx, y = _weekly(n=40, amp=0)
    env = _ok("seasonality", {"series": REQ}, _rows(idx, y))
    assert env.details.seasonal_periods == [] and env.facts["strength"] == 0
    assert env.headline.startswith("No seasonal cycle")


# ── correlation ───────────────────────────────────────────────────────────────


def test_correlation_finds_the_lag_and_always_carries_the_caveat():
    idx, y = _weekly()
    # other[t] = y[t-3]: the measure moves first, the other follows three weeks later.
    other = np.roll(y, 3) * 0.4 + np.random.default_rng(1).normal(0, 500, len(y))
    other[:3] = other[3]
    env = _ok("correlation", {"series": REQ, "other_measure_column": "SalesAmount", "max_lag": 6},
              _rows(idx, y, value2=other))
    assert env.facts["best"]["lag"] == -3 and env.facts["best"]["r"] > 0.9
    assert "SUM(Profit) leading by 3 weeks" in env.headline
    # And the mirror image: the other measure leads.
    lead = np.roll(y, -3) * 0.4
    lead[-3:] = lead[-4]
    env2 = _ok("correlation", {"series": REQ, "other_measure_column": "SalesAmount", "max_lag": 6}, _rows(idx, y, value2=lead))
    assert env2.facts["best"]["lag"] == 3 and "SUM(SalesAmount) leading by 3 weeks" in env2.headline
    assert env.caveats[0].startswith("Correlation is not causation")
    assert env.columns == ["lag", "r", "p_value", "p_adjusted", "n"] and len(env.rows) == 13
    # Significance is judged on the lag- and autocorrelation-adjusted p, never the raw one.
    best = env.facts["best"]
    assert best["p_adjusted"] >= best["p_value"]
    assert env.validation.extras["p_adjusted"] == pytest.approx(best["p_adjusted"], abs=1e-4)
    assert env.validation.band in ("good", "fair")  # a planted r > 0.9 survives the adjustment
    assert any("adjusted for the 13 lags" in c for c in env.caveats)


def test_correlation_of_two_noise_series_is_not_called_significant():
    """A lag search over noise finds *some* |r|; the adjusted p must not bless it."""
    rng = np.random.default_rng(11)
    idx, _ = _weekly()
    n = len(idx)
    # Smooth (autocorrelated) noise makes the raw Pearson p far too optimistic.
    a = np.cumsum(rng.normal(0, 1, n)) + 1000
    b = np.cumsum(rng.normal(0, 1, n)) + 1000
    env = _ok("correlation", {"series": REQ, "other_measure_column": "SalesAmount", "max_lag": 6}, _rows(idx, a, value2=b))
    best = env.facts["best"]
    assert best["p_adjusted"] > best["p_value"]
    if best["p_adjusted"] >= 0.05:
        assert any("not statistically significant" in c for c in env.caveats)
        assert env.validation.band == "poor"
    assert env.chart_spec.chart_type == "bar" and env.chart_spec.x_column == "lag"
    assert env.egress.columns == ["ts", "value", "value2"]
    assert isinstance(env.facts["granger_p_values"], dict)


# ── contribution ──────────────────────────────────────────────────────────────


def test_contribution_decomposes_the_delta_and_picks_the_dimension():
    rows = []
    for dim, slice_, before, after in [
        ("Territory", "NW", 1000, 600), ("Territory", "SW", 500, 520), ("Territory", "NE", 300, 310),
        ("Category", "Bikes", 1200, 800), ("Category", "Accessories", 600, 630),
    ]:
        rows += [{"dimension": dim, "slice": slice_, "period": "before", "value": before},
                 {"dimension": dim, "slice": slice_, "period": "after", "value": after}]
    params = {"series": series_request(grain="month"), "dimensions": ["Territory", "Category"],
              "before_start": "2026-01-01", "before_end": "2026-04-01", "after_start": "2026-04-01", "after_end": "2026-07-01"}
    env = _ok("contribution", params, {"columns": ["dimension", "slice", "period", "value"], "rows": rows})
    assert env.facts["delta"] == -370 and env.facts["before"]["total"] == 1800 and env.facts["after"]["total"] == 1430
    nw = next(r for r in env.rows if r["slice"] == "NW")
    assert nw["delta"] == -400 and abs(nw["share_of_change"] - 400 / 370) < 1e-9
    assert env.facts["best_dimension"] in ("Territory", "Category")
    # Ranking is the documented composite: explanatory power first, concentration as tie-break.
    scores = env.facts["dimension_scores"]
    assert [d["score"] for d in scores] == sorted((d["score"] for d in scores), reverse=True)
    assert scores[0]["dimension"] == env.facts["best_dimension"]
    assert {"explained_by_top", "surprise_in_top", "total_surprise", "score"} <= set(scores[0])
    assert env.chart_spec.chart_type == "horizontal_bar" and env.chart_spec.row_filter["column"] == "dimension"
    assert "fell 370" in env.headline and "accounts for" in env.headline
    assert any("decomposition of the change" in c for c in env.caveats)
    assert env.egress.tier == "A"


def test_contribution_guard_needs_two_slices():
    rows = [{"dimension": "Territory", "slice": "NW", "period": "before", "value": 10},
            {"dimension": "Territory", "slice": "NW", "period": "after", "value": 8}]
    out = execute_skill("contribution", {"series": series_request(), "dimensions": ["Territory"]},
                        {"columns": ["dimension", "slice", "period", "value"], "rows": rows})
    assert out.status == "guard_failed" and out.guard_results[0].name == "slices"
    assert "1 slice across Territory" in out.guard_results[0].detail


# ── multi-series ──────────────────────────────────────────────────────────────


def test_group_by_runs_one_engine_per_series_and_merges():
    idx, y = _weekly(n=60, amp=0)
    rows = []
    for sid, scale in (("A", 1.0), ("B", 0.5), ("C", 0.1)):
        rows += [{"ts": d.date().isoformat(), "series_id": sid, "value": float(v * scale)} for d, v in zip(idx, y)]
    env = _ok("anomaly_detection", {"series": series_request(group_by="Territory")},
              {"columns": ["ts", "series_id", "value"], "rows": rows})
    assert env.columns[0] == "series_id" and len(env.rows) == 180
    assert env.facts["series_count"] == 3 and env.facts["top_series"] == "A"
    assert set(env.facts["series"]) == {"A", "B", "C"}
    assert env.chart_spec.row_filter == {"column": "series_id", "value": "A"}
    assert env.egress.rows_sent_to_model == 180
    assert env.headline.startswith("3 series by Territory")
    assert any(g.name.startswith("series_count") for g in env.guard_results)


def test_series_count_guard_caps_the_split():
    g = series_count(20, "ProductKey")
    assert not g.passed and g.exits[0].params_patch == {"series": {"group_by": None}}
    assert series_count(12, "x").passed


# ── tier B ────────────────────────────────────────────────────────────────────


def _entities(n=400, seed=3):
    rng = np.random.default_rng(seed)
    income = rng.normal(60_000, 15_000, n) + rng.integers(0, 3, n) * 40_000
    kids = rng.integers(0, 5, n)
    cars = rng.integers(0, 4, n)
    profit = 0.02 * income + 500 * kids + rng.normal(0, 300, n)
    rows = [{"entity_key": i, "YearlyIncome": float(income[i]), "TotalChildren": int(kids[i]),
             "NumberCarsOwned": int(cars[i]), "target": float(profit[i]), "Note": "x"} for i in range(n)]
    return {"columns": ["entity_key", "YearlyIncome", "TotalChildren", "NumberCarsOwned", "target", "Note"], "rows": rows}


ENTITY = {"table": "DimCustomer", "entity_key": "CustomerKey", "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"]}


def test_clustering_returns_profiles_and_a_scatter_spec():
    env = _ok("clustering", {"entity": ENTITY, "k": 3}, _entities())
    assert env.egress.tier == "B" and env.facts["k"] == 3
    assert sorted(p["cluster"] for p in env.facts["profiles"]) == [0, 1, 2]
    assert abs(sum(p["share"] for p in env.facts["profiles"]) - 1) < 1e-6
    assert env.columns[:2] == ["entity_key", "cluster"] and len(env.rows) == 400
    assert env.chart_spec.chart_type == "scatter" and env.chart_spec.series_column == "cluster"
    assert env.validation.metric == "silhouette"
    assert any("arbitrary labels" in c for c in env.caveats)
    # Non-numeric requested features are dropped by the guard, not fatal.
    env2 = _ok("clustering", {"entity": {**ENTITY, "features": ["YearlyIncome", "TotalChildren", "Note"]}, "k": 2}, _entities())
    assert env2.facts["features"] == ["YearlyIncome", "TotalChildren"]
    fc = next(g for g in env2.guard_results if g.name == "feature_count")
    assert "not numeric: Note" in fc.detail
    # A numeric column that is mostly NULL is reported as sparse, not as "not numeric".
    base = _entities()
    sparse = {"columns": base["columns"] + ["Weight"],
              "rows": [{**row, "Weight": (row["YearlyIncome"] / 1000 if i % 3 == 0 else None)} for i, row in enumerate(base["rows"])]}
    env3 = _ok("clustering", {"entity": {**ENTITY, "features": ["YearlyIncome", "TotalChildren", "Weight"]}, "k": 2}, sparse)
    fc3 = next(g for g in env3.guard_results if g.name == "feature_count")
    assert "mostly empty: Weight 34% filled" in fc3.detail and "not numeric" not in fc3.detail  # 134 of 400 rows


def test_clustering_auto_k_uses_silhouette():
    env = _ok("clustering", {"entity": {**ENTITY, "features": ["YearlyIncome", "TotalChildren"]}}, _entities())
    assert 2 <= env.facts["k"] <= 8
    assert sum(1 for c in env.details.candidates if c.selected) == 1


def test_driver_analysis_ranks_the_real_drivers():
    env = _ok("driver_analysis", {"entity": {**ENTITY, "target": "Profit"}}, _entities())
    order = [c["feature"] for c in env.facts["contributors"]]
    assert set(order[:2]) == {"YearlyIncome", "TotalChildren"} and order[-1] == "NumberCarsOwned"
    assert env.facts["contributors"][-1]["importance_share"] < 0.05
    assert env.facts["r2_holdout"] > 0.8 and env.validation.metric == "R2"
    assert env.facts["contributors"][0]["direction"] == "positive"
    assert env.caveats[0].startswith("Predictive, not causal")
    assert env.chart_spec.chart_type == "horizontal_bar"
    assert env.egress.tier == "B" and "target" in env.egress.columns


def test_driver_analysis_method_defaults_to_hgb():
    env = _ok("driver_analysis", {"entity": {**ENTITY, "target": "Profit"}}, _entities())
    assert env.details.params_used["engine"] == "HistGradientBoosting"
    assert env.details.method_used.startswith("HistGradientBoosting")
    assert any(c.selected and c.name == "HistGradientBoosting" for c in env.details.candidates)


def test_driver_analysis_auto_selects_an_available_engine():
    env = _ok("driver_analysis", {"entity": {**ENTITY, "target": "Profit"}, "method": "auto"}, _entities())
    selected = [c for c in env.details.candidates if c.selected]
    assert len(selected) == 1 and selected[0].name in {"HistGradientBoosting", "XGBoost", "LightGBM"}
    assert env.facts["r2_holdout"] > 0.8


def test_driver_analysis_unavailable_method_falls_back_with_a_note():
    try:
        import xgboost  # noqa: F401
        pytest.skip("xgboost is installed; the fallback path is not exercised")
    except Exception:  # noqa: BLE001
        pass
    env = _ok("driver_analysis", {"entity": {**ENTITY, "target": "Profit"}, "method": "xgboost"}, _entities())
    assert env.details.params_used["engine"] == "HistGradientBoosting"
    assert any("not installed" in n for n in env.details.notes)


def test_regression_reports_signed_coefficients_and_fit():
    env = _ok("regression", {"entity": {**ENTITY, "target": "Profit"}}, _entities())
    coefs = {r["feature"]: r for r in env.facts["coefficients"]}
    # The DGP is profit = 0.02*income + 500*kids + noise; both are positive and significant.
    assert coefs["YearlyIncome"]["coefficient"] > 0 and coefs["YearlyIncome"]["significant"]
    assert coefs["TotalChildren"]["coefficient"] > 0 and coefs["TotalChildren"]["significant"]
    # OLS recovers the planted slopes within tolerance.
    assert abs(coefs["YearlyIncome"]["coefficient"] - 0.02) < 0.005
    assert abs(coefs["TotalChildren"]["coefficient"] - 500) < 120
    # Cars are noise: the weakest standardized effect.
    weakest = min(coefs.values(), key=lambda r: abs(r["std_coef"]))
    assert weakest["feature"] == "NumberCarsOwned"
    assert env.validation.metric == "R2" and (env.facts["r2_holdout"] or env.facts["r2_full"]) > 0.8
    assert env.chart_spec.chart_type == "horizontal_bar" and env.chart_spec.y_columns == ["std_coef"]
    assert env.caveats[0].startswith("Associative, not causal")
    assert env.egress.tier == "B" and "target" in env.egress.columns
    # Every row carries the stats the Model-details table renders.
    assert {"coefficient", "std_coef", "std_err", "t", "p_value", "ci_low", "ci_high"} <= set(env.rows[0])


def test_regression_missing_target_is_refused():
    no_target_rows = [{k: v for k, v in r.items() if k != "target"} for r in _entities()["rows"]]
    out = execute_skill("regression", {"entity": {**ENTITY, "target": "Nope"}},
                        {"columns": ["entity_key", "YearlyIncome", "TotalChildren", "NumberCarsOwned", "Note"], "rows": no_target_rows})
    assert out.status == "guard_failed" and any(g.name == "target_numeric" for g in out.guard_results)


def _entities_binary(n=400, seed=5):
    rng = np.random.default_rng(seed)
    income = rng.normal(60_000, 15_000, n)
    kids = rng.integers(0, 5, n)
    cars = rng.integers(0, 4, n)
    lin = 0.00006 * (income - 60_000) + 0.45 * (kids - 2)  # cars are noise
    p = 1 / (1 + np.exp(-lin))
    target = rng.binomial(1, p)
    rows = [{"entity_key": i, "YearlyIncome": float(income[i]), "TotalChildren": int(kids[i]),
             "NumberCarsOwned": int(cars[i]), "target": int(target[i]), "Note": "x"} for i in range(n)]
    return {"columns": ["entity_key", "YearlyIncome", "TotalChildren", "NumberCarsOwned", "target", "Note"], "rows": rows}


def test_classification_reports_odds_ratios_and_auc():
    env = _ok("classification", {"entity": {**ENTITY, "target": "Churn"}}, _entities_binary())
    coefs = {r["feature"]: r for r in env.facts["coefficients"]}
    # Higher income raises the odds of the positive class in the DGP.
    assert coefs["YearlyIncome"]["coef_std"] > 0 and coefs["YearlyIncome"]["odds_ratio"] > 1
    assert env.validation.metric == "AUC" and env.facts["auc"] > 0.6
    assert env.facts["positive_class"] in (0, 1) and env.facts["classes"] == [0, 1]
    assert env.chart_spec.chart_type == "horizontal_bar" and env.chart_spec.y_columns == ["coef_std"]
    assert env.caveats[0].startswith("Associative, not causal")
    assert env.egress.tier == "B" and "target" in env.egress.columns
    assert {"odds_ratio", "or_ci_low", "or_ci_high", "p_value"} <= set(env.rows[0])


def test_classification_requires_a_binary_target():
    # Continuous Profit has many distinct values → refused as non-binary.
    out = execute_skill("classification", {"entity": {**ENTITY, "target": "Profit"}}, _entities())
    assert out.status == "guard_failed" and any(g.name == "target_binary" for g in out.guard_results)


COHORT = {"table": "orders", "entity_key": "CustomerKey", "cohort_date": "SignupDate",
          "activity_date": "OrderDate", "grain": "month", "max_periods": 12}


def _cohort_rows(n_cohorts=6, offsets=4, decay=0.85, size=100):
    """Aggregated (cohort, period, active) rows plus a NULL-period size row."""
    rows = []
    for m in range(n_cohorts):
        cohort = (pd.Timestamp("2024-01-01") + pd.DateOffset(months=m)).date().isoformat()
        rows.append({"cohort": cohort, "period": None, "active": size})
        for off in range(offsets):
            p = (pd.Timestamp(cohort) + pd.DateOffset(months=off)).date().isoformat()
            rows.append({"cohort": cohort, "period": p, "active": int(round(size * (decay ** off)))})
    return {"columns": ["cohort", "period", "active"], "rows": rows}


def test_cohort_retention_builds_a_curve():
    env = _ok("cohort_retention", {"cohort": COHORT}, _cohort_rows())
    assert env.skill == "cohort_retention"
    assert env.chart_spec.chart_type == "line" and env.chart_spec.x_column == "period_offset"
    assert env.chart_spec.series_column == "cohort"
    # Offset 0 is the whole cohort; the curve decays after that.
    avg = {c["offset"]: c["retention"] for c in env.facts["avg_curve"]}
    assert avg[0] == pytest.approx(1.0, abs=1e-6)
    assert avg[1] < avg[0] and env.facts["n_cohorts"] == 6
    assert env.validation.metric == "retention@1"
    assert env.egress.tier == "A" and env.egress.columns == ["cohort", "period", "active"]
    # A size-weighted "All cohorts" series precedes the per-cohort rows.
    assert env.rows[0]["cohort"] == "All cohorts"


def test_cohort_retention_needs_two_cohorts():
    out = execute_skill("cohort_retention", {"cohort": COHORT}, _cohort_rows(n_cohorts=1))
    assert out.status == "guard_failed" and any(g.name == "cohort_size" for g in out.guard_results)


def test_cohort_retention_needs_history():
    out = execute_skill("cohort_retention", {"cohort": COHORT}, _cohort_rows(n_cohorts=3, offsets=1))
    assert out.status == "guard_failed" and any(g.name == "retention_history" for g in out.guard_results)


EXPERIMENT = {"table": "events", "group_column": "variant", "outcome_column": "converted", "outcome_type": "binary"}


def _arm_rows(pairs):
    """pairs: list of (arm, n, sum_x, sum_x2)."""
    return {"columns": ["arm", "n", "sum_x", "sum_x2"],
            "rows": [{"arm": a, "n": n, "sum_x": sx, "sum_x2": sx2} for a, n, sx, sx2 in pairs]}


def test_experiment_binary_detects_a_significant_lift():
    payload = _arm_rows([("control", 1000, 100, 100), ("treatment", 1000, 140, 140)])
    env = _ok("experiment_test", {"experiment": {**EXPERIMENT, "control": "control"}}, payload)
    f = env.facts
    assert f["control"] == "control" and f["treatment"] == "treatment"
    assert f["estimate_control"] == pytest.approx(0.10, abs=1e-6) and f["estimate_treatment"] == pytest.approx(0.14, abs=1e-6)
    assert f["rel_lift"] == pytest.approx(0.40, abs=1e-6) and f["significant"] is True and f["p_value"] < 0.05
    assert env.validation.metric == "p-value"
    assert env.chart_spec.chart_type == "bar" and env.chart_spec.x_column == "arm"
    assert env.egress.tier == "A" and env.egress.columns == ["arm", "n", "sum_x", "sum_x2"]
    assert env.rows[0]["is_control"] is True


def test_experiment_continuous_uses_welch():
    # mean 50 vs 50.2, sd 10, n 300 each → not significant.
    def moments(mean, sd, n):
        return mean * n, (sd * sd) * (n - 1) + n * mean * mean
    sxc, sx2c = moments(50.0, 10.0, 300)
    sxt, sx2t = moments(50.2, 10.0, 300)
    payload = _arm_rows([("A", 300, sxc, sx2c), ("B", 300, sxt, sx2t)])
    env = _ok("experiment_test", {"experiment": {**EXPERIMENT, "outcome_column": "revenue", "outcome_type": "continuous", "control": "A"}}, payload)
    assert "Welch" in env.method_used and env.facts["significant"] is False
    assert env.facts["estimate_treatment"] == pytest.approx(50.2, abs=1e-6)


def test_experiment_needs_exactly_two_arms():
    payload = _arm_rows([("a", 500, 50, 50), ("b", 500, 60, 60), ("c", 500, 70, 70)])
    out = execute_skill("experiment_test", {"experiment": EXPERIMENT}, payload)
    assert out.status == "guard_failed" and any(g.name == "arm_count" for g in out.guard_results)


def test_experiment_small_arm_is_refused_then_overridable():
    payload = _arm_rows([("control", 12, 2, 2), ("treatment", 15, 5, 5)])
    out = execute_skill("experiment_test", {"experiment": EXPERIMENT}, payload)
    assert out.status == "guard_failed" and any(g.name == "group_size" for g in out.guard_results)
    forced = execute_skill("experiment_test", {"experiment": EXPERIMENT}, payload, override_guards=True)
    assert forced.ok and forced.envelope.low_confidence


def test_entity_guards():
    small = _entities(n=20)
    out = execute_skill("clustering", {"entity": ENTITY}, small)
    assert out.status == "guard_failed" and out.guard_results[0].name == "cardinality"
    assert "20 entities; at least 50 needed" in out.guard_results[0].detail
    # Override runs anyway and flags low confidence.
    forced = execute_skill("clustering", {"entity": {**ENTITY, "features": ["YearlyIncome", "TotalChildren"]}, "k": 2}, _entities(n=60), override_guards=True)
    assert forced.ok
    assert cardinality(10, 50_000).passed is False and cardinality(500, 50_000).passed
    assert not feature_count(["a"], ["a", "b"]).passed and feature_count(["a", "b"], ["a", "b"]).passed
    assert not slices(1, 2, dimensions=["d"]).passed and slices(5, 10, dimensions=["d"]).passed
    no_target_rows = [{k: v for k, v in r.items() if k != "target"} for r in _entities()["rows"]]
    missing_target = execute_skill("driver_analysis", {"entity": {**ENTITY, "target": "Nope"}},
                                   {"columns": ["entity_key", "YearlyIncome", "TotalChildren", "NumberCarsOwned", "Note"], "rows": no_target_rows})
    assert missing_target.status == "guard_failed" and any(g.name == "target_numeric" for g in missing_target.guard_results)
