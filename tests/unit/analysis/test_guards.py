"""Every guard boundary, with numeric details and executable exits."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.contracts import SeriesRequest
from src.analysis.guards import (
    GAP_RATIO_MAX,
    SERIES_LENGTH_MIN,
    failed,
    gap_ratio,
    horizon_cap,
    intermittent,
    max_horizon,
    min_history,
    run_post_sql_guards,
    series_length,
)
from src.analysis.series import prepare_series
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload


def _sf(n, *, grain="week", drop=(), values=None, agg="sum"):
    idx, y = seasonal_series(n=n, grain=grain, amplitude=0)
    if values is not None:
        y = np.asarray(values, dtype=float)
    return prepare_series(to_payload(idx, y, drop=drop)["rows"], SeriesRequest(**series_request(grain=grain, agg=agg)))


def test_series_length_boundary():
    assert series_length(_sf(SERIES_LENGTH_MIN)).passed
    g = series_length(_sf(SERIES_LENGTH_MIN - 1))
    assert not g.passed
    assert g.detail == "11 of 12 weeks needed"
    kinds = {e.kind for e in g.exits}
    assert {"patch", "override"} <= kinds
    assert any(e.recommended for e in g.exits)
    # The finer-grain exit is a real patch the browser can post back.
    finer = [e for e in g.exits if e.params_patch.get("series", {}).get("grain") == "day"]
    assert finer and finer[0].recommended


def test_gap_ratio_boundary():
    ok = _sf(20, drop=(1, 2, 3, 4))  # 4/20 = 20% — allowed
    assert gap_ratio(ok).passed
    bad = _sf(20, drop=(1, 2, 3, 4, 5))  # 25%
    g = gap_ratio(bad)
    assert not g.passed and "25%" in g.detail and g.required == GAP_RATIO_MAX
    assert g.exits[0].params_patch == {"series": {"grain": "month"}} and g.exits[0].recommended
    assert g.exits[-1].kind == "override"


def test_min_history_is_informational():
    short = _sf(30)  # < 104 weeks: no candidate period
    g = min_history(short)
    assert g.passed and not g.overridable and "30 of 105 weeks" in g.detail
    long = _sf(110)
    g2 = min_history(long)
    assert g2.passed and "110 weeks" in g2.detail


def test_horizon_cap_uses_history_and_longest_period():
    sf = _sf(60)  # no season → n/3 = 20
    assert horizon_cap(sf) == 20
    idx, y = seasonal_series(n=130, grain="week")  # season 52 confirmed → min(43, 104) = 43
    sf2 = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request()))
    assert sf2.seasonal_periods == [52] and horizon_cap(sf2) == 43
    idx3, y3 = seasonal_series(n=48, grain="month")  # m=12 → min(16, 24) = 16
    sf3 = prepare_series(to_payload(idx3, y3)["rows"], SeriesRequest(**series_request(grain="month")))
    assert horizon_cap(sf3) == 16


def test_max_horizon_refuses_with_a_recommended_patch():
    sf = _sf(62, grain="day")
    g = max_horizon(sf, 90)
    assert not g.passed
    assert g.detail == "90 days requested; 62 days of history support at most 20"
    rec = [e for e in g.exits if e.recommended]
    assert rec and rec[0].params_patch == {"horizon": 20}
    assert g.exits[-1].kind == "override" and "anyway" in g.exits[-1].label
    assert max_horizon(sf, 20).passed


def test_intermittent_boundary():
    values = [0.0] * 11 + [5.0] * 9  # 55% zeros
    g = intermittent(_sf(20, values=values))
    assert not g.passed and "55%" in g.detail
    assert g.exits[0].params_patch == {"series": {"grain": "month"}}
    assert intermittent(_sf(20, values=[0.0] * 10 + [5.0] * 10)).passed


def test_grain_exits_restate_horizon_and_window_within_the_skills_bounds():
    # A grain change keeps the calendar span: 26 weeks → 6 months for a skill
    # whose window floor is 12 → clamped to 12; seasonality's floor is 24.
    bad = _sf(20, drop=(1, 2, 3, 4, 5))
    g = gap_ratio(bad, horizon=8, window=26, skill="forecast")
    assert g.exits[0].params_patch == {"series": {"grain": "month"}, "horizon": 2, "window": 12}
    g_season = gap_ratio(bad, window=26, skill="seasonality")
    assert g_season.exits[0].params_patch == {"series": {"grain": "month"}, "window": 24}
    # Through the runner entry point the skill travels with the guard names.
    results = run_post_sql_guards(bad, guard_names=["gap_ratio"], window=26, skill="seasonality")
    assert results[0].exits[0].params_patch["window"] == 24
    # The intermittent guard is informational for forecast, refusing for the rest.
    zeros = _sf(20, values=[0.0] * 11 + [5.0] * 9)
    fc = run_post_sql_guards(zeros, guard_names=["intermittent"], skill="forecast")[0]
    assert fc.passed and not fc.overridable and "handles intermittent demand itself" in fc.detail
    an = run_post_sql_guards(zeros, guard_names=["intermittent"], skill="anomaly_detection")[0]
    assert not an.passed and an.exits[0].kind == "patch"


def test_run_post_sql_guards_follows_the_skill_declaration():
    sf = _sf(8)
    results = run_post_sql_guards(sf, guard_names=["series_length", "gap_ratio", "min_history", "max_horizon", "intermittent"], horizon=8)
    names = [g.name for g in results]
    assert names == ["series_length", "gap_ratio", "min_history", "max_horizon", "intermittent"]
    assert {g.name for g in failed(results)} == {"series_length", "max_horizon"}
    # max_horizon is skipped when the skill has no horizon.
    names2 = [g.name for g in run_post_sql_guards(sf, guard_names=["series_length", "max_horizon"])]
    assert names2 == ["series_length"]
