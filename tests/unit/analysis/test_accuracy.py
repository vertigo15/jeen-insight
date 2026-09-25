"""Realized accuracy: only fully elapsed periods are scored, the engine's metric
rule is reused, and the band's coverage is measured against what was shown."""

from __future__ import annotations

from datetime import date

from src.analysis.accuracy import period_end, realized_accuracy, split_elapsed

_POINTS = [
    {"ts": "2026-06-01", "forecast": 100.0, "lower": 90.0, "upper": 110.0},
    {"ts": "2026-06-08", "forecast": 120.0, "lower": 100.0, "upper": 140.0},
    {"ts": "2026-06-15", "forecast": 130.0, "lower": 110.0, "upper": 150.0},
]


def test_period_end_per_grain():
    assert period_end(date(2026, 6, 1), "day") == date(2026, 6, 1)
    assert period_end(date(2026, 6, 1), "week") == date(2026, 6, 7)
    assert period_end(date(2026, 2, 1), "month") == date(2026, 2, 28)
    assert period_end(date(2026, 12, 1), "month") == date(2026, 12, 31)


def test_split_elapsed_excludes_the_incomplete_current_period():
    split = split_elapsed(_POINTS, "week", date(2026, 6, 16))  # inside the third week
    assert [p["ts"] for p in split["elapsed"]] == ["2026-06-01", "2026-06-08"]
    assert [p["ts"] for p in split["pending"]] == ["2026-06-15"]
    # No data end known: nothing is scored.
    assert split_elapsed(_POINTS, "week", None)["elapsed"] == []


def test_realized_accuracy_wape_and_coverage():
    out = realized_accuracy(_POINTS[:2], {"2026-06-01": 95.0, "2026-06-08": 160.0}, metric="WAPE",
                            mase_scale=None, interval_level=0.8)
    assert out["n"] == 2 and out["metric"] == "WAPE"
    assert out["value"] == round((5 + 40) / (95 + 160), 4)
    assert out["mae"] == 22.5
    assert out["coverage"] == 0.5 and out["coverage_n"] == 2
    inside = {p["ts"]: p["inside"] for p in out["points"]}
    assert inside == {"2026-06-01": True, "2026-06-08": False}


def test_realized_accuracy_mase_on_the_frozen_scale_and_missing_actual_is_zero():
    out = realized_accuracy(_POINTS[:2], {"2026-06-01": 95.0}, metric="MASE", mase_scale=10.0, interval_level=0.8)
    # The second period had no rows: an additive measure counts it as zero.
    assert out["points"][1]["actual"] == 0.0
    assert out["metric"] == "MASE" and out["value"] == round(((5 + 120) / 2) / 10.0, 4)
    assert out["band"] in ("good", "fair", "poor")


def test_realized_accuracy_empty():
    out = realized_accuracy([], {}, metric="WAPE", mase_scale=None, interval_level=0.8)
    assert out["n"] == 0 and out["value"] is None and out["coverage"] is None
