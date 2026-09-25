"""Forecast backtest: realized accuracy of the production forecast path.

Unit tests prove an engine change runs; they cannot say whether it forecasts
better. This harness can. Every case in ``evals/datasets/forecast_backtest_set.yaml``
is a seeded synthetic series with a *known* future: the first ``n`` periods go
through ``execute_skill("forecast", …)`` exactly as a query's aggregate rows
would (series preparation → guards → statsforecast engine), and the returned
horizon is scored against the withheld truth:

* **realized error** — WAPE for a non-negative series, MASE otherwise (the same
  rule the engine uses for its own CV score, so the two are comparable);
* **realized coverage** — the share of withheld points inside the returned
  ``[lower, upper]`` band, against the nominal ``interval``;
* the engine's own view of itself — CV metric/band, ``cv_windows``, whether the
  baseline won, the interval method — via :func:`result_metrics`, so a run can
  be checked against what the ``analysis_result`` log line would have said.

Run::

    python -m evals.forecast_backtest                       # print the report
    python -m evals.forecast_backtest --out /tmp/after.json # save it
    python -m evals.forecast_backtest --baseline /tmp/before.json
                                                            # show deltas vs a saved run

Deterministic and offline: no database, no LLM, no network. It needs the
analytics dependencies (statsforecast etc.), i.e. the same environment the
unit tests for ``src/analysis`` run in.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from src.analysis.engines.common import coverage as band_coverage
from src.analysis.engines.common import mase, wape, wape_applicable
from src.analysis.metrics_view import result_metrics
from src.analysis.runner import execute_skill

logger = logging.getLogger(__name__)

DEFAULT_DATASET = Path(__file__).resolve().parent / "datasets" / "forecast_backtest_set.yaml"
# Fixed anchor so the calendar (and therefore the partial-tail logic) is the
# same on every run.
_ANCHOR = date(2024, 1, 1)


# ── Synthetic generators ─────────────────────────────────────────────────────


def _dates(grain: str, count: int) -> List[date]:
    if grain == "day":
        return [_ANCHOR + timedelta(days=i) for i in range(count)]
    if grain == "week":
        return [_ANCHOR + timedelta(weeks=i) for i in range(count)]  # 2024-01-01 is a Monday
    if grain == "month":
        out = []
        y, m = _ANCHOR.year, _ANCHOR.month
        for _ in range(count):
            out.append(date(y, m, 1))
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out
    raise ValueError(f"unknown grain {grain!r}")


def generate(case: Dict[str, Any]) -> np.ndarray:
    """``n + horizon`` values for one case; deterministic in ``seed``."""
    rng = np.random.default_rng(int(case.get("seed", 0)))
    total = int(case["n"]) + int(case["horizon"])
    t = np.arange(total, dtype=float)
    kind = str(case.get("generator", "seasonal"))
    level = float(case.get("level", 1000.0))
    noise = float(case.get("noise", 0.0))
    trend = float(case.get("trend", 0.0))
    eps = rng.normal(0.0, noise, total) if noise > 0 else np.zeros(total)

    if kind == "seasonal":
        period = float(case.get("period", 12))
        amplitude = float(case.get("amplitude", 0.0))
        y = level + trend * t + amplitude * np.sin(2 * np.pi * t / period) + eps
    elif kind == "trend":
        y = level + trend * t + eps
    elif kind == "noise":
        y = level + eps
    elif kind == "level_shift":
        shift_at = int(case.get("shift_at", total // 2))
        shift_by = float(case.get("shift_by", level * 0.3))
        y = level + eps
        y[shift_at:] += shift_by
    elif kind == "intermittent":
        zero_share = float(case.get("zero_share", 0.5))
        demand = rng.uniform(0.6, 1.4, total) * level + eps
        mask = rng.uniform(0.0, 1.0, total) < zero_share
        y = np.where(mask, 0.0, demand)
    else:
        raise ValueError(f"unknown generator {kind!r}")
    # Business measures the engine forecasts (sales, counts) never go below zero.
    return np.maximum(y, 0.0)


# ── Scoring ──────────────────────────────────────────────────────────────────


@dataclass
class CaseScore:
    case_id: str
    grain: str
    n: int
    horizon: int
    interval: float
    status: str                      # ok | guard_failed | error
    detail: str = ""
    realized_metric: Optional[str] = None
    realized_error: Optional[float] = None
    realized_coverage: Optional[float] = None
    engine: Dict[str, Any] = field(default_factory=dict)   # result_metrics(envelope)


@dataclass
class BacktestReport:
    scores: List[CaseScore] = field(default_factory=list)

    def ok(self) -> List[CaseScore]:
        return [s for s in self.scores if s.status == "ok"]

    def aggregate(self) -> Dict[str, Any]:
        ok = self.ok()
        errors = [s.realized_error for s in ok if s.realized_error is not None]
        cov_gap = [s.realized_coverage - s.interval for s in ok if s.realized_coverage is not None]
        cv_windows = [int(s.engine.get("cv_windows") or 0) for s in ok]
        return {
            "cases": len(self.scores),
            "ok": len(ok),
            "refused": sum(1 for s in self.scores if s.status == "guard_failed"),
            "errors": sum(1 for s in self.scores if s.status == "error"),
            "mean_realized_error": round(statistics.fmean(errors), 4) if errors else None,
            "median_realized_error": round(statistics.median(errors), 4) if errors else None,
            # Negative = the band under-covers its nominal level.
            "mean_coverage_gap": round(statistics.fmean(cov_gap), 4) if cov_gap else None,
            "under_covering_cases": sum(1 for g in cov_gap if g < -0.10),
            "baseline_won": sum(1 for s in ok if s.engine.get("baseline_won")),
            "mean_cv_windows": round(statistics.fmean(cv_windows), 2) if cv_windows else None,
            "zero_cv_window_cases": sum(1 for w in cv_windows if w == 0),
        }

    def to_json(self) -> Dict[str, Any]:
        return {"aggregate": self.aggregate(), "cases": [asdict(s) for s in self.scores]}

    def summary(self, baseline: Optional[Dict[str, Any]] = None) -> str:
        agg = self.aggregate()
        lines = ["Forecast backtest", "-" * 72]
        head = f"{'case':<28} {'st':<6} {'err':>8} {'cov':>6} {'nom':>5} {'cv':>3} {'method':<16} {'band'}"
        lines.append(head)
        prior_cases = {c["case_id"]: c for c in (baseline or {}).get("cases", [])}
        for s in self.scores:
            err = f"{s.realized_error:.3f}" if s.realized_error is not None else "  n/a"
            cov = f"{s.realized_coverage:.2f}" if s.realized_coverage is not None else " n/a"
            delta = ""
            prior = prior_cases.get(s.case_id)
            if prior and prior.get("realized_error") is not None and s.realized_error is not None:
                d = s.realized_error - prior["realized_error"]
                delta = f"  Δerr {d:+.3f}"
            lines.append(
                f"{s.case_id:<28} {s.status[:6]:<6} {err:>8} {cov:>6} {s.interval:>5.2f} "
                f"{str(s.engine.get('cv_windows', '-')):>3} {str(s.engine.get('method') or '-')[:16]:<16} "
                f"{s.engine.get('band') or '-'}{delta}"
                + (f"  [{s.detail}]" if s.status != "ok" and s.detail else "")
            )
        lines.append("-" * 72)
        for key, value in agg.items():
            prior_value = (baseline or {}).get("aggregate", {}).get(key)
            delta = ""
            if isinstance(value, (int, float)) and isinstance(prior_value, (int, float)):
                delta = f"  (was {prior_value})"
            lines.append(f"{key:<24} {value}{delta}")
        return "\n".join(lines)


def _period_end(ts: date, grain: str) -> date:
    if grain == "day":
        return ts
    if grain == "week":
        return ts + timedelta(days=6)
    nxt = date(ts.year + (ts.month // 12), ts.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def run_case(case: Dict[str, Any]) -> CaseScore:
    grain = str(case.get("grain", "week"))
    n, h = int(case["n"]), int(case["horizon"])
    interval = float(case.get("interval", 0.80))
    values = generate(case)
    dates = _dates(grain, n + h)
    history, truth = values[:n], values[n:]
    rows = [{"ts": d.isoformat(), "value": float(v)} for d, v in zip(dates[:n], history)]
    params = {
        "series": {"table": "backtest", "date_column": "ts", "measure_column": "value",
                   "agg": "sum", "grain": grain},
        "window": max(12, n),
        "horizon": h,
        "interval": interval,
        "method": str(case.get("method", "auto")),
    }
    score = CaseScore(case_id=str(case.get("id", "?")), grain=grain, n=n, horizon=h, interval=interval, status="pending")
    outcome = execute_skill(
        "forecast", params, {"columns": ["ts", "value"], "rows": rows},
        override_guards=bool(case.get("override_guards", False)),
        # The last history period is complete: say so, or the value rule may
        # set a quiet final period aside and shift the horizon by one.
        context={"runner": "backtest", "data_end": _period_end(dates[n - 1], grain).isoformat()},
    )
    score.status = outcome.status
    if outcome.status == "guard_failed":
        score.detail = ", ".join(f"{g.name}" for g in outcome.guard_results if not g.passed) or "guard"
        return score
    if outcome.status != "ok" or outcome.envelope is None:
        score.detail = str(outcome.error or "error")[:120]
        return score

    env = outcome.envelope
    # Align by date, not position: if the series preparation set the last
    # history period aside as incomplete, the horizon starts one period early
    # and a positional match would score every point against the wrong truth.
    by_ts = {str(r.get("ts")): r for r in env.rows if r.get("is_forecast")}
    truth_dates = [d.isoformat() for d in dates[n:]]
    fc = [by_ts.get(d) for d in truth_dates]
    if any(r is None for r in fc):
        missing = [d for d, r in zip(truth_dates, fc) if r is None]
        score.status = "error"
        score.detail = f"forecast rows missing for {missing[:3]} (engine horizon starts {min(by_ts) if by_ts else 'n/a'})"
        return score
    point = np.asarray([r.get("forecast") for r in fc], dtype=float)
    lower = np.asarray([np.nan if r.get("lower") is None else r.get("lower") for r in fc], dtype=float)
    upper = np.asarray([np.nan if r.get("upper") is None else r.get("upper") for r in fc], dtype=float)
    if wape_applicable(history):
        score.realized_metric, score.realized_error = "WAPE", wape(truth, point)
    else:
        m = env.facts.get("seasonal_periods") or [1]
        score.realized_metric, score.realized_error = "MASE", mase(truth, point, history, int(max(m)))
    cov, _ = band_coverage(truth, lower, upper)
    score.realized_coverage = cov
    score.engine = result_metrics(env)
    return score


def run_backtest(dataset: Dict[str, Any]) -> BacktestReport:
    report = BacktestReport()
    for case in dataset.get("cases", []):
        try:
            report.scores.append(run_case(case))
        except Exception as exc:  # noqa: BLE001 — one bad case must not abort the run
            logger.exception("backtest case %s crashed", case.get("id"))
            report.scores.append(CaseScore(
                case_id=str(case.get("id", "?")), grain=str(case.get("grain", "?")),
                n=int(case.get("n", 0)), horizon=int(case.get("horizon", 0)),
                interval=float(case.get("interval", 0.8)), status="error", detail=f"crashed: {exc}"[:120],
            ))
    return report


def load_dataset(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("cases"), list):
        raise ValueError(f"{path} has no 'cases' list")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--baseline", type=Path, help="a prior --out file to show deltas against")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the table")
    parser.add_argument("--fail-on-error", action="store_true", help="exit 1 when any case errors")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    report = run_backtest(load_dataset(args.dataset))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    if args.out:
        args.out.write_text(json.dumps(report.to_json(), indent=2, default=str), encoding="utf-8")
    print(json.dumps(report.to_json(), indent=2, default=str) if args.json else report.summary(baseline))
    if args.fail_on_error and report.aggregate()["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
