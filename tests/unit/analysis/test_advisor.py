"""Adjustments to try: derived from real engine envelopes, every patch executable.

The advisor is exercised on actual ``execute_skill`` output rather than
hand-written facts so that a change to what the engine emits (selection
facts, coverage semantics, method names) is caught here, not in the browser.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from src.analysis.advisor import suggest_adjustments
from src.analysis.contracts import merge_params_patch
from src.analysis.runner import execute_skill
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload, white_noise

pytestmark = pytest.mark.filterwarnings("ignore")


def _forecast(params, payload, **kw):
    out = execute_skill("forecast", params, payload, **kw)
    assert out.status == "ok", (out.status, out.error)
    return out.envelope.model_dump(mode="json")


def _assert_executable(skill, env, adjustments):
    """Every offered patch validates against the run's own params and changes something."""
    base = env["params"]
    for item in adjustments:
        merged = merge_params_patch(skill, base, item["params_patch"]).model_dump(mode="json")
        assert merged != base, item


def test_advisor_imports_without_pandas():
    # It runs in the API process on every ML answer; pulling the analytics
    # stack in would cost every request. Checked in a fresh interpreter so
    # this test cannot disturb the modules already loaded here.
    import subprocess

    code = "import sys; import src.analysis.advisor; assert 'pandas' not in sys.modules, 'advisor imported pandas'"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_short_seasonal_history_suggests_a_wider_window_and_theta_when_the_baseline_won():
    # 26 weeks cannot test a 52-week season; the window (26) was the limit.
    idx, y = seasonal_series(n=26, grain="week", seed=11)
    env = _forecast({"series": series_request(), "window": 26, "horizon": 8}, to_payload(idx, y))
    adjustments = suggest_adjustments(env)
    codes = [a["code"] for a in adjustments]
    assert "wider_window_for_season" in codes
    wider = next(a for a in adjustments if a["code"] == "wider_window_for_season")
    assert wider["params_patch"]["window"] >= 105  # 2 × 52 + 1: enough to test the yearly cycle
    if env["facts"]["selection"]["baseline_won"]:
        assert "try_theta" in codes
    else:
        assert "try_theta" not in codes
    assert all(a["code"] != "try_auto_ets" for a in adjustments)  # AutoETS already ran and lost
    _assert_executable("forecast", env, adjustments)


def test_window_that_was_not_the_limit_gets_no_wider_window_chip():
    # The window asked for 60 weeks but the source only had 26: widening would
    # re-run the same series and offer the same chip again.
    idx, y = seasonal_series(n=26, grain="week", seed=11)
    env = _forecast({"series": series_request(), "window": 60, "horizon": 8}, to_payload(idx, y))
    assert all(a["code"] not in ("wider_window_for_season", "wider_window_for_band") for a in suggest_adjustments(env))


def test_poor_band_suggests_a_coarser_grain_with_horizon_and_window_converted():
    rng = np.random.default_rng(5)
    idx, _ = white_noise(n=120, grain="day")
    # Mostly-zero, spiky daily history: WAPE lands in the poor band.
    y = np.where(rng.uniform(size=120) < 0.55, 0.0, rng.uniform(5, 30, 120))
    env = _forecast({"series": series_request(grain="day"), "window": 120, "horizon": 14}, to_payload(idx, y))
    adjustments = suggest_adjustments(env)
    if env["validation"]["band"] == "poor":
        coarser = next(a for a in adjustments if a["code"] == "coarser_grain")
        assert coarser["recommended"] is True
        assert coarser["params_patch"] == {"series": {"grain": "week"}, "horizon": 2, "window": 17}
    _assert_executable("forecast", env, adjustments)


def test_good_long_history_offers_nothing_misleading():
    idx, y = seasonal_series(n=130, grain="week")
    env = _forecast({"series": series_request(), "window": 130, "horizon": 8}, to_payload(idx, y))
    adjustments = suggest_adjustments(env)
    assert all(a["code"] != "coarser_grain" for a in adjustments)
    assert len(adjustments) <= 4
    _assert_executable("forecast", env, adjustments)


def test_overridden_guard_exits_are_replayed_once_and_only_executable_ones():
    idx, y = seasonal_series(n=9, grain="month", amplitude=0)
    out = execute_skill("forecast", {"series": series_request(grain="month"), "window": 12, "horizon": 2},
                        to_payload(idx, y), override_guards=True)
    assert out.status == "ok"
    env = out.envelope.model_dump(mode="json")
    adjustments = suggest_adjustments(env)
    replayed = [a for a in adjustments if a["code"] == "guard_exit"]
    assert replayed, [g["name"] for g in env["guard_results"] if not g["passed"]]
    patches = [str(a["params_patch"]) for a in replayed]
    assert len(patches) == len(set(patches))  # deduplicated
    assert all(a["params_patch"] for a in replayed)  # never an empty patch
    _assert_executable("forecast", env, adjustments)


def test_multi_series_and_unpatchable_skills_get_nothing():
    assert suggest_adjustments({"skill": "cohort_retention", "params": {"cohort": {}}}) == []
    assert suggest_adjustments({"skill": "experiment_test", "params": {"experiment": {}}}) == []
    assert suggest_adjustments(None) == []
    multi = {"skill": "forecast", "params": {"series": {**series_request(), "group_by": "Region"}, "window": 26},
             "facts": {"series": {"A": {}, "B": {}}, "top_series": "A"}, "validation": {"band": "poor"}}
    assert all(a["code"] == "guard_exit" for a in suggest_adjustments(multi))
