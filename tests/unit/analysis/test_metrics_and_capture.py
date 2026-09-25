"""The measurable slice of an envelope (``result_metrics``) and the forecast
capture shape (``forecast_capture``) — both read real engine output."""

from __future__ import annotations

import pytest

from src.agent.analysis_store import InMemoryAnalysisStore, forecast_capture
from src.analysis.metrics_view import result_metrics
from src.analysis.runner import execute_skill
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload

pytestmark = pytest.mark.filterwarnings("ignore")


def _forecast_env(n=130, grain="week", horizon=8, **params):
    idx, y = seasonal_series(n=n, grain=grain)
    out = execute_skill("forecast", {"series": series_request(grain=grain), "window": n, "horizon": horizon, **params},
                        to_payload(idx, y))
    assert out.status == "ok", out.error
    return out.envelope


def test_result_metrics_reads_the_forecast_facts():
    env = _forecast_env()
    m = result_metrics(env)
    assert m["skill"] == "forecast" and m["method"] == env.method_used
    assert m["metric"] == "WAPE" and m["band"] in ("good", "fair", "poor")
    assert m["cv_windows"] == env.facts["cv_windows"] and m["horizon"] == 8
    assert m["interval_method"] in ("conformal_scaled", "conformal_absolute", "native")
    assert m["baseline"] == "SeasonalNaive"
    assert m["baseline_won"] == (env.method_used == "SeasonalNaive")
    assert m["intermittent"] == {"passed": True, "observed": pytest.approx(0.0), "required": 0.5}
    assert m["overridden_guards"] == []
    # The dict form works too (that is what the log line and the backtest see).
    assert result_metrics(env.model_dump(mode="json"))["method"] == env.method_used


def test_forecast_capture_shapes_run_and_points():
    env = _forecast_env()
    params = {"series": {**series_request(), "filters": [{"table": "FactInternetSales", "column": "Region", "op": "equals", "value": "EMEA"}]},
              "window": 130, "horizon": 8, "interval": 0.8, "method": "auto"}
    shaped = forecast_capture(env.model_dump(mode="json"), params)
    assert shaped is not None
    run, points = shaped["run"], shaped["points"]
    assert len(points) == 8 and all(p["series_id"] == "" for p in points)
    assert points[0]["ts"] == env.facts["forecast_first"]["ts"]
    assert run["grain"] == "week" and run["horizon"] == 8 and run["interval_level"] == 0.8
    assert run["method"] == env.method_used and run["metric"] == "WAPE"
    assert run["mase_scale"] == env.facts["mase_scale"] and run["mase_scale"] > 0
    assert run["interval_method"] == env.facts["interval_method"]
    assert run["history_end"] == env.facts["last_actual"]["ts"]
    assert run["engine_hash"] == env.engine.module_hash
    # The ORIGINAL params travel, filter value included — the envelope's copy is redacted.
    assert run["params"]["series"]["filters"][0]["value"] == "EMEA"
    assert run["multi_series"] is False


def test_forecast_capture_ignores_other_skills_and_empty_forecasts():
    assert forecast_capture({"skill": "anomaly_detection", "rows": [{"is_forecast": False}]}, {}) is None
    assert forecast_capture({"skill": "forecast", "rows": [{"is_forecast": False, "actual": 1}]}, {}) is None


def test_forecast_backtest_scores_a_case_against_its_withheld_truth():
    """The evals harness that gates engine changes stays runnable: one seeded
    case through the production path, realized error and coverage computed."""
    from evals.forecast_backtest import BacktestReport, generate, run_case

    case = {"id": "smoke", "generator": "seasonal", "grain": "month", "n": 36, "horizon": 6, "interval": 0.8,
            "level": 50000, "amplitude": 12000, "period": 12, "trend": 400, "noise": 2500, "seed": 22}
    assert len(generate(case)) == 42
    score = run_case(case)
    assert score.status == "ok", score.detail
    assert score.realized_metric == "WAPE" and 0 <= score.realized_error < 1
    assert 0 <= score.realized_coverage <= 1
    assert score.engine["skill"] == "forecast" and score.engine["cv_windows"] >= 1
    report = BacktestReport(scores=[score])
    agg = report.aggregate()
    assert agg["ok"] == 1 and agg["mean_realized_error"] == round(score.realized_error, 4)
    assert "smoke" in report.summary()


@pytest.mark.asyncio
async def test_in_memory_store_records_and_evaluates_a_forecast():
    env = _forecast_env()
    store = InMemoryAnalysisStore()
    ok = await store.record_forecast(query_id="q1", user_id="u", source_key="db", session_id=None,
                                     params={"series": series_request(), "window": 130, "horizon": 8}, envelope=env.model_dump(mode="json"))
    assert ok is True
    run = await store.get_forecast_run("q1", user_id="u", source_key="db")
    assert run is not None and len(run["points"]) == 8 and run["evaluation"] is None
    assert await store.get_forecast_run("q1", user_id="someone-else", source_key="db") is None
    await store.save_forecast_evaluation("q1", user_id="u", evaluation={"realized": {"value": 0.1}})
    run = await store.get_forecast_run("q1", user_id="u", source_key="db")
    assert run["evaluation"] == {"realized": {"value": 0.1}} and run["evaluated_at"]
