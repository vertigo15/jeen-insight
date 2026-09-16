"""Per-skill ML narration — findings + follow-ups grounded in the engine facts.

Each test feeds a representative ``facts`` dict (mirroring what the engine
returns, see the engine survey) and asserts the deterministic output restates
those exact numbers and references the concrete entity in the follow-ups.
"""

from __future__ import annotations

from src.analysis.narration import (
    MAX_FINDINGS,
    MAX_FOLLOWUPS,
    SKILL_NARRATORS,
    narrate,
)

_SKILLS = [
    "anomaly_detection", "forecast", "changepoint", "seasonality", "correlation",
    "contribution", "clustering", "driver_analysis", "regression", "classification",
    "cohort_retention", "experiment_test",
]


def _env(skill, facts, *, caveats=None, headline="", params=None):
    return {
        "skill": skill,
        "facts": {"skill": skill, **facts},
        "params": params or {},
        "caveats": caveats or [],
        "headline": headline,
    }


def test_every_targeted_skill_has_a_registered_narrator():
    for skill in _SKILLS:
        assert skill in SKILL_NARRATORS, f"no narrator for {skill}"


def test_anomaly_findings_reference_flagged_periods_and_deviation():
    env = _env(
        "anomaly_detection",
        {
            "measure": "SalesAmount", "grain": "month", "n_points": 26, "n_flagged": 2,
            "sensitivity": 0.95, "seasonal_periods": [12],
            "headline_detail": "Largest deviation: 2007-02 came in 31% above expectation.",
            "flagged": [
                {"period": "2007-02", "actual": 180348, "expected": 141000, "deviation_pct": 0.279, "direction": "above"},
                {"period": "2006-11", "actual": 95000, "expected": 120000, "deviation_pct": -0.208, "direction": "below"},
            ],
        },
        caveats=["92% of history sits inside the band; sensitivity 0.95 expects about 95%."],
    )
    n = narrate(env)
    # headline_detail is prepended verbatim.
    assert n.findings[0].startswith("Largest deviation")
    joined = " ".join(n.findings)
    assert "2007-02" in joined and "+27.9%" in joined and "-20.8%" in joined
    # follow-ups reference the biggest flagged period.
    assert any("2007-02" in q for q in n.followups)
    assert len(n.findings) <= MAX_FINDINGS and len(n.followups) <= MAX_FOLLOWUPS


def test_anomaly_no_flags_branch():
    env = _env("anomaly_detection", {"measure": "Profit", "grain": "day", "n_points": 90, "n_flagged": 0, "flagged": []})
    n = narrate(env)
    assert any("no anomalies" in f.lower() or "inside the expected range" in f.lower() for f in n.findings)


def test_forecast_states_projection_change_and_interval():
    env = _env("forecast", {
        "measure": "Revenue", "grain": "month", "horizon": 6, "interval": 0.8, "method": "AutoARIMA", "cv_metric": "WAPE",
        "forecast_end": {"period": "2009-06", "value": 250000, "lower": 220000, "upper": 280000},
        "pct_change_vs_trailing": 0.12, "trailing_total": 1_200_000, "horizon_total": 1_344_000,
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "2009-06" in j and "250k" in j and "+12%" in j
    assert any("interval" in f.lower() for f in n.findings)
    assert n.followups


def test_changepoint_reports_largest_shift():
    env = _env("changepoint", {
        "measure": "Orders", "grain": "week", "n_points": 100, "n_changepoints": 1,
        "changepoints": [{"period": "2008-W12", "direction": "up", "level_before": 1000, "level_after": 1500, "magnitude_pct": 0.5}],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "2008-W12" in j and "1,000" in j and "1,500" in j and "+50%" in j
    assert any("2008-W12" in q for q in n.followups)


def test_changepoint_no_shift_branch():
    env = _env("changepoint", {"measure": "Orders", "grain": "week", "n_points": 60, "n_changepoints": 0, "changepoints": []})
    n = narrate(env)
    assert any("no level shift" in f.lower() for f in n.findings)


def test_seasonality_reports_cycle_peak_trough():
    env = _env("seasonality", {
        "measure": "Sales", "grain": "month", "n_points": 36, "seasonal_periods": [12], "strength": 0.72,
        "peak": {"label": "December"}, "trough": {"label": "February"}, "trend_growth_per_year": 0.08,
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "strong" in j and "12-month" in j and "December" in j and "February" in j
    assert any("December" in q for q in n.followups)


def test_correlation_reports_r_lag_and_causation_caveat():
    env = _env("correlation", {
        "measure": "Ad Spend", "other_measure": "Revenue", "grain": "week", "n_points": 52,
        "best": {"lag": 2, "r": 0.65},
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "r = 0.65" in j and "Revenue leading by 2 weeks" in j
    assert any("not causation" in f.lower() for f in n.findings)
    assert n.followups


def test_contribution_reports_delta_and_top_slice():
    env = _env("contribution", {
        "measure": "Profit", "dimensions": ["Category", "Region"], "delta": -50000, "delta_pct": -0.1,
        "best_dimension": "Category",
        "top_slices": [{"dimension": "Category", "slice": "Bikes", "share_of_change": 0.6}],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "-10%" in j and "-50k" in j and "Bikes" in j and "60%" in j
    assert any("Bikes" in q for q in n.followups)


def test_clustering_reports_segments_and_largest_share():
    env = _env("clustering", {
        "table": "customers", "entity_key": "id", "features": ["recency", "frequency"], "n_entities": 5000,
        "k": 4, "silhouette": 0.42,
        "profiles": [
            {"cluster": 0, "n": 2500, "share": 0.5},
            {"cluster": 1, "n": 1500, "share": 0.3},
            {"cluster": 2, "n": 1000, "share": 0.2},
        ],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "4 segments" in j and "5,000" in j and "0.42" in j and "50%" in j
    assert any("segment 0" in q.lower() for q in n.followups)


def test_driver_analysis_reports_r2_and_top_driver():
    env = _env("driver_analysis", {
        "table": "customers", "target": "churn", "features": ["tenure", "spend"], "n_rows": 8000, "r2_holdout": 0.34,
        "contributors": [
            {"feature": "tenure", "importance_share": 0.55, "direction": "negative"},
            {"feature": "spend", "importance_share": 0.30, "direction": "negative"},
        ],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "34%" in j and "tenure" in j and "55%" in j
    assert any("tenure" in q for q in n.followups)


def test_regression_reports_fit_and_significant_count():
    env = _env("regression", {
        "table": "stores", "target": "revenue", "features": ["size", "staff"], "n_rows": 300, "r2_holdout": 0.61,
        "coefficients": [
            {"feature": "size", "direction": "positive", "significant": True},
            {"feature": "staff", "direction": "positive", "significant": False},
        ],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "0.61" in j and "1 of 2" in j and "size" in j
    assert any("size" in q for q in n.followups)


def test_classification_reports_auc_and_top_odds():
    env = _env("classification", {
        "table": "customers", "target": "churned", "positive_class": "yes", "features": ["tenure", "spend"],
        "n_rows": 9000, "positive_rate": 0.22, "auc": 0.78,
        "coefficients": [
            {"feature": "tenure", "odds_ratio": 0.6, "direction": "negative"},
            {"feature": "spend", "odds_ratio": 0.8, "direction": "negative"},
        ],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "AUC 0.78" in j and "22%" in j and "tenure" in j and "x0.60" in j
    assert any("churned=yes" in q for q in n.followups)


def test_cohort_retention_reports_curve_endpoints():
    env = _env("cohort_retention", {
        "table": "users", "grain": "month", "n_cohorts": 12, "max_offset": 6,
        "retention_at_1": 0.45, "retention_at_last": 0.20, "avg_curve": [],
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "45%" in j and "12 cohorts" in j and "20%" in j
    assert n.followups


def test_experiment_test_reports_winner_lift_and_significance():
    env = _env("experiment_test", {
        "table": "ab", "outcome_type": "binary", "test": "two-proportion z-test", "control": "A", "treatment": "B",
        "n_control": 5000, "n_treatment": 5000, "estimate_control": 0.10, "estimate_treatment": 0.12,
        "abs_diff": 0.02, "rel_lift": 0.2, "diff_ci_low": 0.005, "diff_ci_high": 0.035,
        "p_value": 0.004, "confidence": 0.95, "significant": True,
    })
    n = narrate(env)
    j = " ".join(n.findings)
    assert "B" in j and "12%" in j and "10%" in j and "+20%" in j and "p=0.004" in j and "significant" in j
    assert n.followups


def test_generic_fallback_for_unregistered_skill_does_not_crash():
    n = narrate(_env("made_up_skill", {"measure": "X"}))
    assert isinstance(n.findings, list) and isinstance(n.followups, list)
    assert n.followups == []  # the generic narrator offers none


def test_findings_are_deduped_and_capped():
    env = _env(
        "anomaly_detection",
        {
            "measure": "S", "grain": "month", "n_points": 30, "n_flagged": 5,
            "flagged": [
                {"period": f"2007-{i:02d}", "actual": 100, "expected": 80, "deviation_pct": 0.25, "direction": "above"}
                for i in range(1, 6)
            ],
        },
        caveats=["c1", "c2", "c3"],
    )
    n = narrate(env)
    assert len(n.findings) <= MAX_FINDINGS
    assert len(set(n.findings)) == len(n.findings)


def test_narrate_never_raises_or_fabricates_on_missing_or_partial_facts():
    for bad in ({}, {"skill": "anomaly_detection"}, {"skill": "anomaly_detection", "facts": {}}):
        n = narrate(bad)
        assert isinstance(n.findings, list) and isinstance(n.followups, list)
        # No assertive claim built from a missing value (e.g. "All None periods...").
        assert not any("None" in f for f in n.findings)


def test_caveats_are_kept_even_with_many_flagged_points():
    env = _env(
        "anomaly_detection",
        {
            "measure": "S", "grain": "month", "n_points": 40, "n_flagged": 4,
            "headline_detail": "Largest deviation: 2007-03 came in 40% above expectation.",
            "flagged": [
                {"period": f"2007-{i:02d}", "actual": 100, "expected": 70, "deviation_pct": 0.4, "direction": "above"}
                for i in range(1, 5)
            ],
        },
        caveats=["Correlation-style disclaimer.", "A guard was overridden; treat as indicative."],
    )
    n = narrate(env)
    # The reserved slots keep at least one caveat despite the flood of points.
    assert any("guard was overridden" in f or "disclaimer" in f for f in n.findings)
