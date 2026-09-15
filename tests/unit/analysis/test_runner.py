"""Runner boundary: outcomes round-trip through JSON, the subprocess runner
produces the same envelope as in-process, times out cleanly, and is refused
outside development mode."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from src.analysis.runner import (
    InProcessRunner,
    LocalSubprocessRunner,
    RunOutcome,
    build_runner,
    execute_skill,
)
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload

pytestmark = pytest.mark.filterwarnings("ignore")

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def test_run_outcome_json_round_trip():
    idx, y = seasonal_series(n=30, grain="week", amplitude=0)
    out = execute_skill("anomaly_detection", {"series": series_request()}, to_payload(idx, y))
    again = RunOutcome.from_json(out.to_json())
    assert again.status == "ok" and again.envelope.model_dump() == out.envelope.model_dump()

    refused = execute_skill("forecast", {"series": series_request(), "horizon": 20}, to_payload(idx, y))
    again = RunOutcome.from_json(refused.to_json())
    assert again.status == "guard_failed" and [g.name for g in again.guard_results] == [g.name for g in refused.guard_results]


def test_in_process_runner_matches_execute_skill():
    idx, y = seasonal_series(n=30, grain="week", amplitude=0)
    payload = to_payload(idx, y)
    direct = execute_skill("anomaly_detection", {"series": series_request()}, payload, context={"runner": "in_process"})
    via = asyncio.run(InProcessRunner().run("anomaly_detection", {"series": series_request()}, payload))
    assert via.ok and via.envelope.rows == direct.envelope.rows
    assert via.envelope.engine.runner == "in_process"


@pytest.mark.slow
def test_local_subprocess_runner_produces_identical_rows():
    idx, y = seasonal_series(n=30, grain="week", amplitude=0)
    payload = to_payload(idx, y)
    runner = LocalSubprocessRunner(timeout_seconds=120, python=sys.executable, cwd=_REPO)
    out = asyncio.run(runner.run("anomaly_detection", {"series": series_request()}, payload))
    assert out.ok, out.error
    direct = execute_skill("anomaly_detection", {"series": series_request()}, payload)
    assert out.envelope.rows == direct.envelope.rows
    assert out.envelope.engine.runner == "local_subprocess"


def test_local_subprocess_runner_times_out_cleanly(monkeypatch):
    idx, y = seasonal_series(n=30, grain="week", amplitude=0)
    runner = LocalSubprocessRunner(timeout_seconds=1, python=sys.executable, cwd=_REPO)
    # Point the worker at a sleeping process so the timeout path is exercised fast.
    original = asyncio.create_subprocess_exec

    def _sleepy(*_args, **kwargs):
        return original(sys.executable, "-c", "import time; time.sleep(5)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _sleepy)
    out = asyncio.run(runner.run("anomaly_detection", {"series": series_request()}, to_payload(idx, y)))
    assert out.status == "error" and "timed out" in out.error


def test_build_runner_refuses_local_outside_dev_mode():
    assert build_runner(SimpleNamespace(ANALYSIS_RUNNER="local", JEEN_DEV_MODE=False)) is None
    assert build_runner(SimpleNamespace(ANALYSIS_RUNNER="in_process", JEEN_DEV_MODE=False)) is None
    assert isinstance(build_runner(SimpleNamespace(ANALYSIS_RUNNER="local", JEEN_DEV_MODE=True)), LocalSubprocessRunner)
    assert isinstance(build_runner(SimpleNamespace(ANALYSIS_RUNNER="in_process", JEEN_DEV_MODE=True)), InProcessRunner)
    assert build_runner(SimpleNamespace(ANALYSIS_RUNNER="bogus", JEEN_DEV_MODE=True)) is None
