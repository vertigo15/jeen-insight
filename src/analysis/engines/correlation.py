"""CORRELATION engine — tier A.

Two measures on one calendar. Pearson correlation at every lag in
``[-max_lag, +max_lag]`` (a positive lag means the *other* measure leads),
the best lag with its p-value, and — when there is enough history — Granger
tests in both directions as a weak hint about temporal ordering. Every result
carries the not-causation line; an authoritative-sounding tool without it is
a liability.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    CorrelationParams,
    Egress,
    GuardResult,
    ModelDetails,
    ResultEnvelope,
)
from src.analysis.engines.common import (
    RunContext,
    engine_info,
    make_validation,
    num,
    rows_json_safe,
    series_provenance,
)
from src.analysis.series import SeriesFrame

ENGINE_NAME = "scipy-pearson"
NOT_CAUSATION = "Correlation is not causation: a shared driver (season, promotions, calendar) can move both measures."
MIN_GRANGER_POINTS = 30
OTHER_COLUMN = "value2"


def _scipy_version() -> str:
    try:
        import scipy

        return str(scipy.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _lagged(a: np.ndarray, b: np.ndarray, lag: int):
    """Pairs (a_t, b_{t-lag}) — a positive lag means b leads a."""
    if lag > 0:
        return a[lag:], b[:-lag]
    if lag < 0:
        return a[:lag], b[-lag:]
    return a, b


def _lag1_autocorr(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    if len(x) < 3 or np.std(x) == 0:
        return 0.0
    d = x - x.mean()
    r = float(np.dot(d[1:], d[:-1]) / np.dot(d, d))
    return max(-0.99, min(0.99, r))


def _effective_n(n: int, rho_a: float, rho_b: float) -> int:
    """Bartlett's effective sample size for the correlation of two AR(1)-like series."""
    prod = rho_a * rho_b
    return int(max(3, round(n * (1.0 - prod) / (1.0 + prod))))


def _adjusted_p(r: float, n: int, rho_a: float, rho_b: float, n_tests: int) -> float:
    """Two-sided p for ``r`` at the effective sample size, Bonferroni-scaled by the lags searched."""
    from scipy import stats

    n_eff = _effective_n(n, rho_a, rho_b)
    if abs(r) >= 1.0:
        return 0.0
    t = abs(r) * math.sqrt((n_eff - 2) / (1.0 - r * r))
    p = 2.0 * stats.t.sf(t, df=n_eff - 2)
    return float(min(1.0, p * max(1, n_tests)))


def run(params: CorrelationParams, sf: SeriesFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    from scipy import stats

    ctx = ctx or RunContext()
    guard_results = guard_results or []
    a = sf.y_filled().to_numpy(dtype=float)
    if OTHER_COLUMN not in sf.frame.columns:
        raise ValueError("correlation needs the second measure as 'value2'")
    b = sf.frame[OTHER_COLUMN].astype(float).interpolate(limit_direction="both").fillna(0.0).to_numpy()
    n = len(a)
    max_lag = int(min(params.max_lag, max(0, n // 4)))
    notes: List[str] = []
    if max_lag < params.max_lag:
        notes.append(f"Lags were capped at {max_lag} so every correlation still uses at least three quarters of the history.")

    # Picking the largest |r| over 2·max_lag+1 lags is a multiple-comparison
    # search, and autocorrelated series have fewer independent observations
    # than points. Both inflate a raw Pearson p-value, so significance is
    # judged on a p-value that (a) uses Bartlett's effective sample size
    # n·(1−ρa·ρb)/(1+ρa·ρb) from the lag-1 autocorrelations and (b) is
    # Bonferroni-adjusted across the lags tested. The raw p stays in the rows.
    n_tests = 2 * max_lag + 1
    rho_a, rho_b = _lag1_autocorr(a), _lag1_autocorr(b)
    rows: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    for lag in range(-max_lag, max_lag + 1):
        x, y = _lagged(a, b, lag)
        if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
            r, p, p_adj = float("nan"), float("nan"), float("nan")
        else:
            r, p = stats.pearsonr(x, y)
            p_adj = _adjusted_p(float(r), len(x), rho_a, rho_b, n_tests)
        row = {"lag": lag, "r": float(r), "p_value": float(p), "p_adjusted": float(p_adj), "n": int(len(x))}
        rows.append(row)
        if not np.isnan(r) and (best is None or abs(r) > abs(best["r"])):
            best = row
    columns = ["lag", "r", "p_value", "p_adjusted", "n"]
    zero = next((r for r in rows if r["lag"] == 0), None)
    if max_lag > 0:
        notes.append(f"Significance is adjusted for the {n_tests} lags searched and for autocorrelation "
                     f"(effective n ≈ {_effective_n(n, rho_a, rho_b)} of {n}).")

    # Granger tests (weak temporal-ordering hint), both directions.
    granger: Dict[str, Any] = {}
    if n >= MIN_GRANGER_POINTS and max_lag >= 1:
        try:
            from statsmodels.tsa.stattools import grangercausalitytests

            import contextlib
            import io

            lags = max(1, min(max_lag, 4))
            for name, pair in (("other_leads_measure", np.column_stack([a, b])),
                               ("measure_leads_other", np.column_stack([b, a]))):
                try:
                    # statsmodels prints the tables; keep the sandbox log clean.
                    with contextlib.redirect_stdout(io.StringIO()):
                        res = grangercausalitytests(pair, maxlag=lags)
                    granger[name] = num(min(res[k][0]["ssr_ftest"][1] for k in res), 4)
                except Exception as exc:  # noqa: BLE001 — e.g. perfectly collinear series
                    granger[name] = None
                    notes.append(f"Granger test ({name.replace('_', ' ')}) unavailable: {type(exc).__name__}.")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Granger test unavailable ({type(exc).__name__}).")
    else:
        notes.append(f"Fewer than {MIN_GRANGER_POINTS} points; no lead/lag (Granger) test was run.")

    label_a, label_b = sf.request.measure_label, params.other_label
    if best is None:
        headline = f"{label_a} and {label_b} show no measurable correlation over {n} {sf.period_label(plural=True)}."
    else:
        strength = "strong" if abs(best["r"]) >= 0.7 else "moderate" if abs(best["r"]) >= 0.4 else "weak"
        sign = "positive" if best["r"] > 0 else "negative"
        lag_txt = (f" with {label_b} leading by {best['lag']} {sf.period_label(plural=best['lag'] != 1)}" if best["lag"] > 0
                   else f" with {label_a} leading by {-best['lag']} {sf.period_label(plural=best['lag'] != -1)}" if best["lag"] < 0
                   else " in the same period")
        headline = f"{label_a} and {label_b} have a {strength} {sign} correlation (r = {best['r']:.2f}){lag_txt}."

    validation = make_validation(
        "r", best["r"] if best else None,
        basis=f"Pearson correlation on {n} aligned {sf.period_label(plural=True)}; "
              f"p adjusted for {n_tests} lags and autocorrelation",
        extras={"p_value": best["p_value"] if best else float("nan"),
                "p_adjusted": best["p_adjusted"] if best else float("nan"),
                "r_lag0": zero["r"] if zero else float("nan")},
    )
    p_sig = best["p_adjusted"] if best else float("nan")
    validation.band = ("good" if p_sig < 0.01 else "fair" if p_sig < 0.05 else "poor") if best and not np.isnan(p_sig) else "n/a"
    caveats = [NOT_CAUSATION]
    if best and not (p_sig < 0.05):
        caveats.append("After adjusting for the lags searched and for autocorrelation, the best correlation "
                       "is not statistically significant at the 5% level; treat it as exploratory.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the correlation as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "correlation", "measure": label_a, "other_measure": label_b, "grain": sf.grain, "n_points": n,
        "best": best, "lag0": zero, "granger_p_values": granger, "max_lag": max_lag,
        "span_start": sf.frame.index[0].date().isoformat(), "span_end": sf.frame.index[-1].date().isoformat(),
    }
    details = ModelDetails(
        method_used="Pearson cross-correlation" + (" + Granger" if granger else ""),
        seasonal_periods=list(sf.seasonal_periods),
        candidates=[CandidateScore(name=f"lag {r['lag']}", metric="r", value=num(r["r"]), selected=(best is not None and r["lag"] == best["lag"]))
                    for r in rows],
        params_used={"max_lag": max_lag, "other_measure": label_b},
        notes=notes,
    )
    chart = ChartSpec(chart_type="bar", x_column="lag", y_columns=["r"], x_label="lag", y_label="r")
    return ResultEnvelope(
        skill="correlation", params=params.model_dump(mode="json"), method_used=details.method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=n, columns=["ts", "value", OTHER_COLUMN]),
        engine=engine_info(ENGINE_NAME, _scipy_version(), modules=["correlation", "common"], runner=ctx.runner),
        provenance=series_provenance(sf, ctx), details=details, facts=facts, headline=headline, caveats=caveats,
    )
