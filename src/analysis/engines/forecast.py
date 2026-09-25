"""FORECAST engine — tier A.

Shortlist → rolling-origin cross-validation → pick the winner only if it beats
the seasonal-naive baseline → refit on the full history → forecast with a
prediction interval. Every candidate's error is reported so the Model details
tab can show winner, runner-up and baseline side by side.

Method selection rules (see docs/ml_skills_handoff/README.md §3):
* the shortlist is fixed — statsforecast Naive, SeasonalNaive, Drift (random
  walk with drift), AutoETS, AutoARIMA and, for long seasonal periods, MSTL
  with an ETS/ARIMA trend forecaster (ETS/ARIMA are not fitted directly with
  m > 24); AutoTheta only when asked for;
* CV windows are scored at the requested horizon, never a single holdout;
* the metric is WAPE for a non-negative series with a positive total,
  otherwise MASE (MAE in business units goes to ``extras``);
* in ``auto`` mode a method that cannot beat the baseline is a finding: the
  baseline is returned and the note says so. An explicitly requested method
  is returned regardless, with its score against the baseline in the note;
* an incomplete trailing period (see ``series.detect_partial_tail``) is not
  history: the fit ends at the last complete period and the set-aside period
  becomes the first forecast period, its to-date figure reported beside the
  full-period estimate;
* a history that never went below zero is never forecast below zero — the
  point and the interval are floored at zero and the note says so.
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
from src.analysis.series import SeriesFrame, format_period, partial_tail_sentence

logger = logging.getLogger(__name__)

ENGINE_NAME = "statsforecast"
# Above this seasonal period ETS/ARIMA are fitted through an MSTL decomposition.
DIRECT_SEASONAL_MAX = 24
# Rolling-origin folds. Origins may overlap (step < h) so a history that can
# only hold the horizon out once or twice still yields several folds — the
# interval calibration needs the residuals and the selection needs the votes.
MAX_CV_WINDOWS = 5
UID = "series"
# Top-2 mean ensemble in ``auto``: implemented and measured, not enabled.
# On evals/forecast_backtest_set the ensemble was selected on one seasonal
# weekly series and did worse than the single winner (realized WAPE 0.076 →
# 0.082, 80% band coverage 0.75 → 0.50, because a mean has no native band
# shape and falls back to a constant conformal half-width). Flip to re-test
# after the backtest set grows; the selection rule stays "beat both the best
# single model and the baseline on the same folds".
ENABLE_TOP2_ENSEMBLE = False


def _statsforecast_version() -> str:
    try:
        import statsforecast

        return str(statsforecast.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _build_candidates(method: str, m: Optional[int], *, seasonal_terms: bool = True) -> Tuple[List[Any], str]:
    """Return (models, baseline_alias). Aliases are what statsforecast names the columns.

    The baseline is always first; for an explicit ``method`` the requested
    model is the (single) other entry, so ``models[-1]`` is what the user asked for.

    ``seasonal_terms=False`` keeps the seasonal baseline but fits ETS/ARIMA/
    Theta without a seasonal component: the shape used when the history can
    hold a confirmed season but not two full cycles *and* a held-out horizon.
    """
    from statsforecast.models import AutoARIMA, AutoETS, AutoTheta, MSTL, Naive, RandomWalkWithDrift, SeasonalNaive

    season = int(m) if m else 1
    baseline = SeasonalNaive(season_length=season, alias="SeasonalNaive") if m else Naive(alias="Naive")
    baseline_alias = "SeasonalNaive" if m else "Naive"
    fit_season = season if seasonal_terms else 1

    def ets():
        if seasonal_terms and m and m > DIRECT_SEASONAL_MAX:
            return MSTL(season_length=[season], trend_forecaster=AutoETS(model="ZZN", season_length=1),
                        alias="MSTL+AutoETS")
        return AutoETS(season_length=fit_season, alias="AutoETS")

    def arima():
        if seasonal_terms and m and m > DIRECT_SEASONAL_MAX:
            return MSTL(season_length=[season], trend_forecaster=AutoARIMA(season_length=1), alias="MSTL+AutoARIMA")
        return AutoARIMA(season_length=fit_season, alias="AutoARIMA")

    def theta():
        return AutoTheta(season_length=fit_season if fit_season <= DIRECT_SEASONAL_MAX else 1, alias="AutoTheta")

    def drift():
        # Last value plus the average historical step: the standard trend
        # benchmark, and what a short trending history can actually support.
        return RandomWalkWithDrift(alias="Drift")

    if method == "auto":
        models: List[Any] = [baseline, drift(), ets(), arima()]
        if m:
            models.insert(1, Naive(alias="Naive"))
    elif method == "auto_arima":
        models = [baseline, arima()]
    elif method == "auto_ets":
        models = [baseline, ets()]
    elif method == "theta":
        models = [baseline, theta()]
    elif method == "drift":
        models = [baseline, drift()]
    else:  # seasonal_naive
        models = [baseline]
    return models, baseline_alias


def baseline_alias_for(m: Optional[int]) -> str:
    return "SeasonalNaive" if m else "Naive"


# Share of zero periods above which the history is intermittent demand: the
# smoothing/ARIMA shortlist averages the zeros away, so the Croston family is
# added to ``auto``. Mirrors ``guards.ZERO_SHARE_MAX`` (kept here so the engine
# does not import the guards module).
INTERMITTENT_ZERO_SHARE = 0.50
INTERMITTENT_ALIASES = ("CrostonSBA", "CrostonClassic", "ADIDA", "IMAPA")


def _intermittent_candidates() -> List[Any]:
    """Point-only intermittent-demand models. They refuse a ``level`` argument
    (no native interval), so they are fitted in their own StatsForecast and
    their band comes from the residual calibration."""
    from statsforecast.models import ADIDA, IMAPA, CrostonClassic, CrostonSBA

    return [CrostonSBA(alias="CrostonSBA"), CrostonClassic(alias="CrostonClassic"),
            ADIDA(alias="ADIDA"), IMAPA(alias="IMAPA")]


def _merge_point_columns(cv: pd.DataFrame, other: pd.DataFrame, aliases: List[str]) -> pd.DataFrame:
    """Add ``other``'s point columns to ``cv`` on (ds, cutoff)."""
    keep = [c for c in ("unique_id", "ds", "cutoff") if c in other.columns] + [a for a in aliases if a in other.columns]
    return cv.merge(other[keep], on=[c for c in ("unique_id", "ds", "cutoff") if c in cv.columns], how="left")


def _sf_frame(sf: SeriesFrame) -> pd.DataFrame:
    y = sf.y_filled()
    return pd.DataFrame({"unique_id": UID, "ds": sf.frame.index, "y": y.to_numpy(dtype=float)})


# ── Holiday calendar regressor ────────────────────────────────────────────────

HOLIDAY_COLUMN = "holiday"


def holiday_regressor(country: Optional[str], index: pd.DatetimeIndex, grain: str, freq: str) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """Known-future regressor from a public-holiday calendar.

    Daily grain: 1 on a holiday, else 0. Weekly / monthly: the number of
    holidays inside the period. Returns ``(values, note)``; values are None
    when the calendar is unavailable (library missing, unknown country) — the
    note says so and the forecast runs without it.
    """
    if not country:
        return None, None
    try:
        import holidays as _holidays  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None, f"Holiday calendar {country} requested but the holidays library is not installed; fitted without it."
    if len(index) == 0:
        return None, None
    years = list(range(int(index.min().year), int(index.max().year) + 2))
    try:
        cal = _holidays.country_holidays(country.upper(), years=years)
    except Exception:  # noqa: BLE001
        return None, f"Holiday calendar {country} is not a known country code; fitted without it."
    days = set(cal.keys())
    if grain == "day":
        values = np.asarray([1.0 if ts.date() in days else 0.0 for ts in index], dtype=float)
    else:
        one = pd.tseries.frequencies.to_offset(freq)
        values = np.zeros(len(index), dtype=float)
        for i, ts in enumerate(index):
            start = pd.Timestamp(ts)
            end = (start + one) - pd.Timedelta(days=1)
            values[i] = float(sum(1 for d in days if start.date() <= d <= end.date()))
    if not np.any(values):
        return values, f"Holiday calendar {country}: no holidays fall inside this history or horizon."
    return values, None


def _min_train(m: Optional[int], *, seasonal_terms: bool = True) -> int:
    """Shortest training prefix a fold may start from.

    With seasonal terms the decomposing models need two full cycles; without
    them the seasonal-naive baseline still needs one, and everything else a
    dozen points.
    """
    if seasonal_terms:
        return max(2 * (m or 1), 12)
    return max(int(m or 1), 12)


def _cv_plan(n: int, h: int, m: Optional[int], *, seasonal_terms: bool = True) -> Tuple[int, int]:
    """``(windows, step_size)`` for the rolling-origin CV.

    Non-overlapping folds (step = h) when the history affords ``MAX_CV_WINDOWS``
    of them; otherwise the origins overlap (step = h/2, at least 1) so a short
    history still yields several folds. Zero windows when the horizon cannot
    be held out even once.
    """
    spare = n - _min_train(m, seasonal_terms=seasonal_terms)
    if spare < h:
        return 0, h
    if spare // h >= MAX_CV_WINDOWS:
        return MAX_CV_WINDOWS, h
    step = max(1, h // 2)
    windows = 1 + (spare - h) // step
    return int(max(1, min(MAX_CV_WINDOWS, windows))), step


def _cv_windows(n: int, h: int, m: Optional[int]) -> int:
    return _cv_plan(n, h, m)[0]


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


# ── Interval calibration ──────────────────────────────────────────────────────

# A conformal quantile at level q needs at least q/(1-q) residuals before the
# finite-sample rank ceil((n+1)q) is even defined inside the sample (4 at 80%,
# 19 at 95%); below that the native band is kept.
def _min_residuals(level: int) -> int:
    q = level / 100.0
    return int(math.ceil(q / max(1e-9, 1.0 - q)))


def _conformal_quantile(scores: np.ndarray, level: int) -> Optional[float]:
    """Finite-sample conformal quantile: the ceil((n+1)·q)-th smallest score."""
    s = np.sort(scores[np.isfinite(scores)])
    n = s.size
    if n == 0:
        return None
    q = level / 100.0
    rank = int(math.ceil((n + 1) * q))
    if rank > n:
        return None  # not enough residuals for this level
    return float(s[rank - 1])


def _calibrate_band(cv: Optional[pd.DataFrame], alias: str, level: int, point: np.ndarray,
                    lower: np.ndarray, upper: np.ndarray, *, floor_zero: bool) -> Dict[str, Any]:
    """Calibrate the selected model's band on its own cross-validation residuals.

    Two flavours, both distribution-free:

    * **scaled** — when the model has a native band: each held-out residual is
      divided by that fold's native half-width at the same step, the conformal
      quantile of those ratios is one factor, and the final band is the refit
      model's own half-width times that factor. The horizon shape stays the
      model's; only its scale is corrected.
    * **absolute** — when the model has no native band (point-only methods):
      the conformal quantile of the absolute residuals becomes a constant
      half-width.

    Coverage is reported *honestly*: the factor is fitted on every fold but the
    last and tested on the last, so the figure is out-of-sample. The band that
    ships uses every fold. Falls back to the native band (or none) when there
    are fewer than two folds or too few residuals for the level.
    """
    out: Dict[str, Any] = {
        "lower": lower, "upper": upper, "method": "native", "factor": None,
        "coverage": None, "coverage_n": 0, "n_residuals": 0, "note": None,
    }
    if cv is None or alias not in cv.columns or "cutoff" not in cv.columns:
        return out
    yhat = cv[alias].to_numpy(dtype=float)
    if floor_zero:
        yhat = np.maximum(yhat, 0.0)
    resid = np.abs(cv["y"].to_numpy(dtype=float) - yhat)
    lo_col, hi_col = f"{alias}-lo-{level}", f"{alias}-hi-{level}"
    native = lo_col in cv.columns and hi_col in cv.columns
    final_half = (upper - lower) / 2.0 if native else None
    if native:
        half = (cv[hi_col].to_numpy(dtype=float) - cv[lo_col].to_numpy(dtype=float)) / 2.0
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(half > 0, resid / half, np.nan)
    else:
        scores = resid
    cutoffs = cv["cutoff"].to_numpy()
    distinct = sorted(set(cutoffs))
    usable = np.isfinite(scores)
    out["n_residuals"] = int(usable.sum())
    needed = _min_residuals(level)
    if len(distinct) < 2 or out["n_residuals"] < needed:
        out["note"] = (f"{out['n_residuals']} held-out residuals over {len(distinct)} fold(s); "
                       f"at least {needed} over 2 folds are needed to calibrate a {level}% band")
        return out

    # Honest coverage: fit on the earlier folds, test on the latest one (two
    # when there are at least four, so the figure rests on 2·h points).
    held_out = distinct[-2:] if len(distinct) >= 4 else distinct[-1:]
    is_test = np.isin(cutoffs, np.asarray(held_out))
    train_mask = usable & ~is_test
    test_mask = usable & is_test
    q_cal = _conformal_quantile(scores[train_mask], level) if train_mask.any() else None
    if q_cal is not None and test_mask.any():
        inside = scores[test_mask] <= q_cal
        out["coverage"], out["coverage_n"] = float(inside.mean()), int(test_mask.sum())

    q_all = _conformal_quantile(scores[usable], level)
    if q_all is None:
        out["note"] = f"{out['n_residuals']} residuals cannot support a {level}% conformal quantile"
        return out
    out["factor"] = q_all
    if native:
        half_final = np.where(np.isfinite(final_half), final_half * q_all, np.nan)
        out["lower"] = point - half_final
        out["upper"] = point + half_final
        out["method"] = "conformal_scaled"
    else:
        out["lower"] = point - q_all
        out["upper"] = point + q_all
        out["method"] = "conformal_absolute"
    return out


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

    # ── Holiday calendar (the one regressor whose future is known) ───────
    # The training frame carries the column (statsforecast CV takes future
    # values from the frame itself); the refit gets the horizon's values as
    # ``X_df``. ARIMA uses it; the other candidates ignore the column.
    holiday_country = getattr(params, "holidays", None)
    x_future: Optional[pd.DataFrame] = None
    holidays_used = False
    if holiday_country:
        horizon_index = pd.date_range(start=sf.frame.index[-1], periods=h + 1, freq=sf.freq)[1:]
        full_index = sf.frame.index.append(horizon_index)
        values, note = holiday_regressor(holiday_country, pd.DatetimeIndex(full_index), sf.grain, sf.freq)
        if note:
            notes.append(note)
        if values is not None and np.any(values):
            df[HOLIDAY_COLUMN] = values[:n]
            x_future = pd.DataFrame({"unique_id": UID, "ds": horizon_index, HOLIDAY_COLUMN: values[n:]})
            holidays_used = True
            notes.append(f"Holiday calendar {holiday_country} was fitted as a regressor (used by the ARIMA candidates).")
    # A confirmed season whose two cycles leave no room to hold the horizon
    # out: validate the shortlist without seasonal terms (the seasonal-naive
    # baseline stays) rather than return an unvalidated baseline.
    seasonal_terms = True
    windows, step = _cv_plan(n, h, m, seasonal_terms=True)
    if not windows and m:
        reduced_windows, reduced_step = _cv_plan(n, h, m, seasonal_terms=False)
        if reduced_windows:
            seasonal_terms = False
            windows, step = reduced_windows, reduced_step
            notes.append(
                f"{n} {sf.period_label(plural=True)} cannot hold out {h} after two full {m}-period cycles, "
                f"so ETS/ARIMA were validated without a seasonal term ({baseline_alias_for(m)} keeps the season)."
            )
    models, baseline_alias = _build_candidates(params.method, m, seasonal_terms=seasonal_terms)
    aliases = [getattr(mod, "alias", type(mod).__name__) for mod in models]
    # Intermittent demand: many zero periods. The Croston family joins the
    # ``auto`` shortlist; it competes on the same folds and the same metric.
    intermittent_models: List[Any] = []
    zero_share = float(sf.zero_share) if hasattr(sf, "zero_share") else 0.0
    if params.method == "auto" and zero_share > INTERMITTENT_ZERO_SHARE:
        intermittent_models = _intermittent_candidates()
        notes.append(
            f"{zero_share:.0%} of {sf.period_label(plural=True)} are zero (intermittent demand); "
            f"{', '.join(INTERMITTENT_ALIASES)} were added to the shortlist."
        )
    intermittent_aliases = [getattr(mod, "alias", type(mod).__name__) for mod in intermittent_models]

    # ── Rolling-origin cross-validation at the requested horizon ────────
    candidates: List[CandidateScore] = []
    cv_errors: Dict[str, Optional[float]] = {}
    cov_by_model: Dict[str, Tuple[Optional[float], int]] = {}
    cv = None
    basis = "no cross-validation possible (history too short for the horizon)"
    if windows:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sfc = StatsForecast(models=models, freq=sf.freq, n_jobs=1)
                cv = sfc.cross_validation(h=h, df=df, n_windows=windows, step_size=step, level=[level])
                if intermittent_models:
                    try:
                        sfi = StatsForecast(models=intermittent_models, freq=sf.freq, n_jobs=1)
                        cv_i = sfi.cross_validation(h=h, df=df, n_windows=windows, step_size=step)
                        cv = _merge_point_columns(cv, cv_i, intermittent_aliases)
                        aliases = aliases + intermittent_aliases
                        models = models + intermittent_models
                    except Exception as exc:  # noqa: BLE001 — the ordinary shortlist still stands
                        logger.warning("forecast: intermittent candidates failed in CV: %s", exc)
                        notes.append(f"The intermittent-demand models could not be validated ({type(exc).__name__}) and were dropped.")
                        intermittent_models, intermittent_aliases = [], []
            for alias in aliases:
                if alias not in cv.columns:
                    cv_errors[alias] = None
                    continue
                cv_errors[alias] = _cv_score(cv, alias, df, y, season=m or 1, use_wape=use_wape)
                lo, hi = f"{alias}-lo-{level}", f"{alias}-hi-{level}"
                if lo in cv.columns and hi in cv.columns:
                    cov_by_model[alias] = coverage(cv["y"], cv[lo], cv[hi])
            overlap = f", origins {step} apart" if step != h else ""
            basis = f"rolling-origin CV, {windows} window{'s' if windows != 1 else ''} of h={h} {sf.period_label(plural=True)}{overlap}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("forecast: cross-validation failed: %s", exc)
            notes.append(f"Cross-validation failed ({type(exc).__name__}); falling back to the baseline.")
            cv_errors = {alias: None for alias in aliases}
            cv = None
    else:
        notes.append(
            f"{n} {sf.period_label(plural=True)} of history cannot hold out {h}; no cross-validation was run."
        )

    # ── Selection: beat the baseline or return it; an explicit choice stands ──
    def _fmt_err(value: Optional[float]) -> str:
        if value is None:
            return "n/a"
        return fmt_pct(value) if use_wape else f"{value:.2f}"

    baseline_err = cv_errors.get(baseline_alias)
    selected = baseline_alias
    best_err = baseline_err
    ensemble_members: List[str] = []
    requested = aliases[-1] if params.method not in ("auto", "seasonal_naive") else None
    scored = [(a, e) for a, e in cv_errors.items() if e is not None and a != baseline_alias]
    scored.sort(key=lambda t: t[1])
    if requested is not None:
        # The user named the model. It is what they get; the note says how it
        # compared to the baseline (or that it could not be compared).
        selected, best_err = requested, cv_errors.get(requested)
        if best_err is None:
            notes.append(f"{requested} was requested; it could not be validated against {baseline_alias} on this history.")
        elif baseline_err is not None and best_err >= baseline_err:
            how = "scored better" if best_err > baseline_err else "scored the same"
            notes.append(
                f"{requested} was requested and is shown although {baseline_alias} {how} in cross-validation "
                f"({metric_name} {_fmt_err(best_err)} vs {_fmt_err(baseline_err)})."
            )
    elif scored:
        top_alias, top_err = scored[0]
        # The mean of the two best candidates is scored on the same folds and
        # chosen only when it beats both the best single model and the
        # baseline — never because two models happen to be close.
        if ENABLE_TOP2_ENSEMBLE and params.method == "auto" and len(scored) >= 2 and cv is not None:
            a1, a2 = scored[0][0], scored[1][0]
            if a1 in cv.columns and a2 in cv.columns:
                ens_alias = f"Mean({a1}+{a2})"
                cv[ens_alias] = (cv[a1].to_numpy(dtype=float) + cv[a2].to_numpy(dtype=float)) / 2.0
                ens_err = _cv_score(cv, ens_alias, df, y, season=m or 1, use_wape=use_wape)
                if (ens_err is not None and ens_err < top_err
                        and (baseline_err is None or ens_err < baseline_err)):
                    ensemble_members = [a1, a2]
                    cv_errors[ens_alias] = ens_err
                    aliases.append(ens_alias)
                    top_alias, top_err = ens_alias, ens_err
                    notes.append(
                        f"The mean of {a1} and {a2} scored {metric_name} {_fmt_err(ens_err)} in cross-validation, "
                        f"below either alone ({_fmt_err(scored[0][1])}); it is the forecast shown."
                    )
        if baseline_err is None or top_err < baseline_err:
            selected, best_err = top_alias, top_err
        else:
            notes.append(
                f"No candidate beat {baseline_alias} ({metric_name} {_fmt_err(baseline_err)}); "
                f"returning the baseline rather than a confident wrong number."
            )
    elif params.method == "auto" and not windows:
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
    single_aliases = [getattr(mod, "alias", type(mod).__name__) for mod in models]
    selected_is_ensemble = bool(ensemble_members) and selected not in single_aliases
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        exog = {"X_df": x_future} if x_future is not None else {}
        if selected_is_ensemble:
            # Refit both members; the shown forecast is their mean. The
            # ensemble has no native band — the calibration below supplies one.
            member_models = [models[aliases.index(a)] for a in ensemble_members]
            sff = StatsForecast(models=member_models, freq=sf.freq, n_jobs=1)
            fc = sff.forecast(h=h, df=df, **exog)
            fc[selected] = sum(fc[a].to_numpy(dtype=float) for a in ensemble_members) / float(len(ensemble_members))
        else:
            winner_model = models[aliases.index(selected)]
            point_only = selected in intermittent_aliases
            sff = StatsForecast(models=[winner_model], freq=sf.freq, n_jobs=1)
            # Point-only models raise on ``level``; their band is calibrated below.
            fc = sff.forecast(h=h, df=df, **exog) if point_only else sff.forecast(h=h, df=df, level=[level], **exog)
    point = fc[selected].to_numpy(dtype=float)
    lo_col, hi_col = f"{selected}-lo-{level}", f"{selected}-hi-{level}"
    lower = fc[lo_col].to_numpy(dtype=float) if lo_col in fc.columns else np.full(h, np.nan)
    upper = fc[hi_col].to_numpy(dtype=float) if hi_col in fc.columns else np.full(h, np.nan)
    fc_index = pd.DatetimeIndex(fc["ds"])

    # ── Calibrate the band on the winner's own held-out residuals ─────────
    # The native (Gaussian) band is a model assumption; the residuals are what
    # actually happened at this horizon. Coverage below is out-of-sample.
    calib = _calibrate_band(cv, selected, level, point, lower, upper, floor_zero=use_wape)
    interval_method = calib["method"]
    if interval_method != "native":
        lower, upper = calib["lower"], calib["upper"]
        cov, cov_n = calib["coverage"], int(calib["coverage_n"] or 0)
        notes.append(
            f"The {level}% band was calibrated on {calib['n_residuals']} held-out residuals "
            f"(factor {calib['factor']:.2f} on the model's own width)."
            if interval_method == "conformal_scaled" else
            f"The {level}% band is a conformal half-width from {calib['n_residuals']} held-out residuals "
            "(the model has no native interval)."
        )
    else:
        cov, cov_n = cov_by_model.get(selected, (None, 0))
        if calib["note"] and windows:
            notes.append(f"Band not calibrated: {calib['note']}; the model's native interval is shown.")

    # A measure that never went below zero (sales, counts) cannot be forecast
    # below zero either; the Gaussian interval does not know that. Floor the
    # point and the band and say so, rather than chart a negative sales month.
    floored = False
    if use_wape:
        with np.errstate(invalid="ignore"):
            below = (point < 0) | (lower < 0) | (upper < 0)
        floored = bool(np.any(below))
        if floored:
            point = np.maximum(point, 0.0)
            lower = np.where(np.isfinite(lower), np.maximum(lower, 0.0), lower)
            upper = np.where(np.isfinite(upper), np.maximum(upper, 0.0), upper)
            notes.append(
                f"{sf.request.measure_label} never went below zero in the history, so the forecast and its "
                f"{level}% interval were floored at zero."
            )

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
        "cv_step": step,
        "seasonal_terms_fitted": seasonal_terms and bool(m),
        "coverage": num(cov),
        "coverage_n": cov_n,
        "interval_method": interval_method,
        "conformal_factor": num(calib["factor"]),
        "calibration_residuals": int(calib["n_residuals"]),
        # Frozen MASE denominator so a later realized-accuracy check can score
        # this forecast on the scale it was validated against.
        "mase_scale": num(naive_scale(y, m or 1)),
        "floored_at_zero": floored,
        "holidays": holiday_country,
        "holidays_used": holidays_used,
        # How the method was chosen, as data (the notes say it in prose): the
        # advisor and the log line read this, never the sentence.
        "selection": {
            "mode": "requested" if requested is not None else "auto",
            "baseline_won": selected == baseline_alias,
            "validated": windows > 0 and any(e is not None for e in cv_errors.values()),
            "best_candidate": scored[0][0] if scored else None,
            "best_candidate_error": num(scored[0][1]) if scored else None,
            "baseline_error": num(baseline_err),
            "ensemble_members": list(ensemble_members) if selected_is_ensemble else [],
        },
    }
    tail = sf.partial_tail
    if tail is not None:
        # The set-aside period is the first forecast period: the model's estimate
        # for the whole of it sits next to the to-date figure it replaced.
        tail_is_first = fc_index[0] == tail.ts
        facts["partial_tail"] = {
            "ts": tail.ts.date().isoformat(),
            "period": format_period(tail.ts, sf.grain),
            "value": num(tail.value),
            "reason": tail.reason,
            "basis": tail.basis,
            "forecast": num(point[0]) if tail_is_first else None,
        }

    change = facts["pct_change_vs_trailing"]
    direction = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"
    unit_all = sf.period_label(plural=True)
    headline = (
        f"{sf.request.measure_label} is projected at {fmt_value(end_value)} for {facts['forecast_end']['period']}"
        + (f", {direction} {fmt_pct(abs(change))} against the last {h} {unit_all}." if change is not None and direction != "flat" else ".")
    )

    caveats: List[str] = []
    if tail is not None:
        sentence = partial_tail_sentence(sf)
        if facts["partial_tail"]["forecast"] is not None:
            sentence += (f" The forecast for the whole of {facts['partial_tail']['period']} is "
                         f"{fmt_value(point[0])}.")
        caveats.append(sentence)
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
            "cv_windows": windows, "cv_step": step, "seasonal_terms": seasonal_terms and bool(m),
            "holidays": holiday_country if holidays_used else None,
            "seasonal_strength": {str(k): v for k, v in sf.seasonal_strength.items()},
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
