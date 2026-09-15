"""FORECAST engine — tier A.

Shortlist → rolling-origin cross-validation → pick the winner only if it beats
the seasonal-naive baseline → refit on the full history → forecast with a
prediction interval. Every candidate's error is reported so the Model details
tab can show winner, runner-up and baseline side by side.

Method selection rules (see docs/ml_skills_handoff/README.md §3):
* the shortlist is fixed — statsforecast Naive, SeasonalNaive, AutoETS,
  AutoARIMA and, for long seasonal periods, MSTL with an ETS/ARIMA trend
  forecaster (ETS/ARIMA are not fitted directly with m > 24);
* CV windows are scored at the requested horizon, never a single holdout;
* the metric is WAPE for a non-negative series with a positive total,
  otherwise MASE (MAE in business units goes to ``extras``);
* a method that cannot beat SeasonalNaive is a finding: the baseline is
  returned and the note says so.
"""

from __future__ import annotations

import logging
import math
import warnings
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    CandidateScore,
    ChartSeriesSpec,
    ChartSpec,
    Egress,
    ForecastParams,
    GuardResult,
    ModelDetails,
    Provenance,
    ResultEnvelope,
)
from src.analysis.engines.common import (
    RunContext,
    coverage,
    engine_info,
    fmt_pct,
    fmt_value,
    make_validation,
    naive_scale,
    now_iso,
    num,
    pct_change,
    rows_json_safe,
    score,
    wape_applicable,
)
from src.analysis.series import SeriesFrame, format_period

logger = logging.getLogger(__name__)

ENGINE_NAME = "statsforecast"
# Above this seasonal period ETS/ARIMA are fitted through an MSTL decomposition.
DIRECT_SEASONAL_MAX = 24
MAX_CV_WINDOWS = 3
UID = "series"


def _statsforecast_version() -> str:
    try:
        import statsforecast

        return str(statsforecast.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _build_candidates(method: str, m: Optional[int]) -> Tuple[List[Any], str]:
    """Return (models, baseline_alias). Aliases are what statsforecast names the columns."""
    from statsforecast.models import AutoARIMA, AutoETS, AutoTheta, MSTL, Naive, SeasonalNaive

    season = int(m) if m else 1
    baseline = SeasonalNaive(season_length=season, alias="SeasonalNaive") if m else Naive(alias="Naive")
    baseline_alias = "SeasonalNaive" if m else "Naive"

    def ets():
        if m and m > DIRECT_SEASONAL_MAX:
            return MSTL(season_length=[season], trend_forecaster=AutoETS(model="ZZN", season_length=1),
                        alias="MSTL+AutoETS")
        return AutoETS(season_length=season, alias="AutoETS")

    def arima():
        if m and m > DIRECT_SEASONAL_MAX:
            return MSTL(season_length=[season], trend_forecaster=AutoARIMA(season_length=1), alias="MSTL+AutoARIMA")
        return AutoARIMA(season_length=season, alias="AutoARIMA")

    def theta():
        return AutoTheta(season_length=season if season <= DIRECT_SEASONAL_MAX else 1, alias="AutoTheta")

    if method == "auto":
        models: List[Any] = [baseline, ets(), arima()]
        if m:
            models.insert(1, Naive(alias="Naive"))
    elif method == "auto_arima":
        models = [baseline, arima()]
    elif method == "auto_ets":
        models = [baseline, ets()]
    elif method == "theta":
        models = [baseline, theta()]
    else:  # seasonal_naive
        models = [baseline]
    return models, baseline_alias


def _sf_frame(sf: SeriesFrame) -> pd.DataFrame:
    y = sf.y_filled()
    return pd.DataFrame({"unique_id": UID, "ds": sf.frame.index, "y": y.to_numpy(dtype=float)})


def _cv_windows(n: int, h: int, m: Optional[int]) -> int:
    min_train = max(2 * (m or 1), 12)
    spare = n - min_train
    if spare < h:
        return 0
    return int(max(1, min(MAX_CV_WINDOWS, spare // h)))


def _level(interval: float) -> int:
    return int(round(interval * 100))


def _cv_score(cv, alias: str, df, y: np.ndarray, *, season: int, use_wape: bool) -> Optional[float]:
    """Score one model over the CV folds.

    WAPE pools every fold. MASE is scaled per fold by the naive error of that
    fold's *training prefix* (history up to its cutoff) and averaged, so the
    held-out periods never leak into their own denominator.
    """
    if use_wape or "cutoff" not in cv.columns:
        _, value = score(cv["y"], cv[alias], history=y, season=season, use_wape=use_wape)
        return value
    ds = df["ds"].to_numpy()
    fold_scores: List[float] = []
    for cutoff, fold in cv.groupby("cutoff", sort=True):
        train = y[ds <= np.datetime64(cutoff)]
        _, value = score(fold["y"], fold[alias], history=train, season=season, use_wape=False)
        if value is not None and not math.isnan(value):
            fold_scores.append(value)
    return float(np.mean(fold_scores)) if fold_scores else None


def run(params: ForecastParams, sf: SeriesFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    from statsforecast import StatsForecast

    ctx = ctx or RunContext()
    guard_results = guard_results or []
    n = sf.n
    h = int(params.horizon)
    m = sf.longest_period
    level = _level(params.interval)
    notes: List[str] = []

    df = _sf_frame(sf)
    y = df["y"].to_numpy(dtype=float)
    use_wape = wape_applicable(y)
    metric_name = "WAPE" if use_wape else "MASE"
    models, baseline_alias = _build_candidates(params.method, m)
    aliases = [getattr(mod, "alias", type(mod).__name__) for mod in models]

    # ── Rolling-origin cross-validation at the requested horizon ────────
    candidates: List[CandidateScore] = []
    cv_errors: Dict[str, Optional[float]] = {}
    cov_by_model: Dict[str, Tuple[Optional[float], int]] = {}
    windows = _cv_windows(n, h, m)
    basis = "no cross-validation possible (history too short for the horizon)"
    if windows:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sfc = StatsForecast(models=models, freq=sf.freq, n_jobs=1)
                cv = sfc.cross_validation(h=h, df=df, n_windows=windows, step_size=h, level=[level])
            for alias in aliases:
                if alias not in cv.columns:
                    cv_errors[alias] = None
                    continue
                cv_errors[alias] = _cv_score(cv, alias, df, y, season=m or 1, use_wape=use_wape)
                lo, hi = f"{alias}-lo-{level}", f"{alias}-hi-{level}"
                if lo in cv.columns and hi in cv.columns:
                    cov_by_model[alias] = coverage(cv["y"], cv[lo], cv[hi])
            basis = f"rolling-origin CV, {windows} window{'s' if windows != 1 else ''} of h={h} {sf.period_label(plural=True)}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("forecast: cross-validation failed: %s", exc)
            notes.append(f"Cross-validation failed ({type(exc).__name__}); falling back to the baseline.")
            cv_errors = {alias: None for alias in aliases}
    else:
        notes.append(
            f"{n} {sf.period_label(plural=True)} of history cannot hold out {h}; no cross-validation was run."
        )

    # ── Selection: beat the baseline or return it ────────────────────────
    baseline_err = cv_errors.get(baseline_alias)
    selected = baseline_alias
    best_err = baseline_err
    scored = [(a, e) for a, e in cv_errors.items() if e is not None and a != baseline_alias]
    scored.sort(key=lambda t: t[1])
    if scored:
        top_alias, top_err = scored[0]
        if baseline_err is None or top_err < baseline_err:
            selected, best_err = top_alias, top_err
        else:
            notes.append(
                f"No candidate beat {baseline_alias} ({metric_name} {fmt_pct(baseline_err) if use_wape else f'{baseline_err:.2f}'}); "
                f"returning the baseline rather than a confident wrong number."
            )
    elif params.method != "seasonal_naive" and not windows:
        notes.append(f"Returning {baseline_alias} because no candidate could be validated.")

    for alias in aliases:
        candidates.append(CandidateScore(
            name=alias,
            metric=metric_name,
            value=num(cv_errors.get(alias)),
            selected=(alias == selected),
            is_baseline=(alias == baseline_alias),
        ))

    # ── Refit the winner on the full history and forecast ────────────────
    winner_model = models[aliases.index(selected)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sff = StatsForecast(models=[winner_model], freq=sf.freq, n_jobs=1)
        fc = sff.forecast(h=h, df=df, level=[level])
    point = fc[selected].to_numpy(dtype=float)
    lo_col, hi_col = f"{selected}-lo-{level}", f"{selected}-hi-{level}"
    lower = fc[lo_col].to_numpy(dtype=float) if lo_col in fc.columns else np.full(h, np.nan)
    upper = fc[hi_col].to_numpy(dtype=float) if hi_col in fc.columns else np.full(h, np.nan)
    fc_index = pd.DatetimeIndex(fc["ds"])

    # ── Rows: history then forecast, one ordinary table ──────────────────
    rows: List[Dict[str, Any]] = []
    observed = sf.frame["observed"].to_numpy(dtype=bool)
    for i, ts in enumerate(sf.frame.index):
        rows.append({
            "ts": ts.date().isoformat(), "actual": float(y[i]), "forecast": None,
            "lower": None, "upper": None, "observed": bool(observed[i]), "is_forecast": False,
        })
    for i, ts in enumerate(fc_index):
        rows.append({
            "ts": ts.date().isoformat(), "actual": None, "forecast": float(point[i]),
            "lower": float(lower[i]) if np.isfinite(lower[i]) else None,
            "upper": float(upper[i]) if np.isfinite(upper[i]) else None,
            "observed": False, "is_forecast": True,
        })
    columns = ["ts", "actual", "forecast", "lower", "upper", "observed", "is_forecast"]

    # ── Facts ────────────────────────────────────────────────────────────
    last_actual = float(y[-1])
    horizon_total = float(np.nansum(point))
    trailing_total = float(np.nansum(y[-h:])) if n >= h else float(np.nansum(y))
    end_value = float(point[-1])
    cov, cov_n = cov_by_model.get(selected, (None, 0))
    facts: Dict[str, Any] = {
        "skill": "forecast",
        "measure": sf.request.measure_label,
        "grain": sf.grain,
        "horizon": h,
        "interval": params.interval,
        "method": selected,
        "baseline": baseline_alias,
        "n_points": n,
        "last_actual": {"ts": sf.frame.index[-1].date().isoformat(), "period": format_period(sf.frame.index[-1], sf.grain), "value": num(last_actual)},
        "forecast_end": {
            "ts": fc_index[-1].date().isoformat(), "period": format_period(fc_index[-1], sf.grain),
            "value": num(end_value), "lower": num(lower[-1]), "upper": num(upper[-1]),
        },
        "forecast_first": {"ts": fc_index[0].date().isoformat(), "value": num(point[0]), "lower": num(lower[0]), "upper": num(upper[0])},
        "horizon_total": num(horizon_total),
        "trailing_total": num(trailing_total),
        "pct_change_vs_trailing": pct_change(horizon_total, trailing_total),
        "pct_change_end_vs_last": pct_change(end_value, last_actual),
        "seasonal_periods": list(sf.seasonal_periods),
        "candidates": [c.model_dump() for c in candidates],
        "cv_metric": metric_name,
        "cv_windows": windows,
        "coverage": num(cov),
        "coverage_n": cov_n,
    }

    change = facts["pct_change_vs_trailing"]
    direction = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"
    unit_all = sf.period_label(plural=True)
    headline = (
        f"{sf.request.measure_label} is projected at {fmt_value(end_value)} for {facts['forecast_end']['period']}"
        + (f", {direction} {fmt_pct(abs(change))} against the last {h} {unit_all}." if change is not None and direction != "flat" else ".")
    )

    caveats: List[str] = []
    stable = min(3, h)
    if h > stable:
        caveats.append(f"Treat anything past {stable} {sf.period_label(plural=stable != 1)} as a direction, not a number.")
    if np.isfinite(lower[-1]) and np.isfinite(upper[-1]):
        caveats.append(
            f"By {facts['forecast_end']['period']} the {level}% range spans {fmt_value(lower[-1])} to {fmt_value(upper[-1])}."
        )
    if not sf.seasonal_periods:
        caveats.append("No seasonal pattern could be confirmed on this history, so no seasonal term was fitted.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; the interval is wider than it looks.")
    caveats.extend(notes)

    extras: Dict[str, float] = {}
    if not use_wape:
        from src.analysis.engines.common import mae as _mae
        scale = naive_scale(y, m or 1)
        if best_err is not None and scale:
            extras["MAE"] = best_err * scale
    validation = make_validation(metric_name, best_err, basis=basis, cov=cov, cov_n=cov_n or None, extras=extras)

    details = ModelDetails(
        method_used=selected,
        seasonal_periods=list(sf.seasonal_periods),
        candidates=candidates,
        params_used={
            "horizon": h, "interval": params.interval, "method": params.method,
            "cv_windows": windows, "seasonal_strength": {str(k): v for k, v in sf.seasonal_strength.items()},
        },
        notes=notes,
    )

    chart = ChartSpec(
        chart_type="band",
        x_column="ts",
        series=[
            ChartSeriesSpec(role="actual", label="Actual", column="actual"),
            ChartSeriesSpec(role="forecast", label="Forecast", column="forecast"),
            ChartSeriesSpec(role="interval", label=f"{level}% interval", lower_column="lower", upper_column="upper"),
        ],
        forecast_start=fc_index[0].date().isoformat(),
        y_label=sf.request.measure_label,
    )

    return ResultEnvelope(
        skill="forecast",
        params=params.model_dump(mode="json"),
        method_used=selected,
        columns=columns,
        rows=rows_json_safe(rows),
        chart_spec=chart,
        validation=validation,
        guard_results=guard_results,
        low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=n, columns=["ts", "value"]),
        engine=engine_info(ENGINE_NAME, _statsforecast_version(), modules=["forecast", "common"], runner=ctx.runner),
        provenance=Provenance(
            query_ts=ctx.query_ts or now_iso(),
            sql=ctx.sql,
            filters_summary=ctx.filters_summary,
            missing_policy=sf.missing_policy,
            grain=sf.grain,
            span_start=sf.frame.index[0].date().isoformat(),
            span_end=sf.frame.index[-1].date().isoformat(),
            periods_observed=sf.periods_observed,
            periods_filled=sf.periods_filled,
        ),
        details=details,
        facts=facts,
        headline=headline,
        caveats=caveats,
    )
