"""An incomplete trailing period is set aside, named, and never fed to a model.

Reproduces the AdventureWorks screenshot: 24 monthly SUM(salesamount) points
climbing to ~1.9M, the last a 51k stub. Before, Naive anchored on the stub and
projected 51k a month with an interval reaching -1.5M; now the stub is set
aside, the fit ends at the last complete month and the stub becomes the first
forecast period.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.contracts import SeriesRequest
from src.analysis.guards import horizon_cap, max_horizon
from src.analysis.narration import narrate
from src.analysis.runner import execute_skill
from src.analysis.series import (
    PARTIAL_TAIL_SLACK_DAYS,
    detect_partial_tail,
    partial_tail_sentence,
    period_last_day,
    prepare_series,
)
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload

pytestmark = pytest.mark.filterwarnings("ignore")

STUB = 50_841.0


def _sales(n: int = 24, *, tail: float = STUB):
    rng = np.random.default_rng(3)
    idx = pd.date_range("2006-08-01", periods=n, freq="MS")
    y = np.clip(np.linspace(420_000, 1_950_000, n) + rng.normal(0, 90_000, n), 300_000, None)
    y[-1] = tail
    return idx, y


def _req(**over) -> SeriesRequest:
    base = {"grain": "month", "agg": "sum", "measure_column": "salesamount", **over}
    return SeriesRequest(**series_request(**base))


# ── Detection rules ───────────────────────────────────────────────────────────


def test_period_last_day_for_each_grain():
    assert period_last_day(pd.Timestamp("2008-07-01"), "MS") == pd.Timestamp("2008-07-31")
    assert period_last_day(pd.Timestamp("2008-02-01"), "MS") == pd.Timestamp("2008-02-29")
    assert period_last_day(pd.Timestamp("2026-09-14"), "W-MON") == pd.Timestamp("2026-09-20")


def test_date_rule_sets_aside_a_month_whose_data_stops_early():
    idx, y = _sales(tail=1_900_000.0)  # the value alone gives nothing away
    sf = prepare_series(to_payload(idx, y)["rows"], _req(), data_end="2008-07-16 09:30:00")
    assert sf.partial_tail is not None and sf.partial_tail.basis == "date"
    assert sf.partial_tail.ts == pd.Timestamp("2008-07-01")
    assert sf.partial_tail.value == 1_900_000.0
    assert sf.partial_tail.reason == "data ends 16 Jul 2008, 16 of 31 days"
    assert sf.n == 23 and sf.frame.index[-1] == pd.Timestamp("2008-06-01")
    assert "Jul 2008 set aside as incomplete" in sf.missing_policy
    assert partial_tail_sentence(sf) == (
        "Jul 2008 is incomplete (data ends 16 Jul 2008, 16 of 31 days) and was set aside; "
        "the history used ends at Jun 2008."
    )


def test_date_rule_tolerates_a_weekend_at_the_end_of_the_period():
    idx, y = _sales(tail=1_900_000.0)
    rows = to_payload(idx, y)["rows"]
    # 29 Jul leaves 2 days: within the slack, the month counts as complete.
    complete_end = pd.Timestamp("2008-07-31") - pd.Timedelta(days=PARTIAL_TAIL_SLACK_DAYS)
    assert prepare_series(rows, _req(), data_end=complete_end).partial_tail is None
    # One more day short and it is not.
    assert prepare_series(rows, _req(), data_end=complete_end - pd.Timedelta(days=1)).partial_tail is not None
    # Data that runs to the last day is a complete month, whatever its value.
    assert prepare_series(rows, _req(), data_end="2008-07-31").partial_tail is None or \
        prepare_series(rows, _req(), data_end="2008-07-31").partial_tail.basis == "value"


def test_date_rule_never_applies_to_days_and_ignores_a_data_end_outside_the_tail():
    idx, y = seasonal_series(n=40, grain="day", amplitude=0)
    rows = to_payload(idx, y)["rows"]
    sf = prepare_series(rows, SeriesRequest(**series_request(grain="day")), data_end=idx[-1])
    assert sf.partial_tail is None and sf.n == 40
    idx, y = _sales(tail=1_900_000.0)
    # A data end before the tail period (calendar extended by request.end) is not "partial".
    sf = prepare_series(to_payload(idx, y)["rows"], _req(), data_end="2008-06-20")
    assert sf.partial_tail is None


def test_value_rule_catches_a_stub_month_when_no_data_end_is_known():
    idx, y = _sales()
    sf = prepare_series(to_payload(idx, y)["rows"], _req())
    assert sf.partial_tail is not None and sf.partial_tail.basis == "value"
    assert sf.partial_tail.value == STUB
    assert sf.partial_tail.reason.startswith("51k against a typical ") and sf.partial_tail.reason.endswith(" so far")
    assert sf.n == 23
    assert partial_tail_sentence(sf).startswith("Jul 2008 looks incomplete (51k against a typical ")
    assert partial_tail_sentence(sf).endswith("so far) and was set aside as a likely partial load; "
                                              "the history used ends at Jun 2008.")


def test_value_rule_still_applies_when_the_days_are_covered_and_says_so():
    # AdventureWorks' shape: the final month has rows on every day through the
    # 29th of a 30-day month, yet totals 3% of the usual. The date rule calls the
    # month complete; the value rule still sets it aside, and the reason names
    # both facts so the reader can judge.
    idx, y = _sales()  # ends Jul 2008 (31 days)
    sf = prepare_series(to_payload(idx, y)["rows"], _req(), data_end="2008-07-30")
    assert sf.partial_tail is not None and sf.partial_tail.basis == "value"
    assert sf.partial_tail.reason.endswith(", although its data runs to 30 Jul 2008")
    assert "so far" not in sf.partial_tail.reason
    assert sf.n == 23


def test_value_rule_spares_a_seasonal_trough():
    # An August shutdown every year: the tail is 10% of typical, but so was last
    # August. That is a season, not a stub, and stays in the history.
    idx = pd.date_range("2006-01-01", periods=32, freq="MS")  # Jan 2006 .. Aug 2008
    y = np.full(32, 1_000_000.0)
    y[idx.month == 8] = 100_000.0
    sf = prepare_series(to_payload(idx, y)["rows"], _req(), data_end="2008-08-31")
    assert sf.partial_tail is None and sf.n == 32
    # The same August without a precedent is set aside.
    y[19] = 1_000_000.0  # Aug 2007 back to normal
    sf = prepare_series(to_payload(idx, y)["rows"], _req(), data_end="2008-08-31")
    assert sf.partial_tail is not None and sf.partial_tail.basis == "value"


def test_calendar_never_runs_past_the_newest_source_row():
    # A requested end past the data would add zero-filled "future" months; those
    # are not zeros. The calendar stops at the period holding the newest row, and
    # that period is then judged (here: partial by date).
    idx, y = _sales(tail=900_000.0)  # Aug 2006 .. Jul 2008
    req = _req(start="2006-08-01", end="2008-11-01")
    without = prepare_series(to_payload(idx, y)["rows"], req)
    assert without.n == 27 and without.periods_filled == 3  # Aug..Oct 2008 zero-filled: the old behaviour
    sf = prepare_series(to_payload(idx, y)["rows"], req, data_end="2008-07-16")
    assert sf.n == 23 and sf.periods_filled == 0
    assert sf.partial_tail is not None and sf.partial_tail.ts == pd.Timestamp("2008-07-01")
    # Observed rows are never dropped even if data_end disagrees with them.
    sf = prepare_series(to_payload(idx, y)["rows"], req, data_end="2008-05-10")
    assert sf.frame.index[-1] == pd.Timestamp("2008-07-01")


def test_value_rule_leaves_a_decline_and_a_non_additive_series_alone():
    idx, y = _sales()
    y[-2] = 0.3 * float(np.median(y[-14:-2]))  # already falling before the tail: a trend
    assert prepare_series(to_payload(idx, y)["rows"], _req()).partial_tail is None
    idx, y = _sales()
    assert prepare_series(to_payload(idx, y)["rows"], _req(agg="avg")).partial_tail is None
    # A history with negatives (profit) has no "typical" share to compare against.
    idx, y = _sales()
    y[3] = -10.0
    assert prepare_series(to_payload(idx, y)["rows"], _req()).partial_tail is None


def test_value_rule_compares_days_with_the_same_weekday():
    # Weekday 1,000 / weekend 100: a Sunday tail is a quiet Sunday, not a stub.
    idx = pd.date_range("2026-08-03", periods=42, freq="D")  # Mon .. Sun
    y = np.where(idx.weekday >= 5, 100.0, 1000.0)
    sf = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request(grain="day")))
    assert sf.partial_tail is None
    # A Sunday at 10% of every other Sunday is.
    y[-1] = 10.0
    sf = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request(grain="day")))
    assert sf.partial_tail is not None and sf.partial_tail.basis == "value"
    # A Monday stub after a quiet Sunday: "the period before" is last Monday, not yesterday.
    idx = pd.date_range("2026-08-03", periods=43, freq="D")  # Mon .. Mon
    y = np.where(idx.weekday >= 5, 100.0, 1000.0)
    y[-1] = 10.0
    sf = prepare_series(to_payload(idx, y)["rows"], SeriesRequest(**series_request(grain="day")))
    assert sf.partial_tail is not None and sf.partial_tail.ts == idx[-1]


def test_unreadable_data_end_is_ignored():
    idx, y = _sales(tail=1_900_000.0)
    sf = prepare_series(to_payload(idx, y)["rows"], _req(), data_end="not a date")
    assert sf.partial_tail is None and sf.n == 24


def test_detect_partial_tail_needs_history_to_compare_against():
    frame = pd.DataFrame({"y": [100.0, 1.0], "observed": [True, True]},
                         index=pd.date_range("2026-01-01", periods=2, freq="MS"))
    assert detect_partial_tail(frame, "month", "MS", _req()) is None


# ── Guards ────────────────────────────────────────────────────────────────────


def test_horizon_cap_counts_the_set_aside_period_as_history():
    idx, y = _sales()
    rows = to_payload(idx, y)["rows"]
    with_stub = prepare_series(rows, _req())  # 23 complete + 1 set aside
    assert with_stub.n == 23 and horizon_cap(with_stub) == 8
    assert max_horizon(with_stub, 8).passed
    trimmed = prepare_series(rows[:-1], _req())  # a real 23-month window
    assert trimmed.partial_tail is None and horizon_cap(trimmed) == 7


# ── End to end through the runner ─────────────────────────────────────────────


def _forecast(payload, **context):
    out = execute_skill("forecast", {"series": series_request(grain="month", agg="sum", measure_column="salesamount"),
                                     "horizon": 8}, payload, context=context or None)
    assert out.status == "ok", (out.status, out.error, [g.detail for g in out.guard_results if not g.passed])
    return out.envelope


def test_forecast_sets_the_stub_aside_and_starts_the_horizon_there():
    idx, y = _sales()
    env = _forecast(to_payload(idx, y), data_end="2008-07-01")

    assert env.egress.rows_sent_to_model == 23 and env.provenance.span_end == "2008-06-01"
    history = [r for r in env.rows if not r["is_forecast"]]
    forecast = [r for r in env.rows if r["is_forecast"]]
    assert history[-1]["ts"] == "2008-06-01" and len(forecast) == 8
    assert forecast[0]["ts"] == "2008-07-01" and forecast[-1]["ts"] == "2009-02-01"
    assert env.chart_spec.forecast_start == "2008-07-01"
    # The stub never reaches the model: the first forecast is a full month, not 51k.
    assert forecast[0]["forecast"] > 1_000_000
    assert all(r["lower"] >= 0 for r in forecast)

    tail = env.facts["partial_tail"]
    assert tail["ts"] == "2008-07-01" and tail["period"] == "Jul 2008" and tail["basis"] == "date"
    assert tail["value"] == STUB and tail["forecast"] == pytest.approx(forecast[0]["forecast"], abs=1e-3)
    assert env.facts["last_actual"]["ts"] == "2008-06-01"

    assert env.caveats[0].startswith("Jul 2008 is incomplete (data ends 1 Jul 2008, 1 of 31 days) and was set aside; "
                                     "the history used ends at Jun 2008. The forecast for the whole of Jul 2008 is ")
    assert sum("incomplete" in c for c in env.caveats) == 1  # the runner does not add a second sentence
    guard = next(g for g in env.guard_results if g.name == "partial_tail")
    assert guard.passed and "Jul 2008 set aside as incomplete" in guard.detail and "23 complete months" in guard.detail
    assert next(g for g in env.guard_results if g.name == "max_horizon").passed

    # Key insights carry the engine's sentence exactly once.
    findings = narrate(env.model_dump(mode="json")).findings
    assert sum(f == env.caveats[0] for f in findings) == 1
    assert sum("incomplete" in f for f in findings) == 1


def test_forecast_value_rule_is_the_fallback_without_a_data_end():
    idx, y = _sales()
    env = _forecast(to_payload(idx, y))
    assert env.facts["partial_tail"]["basis"] == "value"
    assert env.caveats[0].startswith("Jul 2008 looks incomplete (51k against a typical ")
    assert "set aside as a likely partial load" in env.caveats[0]
    assert [r for r in env.rows if r["is_forecast"]][0]["ts"] == "2008-07-01"


def test_forecast_keeps_a_complete_tail():
    idx, y = _sales(tail=1_900_000.0)
    env = _forecast(to_payload(idx, y), data_end="2008-07-31")
    assert "partial_tail" not in env.facts and env.egress.rows_sent_to_model == 24
    assert [r for r in env.rows if r["is_forecast"]][0]["ts"] == "2008-08-01"
    assert not any(g.name == "partial_tail" for g in env.guard_results)


def test_other_series_skills_get_the_shared_sentence():
    idx, y = _sales(n=36)  # Aug 2006 .. Jul 2009
    out = execute_skill("anomaly_detection", {"series": series_request(grain="month", agg="sum", measure_column="salesamount")},
                        to_payload(idx, y), context={"data_end": "2009-07-16"})
    assert out.status == "ok"
    env = out.envelope
    assert env.facts["n_points"] == 35
    assert any(c == "Jul 2009 is incomplete (data ends 16 Jul 2009, 16 of 31 days) and was set aside; "
                    "the history used ends at Jun 2009." for c in env.caveats)
    # The stub is not a flagged anomaly because it is not in the analysed series.
    assert "2009-07-01" not in {f["ts"] for f in env.facts["flagged"]}
    assert next(g for g in env.guard_results if g.name == "partial_tail").passed


def test_multi_series_names_the_series_whose_tail_was_set_aside():
    # Only the largest series' caveats travel on a grouped result; a stub in a
    # smaller series must still be named somewhere the reader will see it.
    idx, big = _sales(tail=1_900_000.0)
    _, small = _sales(tail=STUB)
    small = small / 4.0
    small[-1] = STUB
    rows = [{"series_id": "A", "ts": d.date().isoformat(), "value": float(v)} for d, v in zip(idx, big)]
    rows += [{"series_id": "B", "ts": d.date().isoformat(), "value": float(v)} for d, v in zip(idx, small)]
    out = execute_skill("forecast", {"series": series_request(grain="month", agg="sum", measure_column="salesamount",
                                                              group_by="Territory"), "horizon": 6},
                        {"columns": ["series_id", "ts", "value"], "rows": rows}, context={"data_end": "2008-07-31"})
    assert out.status == "ok", out.error
    env = out.envelope
    assert env.facts["top_series"] == "A"
    assert any(g.name == "partial_tail[B]" for g in env.guard_results)
    assert not any(g.name == "partial_tail[A]" for g in env.guard_results)
    assert any(c.startswith("An incomplete last period was set aside for 1 series (B)") for c in env.caveats)
    assert env.facts["series"]["B"]["partial_tail"]["period"] == "Jul 2008"
    assert "partial_tail" not in env.facts["series"]["A"]


# ── Non-negative floor ────────────────────────────────────────────────────────


def test_forecast_floors_a_non_negative_measure_at_zero():
    # Small, noisy counts: Naive's Gaussian interval would go below zero within a few steps.
    rng = np.random.default_rng(5)
    idx = pd.date_range("2025-01-06", periods=30, freq="W-MON")
    y = np.clip(rng.normal(8, 6, 30), 0, None)
    out = execute_skill("forecast", {"series": series_request(agg="count"), "horizon": 8, "method": "seasonal_naive"},
                        to_payload(idx, y))
    assert out.status == "ok"
    env = out.envelope
    forecast = [r for r in env.rows if r["is_forecast"]]
    assert all(r["lower"] >= 0 and r["forecast"] >= 0 for r in forecast)
    assert env.facts["floored_at_zero"] is True
    assert any("floored at zero" in n for n in env.details.notes)
    assert all(r["lower"] <= r["forecast"] <= r["upper"] for r in forecast)
