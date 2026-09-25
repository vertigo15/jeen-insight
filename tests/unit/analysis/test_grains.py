"""Grain arithmetic: a grain patch keeps the horizon and window on the same calendar span."""

from __future__ import annotations

from src.analysis.grains import HORIZON_BOUNDS, WINDOW_BOUNDS, convert_periods, grain_patch


def test_convert_periods_keeps_the_calendar_span():
    assert convert_periods(14, "day", "week") == 2
    assert convert_periods(8, "week", "month") == 2
    assert convert_periods(6, "month", "week") == 26
    assert convert_periods(3, "week", "day") == 21
    assert convert_periods(None, "day", "week") is None
    # Never below one period.
    assert convert_periods(1, "day", "month") == 1


def test_convert_periods_clamps_to_the_contract_bounds():
    assert convert_periods(1000, "day", "week", WINDOW_BOUNDS) == 143
    assert convert_periods(3, "week", "month", WINDOW_BOUNDS) == WINDOW_BOUNDS[0]
    assert convert_periods(2000, "day", "day", HORIZON_BOUNDS) == HORIZON_BOUNDS[1]


def test_grain_patch_restates_horizon_and_window():
    patch = grain_patch("day", "week", horizon=14, window=120)
    assert patch == {"series": {"grain": "week"}, "horizon": 2, "window": 17}
    # Absent counts stay absent (the guard fills the new grain's default).
    assert grain_patch("week", "month") == {"series": {"grain": "month"}}
    assert grain_patch("week", "month", window=26) == {"series": {"grain": "month"}, "window": 12}
