"""prepare_series: calendar reindex, missingness mask, fill policy, seasonality."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.contracts import SeriesRequest
from src.analysis.series import (
    candidate_periods,
    floor_to_grain,
    format_period,
    pandas_freq,
    prepare_series,
    seasonal_strength,
    series_payload,
)
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload, white_noise


def test_floor_to_grain_snaps_week_to_monday_and_month_to_first():
    ts = pd.Series(pd.to_datetime(["2026-09-16", "2026-09-14", "2026-09-20"]))  # Wed, Mon, Sun
    monday = floor_to_grain(ts, "week", "monday")
    assert monday.dt.date.tolist() == [pd.Timestamp("2026-09-14").date()] * 3
    sunday = floor_to_grain(ts, "week", "sunday")
    assert sunday.dt.date.tolist()[0] == pd.Timestamp("2026-09-13").date()
    assert floor_to_grain(ts, "month").dt.day.tolist() == [1, 1, 1]
    assert pandas_freq("week", "sunday") == "W-SUN" and pandas_freq("month") == "MS"


def test_additive_gap_is_zero_filled_and_masked():
    idx, y = seasonal_series(n=30, grain="week", amplitude=0)
    payload = to_payload(idx, y, drop=(5, 6))
    sf = prepare_series(payload["rows"], SeriesRequest(**series_request(agg="sum")))
    assert sf.n == 30
    assert sf.periods_filled == 2 and sf.periods_observed == 28
    assert not sf.frame["observed"].iloc[5] and sf.frame["y"].iloc[5] == 0.0
    assert sf.frame["observed"].iloc[7]
    assert "zero-filled" in sf.missing_policy
    assert abs(sf.gap_ratio - 2 / 30) < 1e-9


def test_non_additive_gap_stays_nan():
    idx, y = seasonal_series(n=24, grain="month", amplitude=0)
    payload = to_payload(idx, y, drop=(3,))
    sf = prepare_series(payload["rows"], SeriesRequest(**series_request(agg="avg", grain="month")))
    assert sf.n == 24 and np.isnan(sf.frame["y"].iloc[3])
    assert "left empty" in sf.missing_policy
    # Engines get a full vector anyway.
    assert not sf.y_filled().isna().any()


def test_positional_rows_and_duplicate_periods_are_handled():
    rows = [["2026-01-05", 10], ["2026-01-07", 5], ["2026-01-12", 7]]  # two rows in the same week
    sf = prepare_series(rows, SeriesRequest(**series_request(agg="sum")), columns=["ts", "value"])
    assert sf.n == 2 and sf.frame["y"].tolist() == [15.0, 7.0]


def test_requested_range_extends_calendar():
    idx, y = seasonal_series(n=10, grain="day", amplitude=0)
    req = SeriesRequest(**series_request(grain="day", start="2023-12-30", end="2024-01-13"))
    sf = prepare_series(to_payload(idx, y)["rows"], req)
    # 2023-12-30 .. 2024-01-12 inclusive = 14 days; observed 10.
    assert sf.n == 14 and sf.periods_observed == 10


def test_candidate_periods_need_more_than_two_cycles():
    # STL cannot decompose a period of exactly n/2, so the boundary is 2·m + 1.
    assert candidate_periods("week", 104) == []
    assert candidate_periods("week", 105) == [52]
    assert candidate_periods("month", 24) == [] and candidate_periods("month", 25) == [12]
    assert candidate_periods("day", 100) == [7]
    assert candidate_periods("day", 730) == [7] and candidate_periods("day", 800) == [7, 365]


def test_seasonality_is_confirmed_only_when_present():
    idx, y = seasonal_series(n=130, grain="week")
    sf = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request()))
    assert sf.seasonal_periods == [52] and sf.seasonal_strength[52] >= 0.3

    idx, y = white_noise(n=130, grain="week")
    sf = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request()))
    assert sf.candidate_periods == [52] and sf.seasonal_periods == []


def test_seasonal_strength_edge_cases():
    assert seasonal_strength([1.0] * 30, 7) == 0.0
    assert seasonal_strength([1.0, 2.0], 7) == 0.0


def test_series_payload_is_json_safe_and_labels_are_human():
    idx, y = seasonal_series(n=3, grain="month", amplitude=0)
    sf = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request(grain="month")))
    payload = series_payload(sf)
    assert payload["columns"] == ["ts", "value", "observed"]
    assert payload["rows"][0]["ts"] == "2024-01-01" and payload["rows"][0]["observed"] is True
    assert format_period(pd.Timestamp("2026-02-18"), "week") == "week of 18 Feb 2026"
    assert format_period(pd.Timestamp("2026-02-18"), "month") == "Feb 2026"
    assert format_period(pd.Timestamp("2026-02-18"), "day") == "18 Feb 2026"


def test_empty_rows_yield_empty_frame():
    sf = prepare_series([], SeriesRequest(**series_request()))
    assert sf.n == 0 and sf.gap_ratio == 1.0
