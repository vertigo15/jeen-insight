"""ANOMALY_DETECTION engine — tier A.

Every method builds the same kind of answer: an *expected* line, then a band
around it, and points outside the band are flagged. They differ only in how the
expected line is drawn and how the band is scaled.

``auto``
    Decompose the series with MSTL on the confirmed seasonal periods (or a
    robust LOWESS trend when none is confirmed); the expected value is trend +
    seasonal.

``seasonal``
    Force the MSTL seasonal decomposition. When no period is confirmed it uses
    the grain's candidate period anyway (an explicit choice is honoured), and
    only falls back to a trend when the grain/length leaves no testable period
    or the decomposition fails — the note says which.

``trend``
    Force the robust LOWESS trend, with no seasonal term.

``auto``/``seasonal``/``trend`` scale the residuals with a robust MAD estimate,
so the anomalies being looked for do not inflate the band that should catch
them. The band is ``expected ± k · scale`` with ``k`` the two-sided normal
quantile for ``sensitivity``: on well-behaved history about ``1 − sensitivity``
of points fall outside, which is exactly what the parameter promises.

``sigma3``
    ``expected ± 3σ`` on the same (auto) residuals, using the ordinary standard
    deviation. Kept because people ask for it by name; labelled non-robust in
    the notes, and it ignores ``sensitivity``.

Whichever method runs, the result states whether a season was *detected* (a
confirmed period, independent of the method) and whether the expected line
actually *modelled* it, so the reader is never left guessing what ``auto`` did.
The engine never sees the database or the user's text. It reports the empirical
band coverage so the user can compare it with the sensitivity they set.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as _stats

from src.analysis.contracts import (
    AnomalyParams,
    CandidateScore,
    ChartSeriesSpec,
    ChartSpec,
    Egress,
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
    now_iso,
    num,
    rows_json_safe,
    wape,
    wape_applicable,
)
from src.analysis.series import SeriesFrame, format_period

ENGINE_NAME = "statsmodels-mstl"
MAD_TO_SIGMA = 1.4826
# Floor on the robust scale as a share of the series' typical magnitude, so a
# nearly-constant history does not flag every 0.1% wobble.
SCALE_FLOOR_SHARE = 0.005


def _statsmodels_version() -> str:
    try:
        import statsmodels

        return str(statsmodels.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _expected_line(y: np.ndarray, periods: List[int]) -> Tuple[np.ndarray, str, List[str], float]:
    """Return (expected, method_label, notes, scale_correction).

    With few cycles a loess seasonal smoother interpolates noise and swallows
    the very anomalies it should expose, so the seasonal component is forced
    to be periodic (one estimate per phase, averaged across cycles) and the
    residual scale is inflated by ``1/sqrt(1 - 1/cycles)`` to undo the
    variance that averaging absorbed. Robust STL is deliberately *off* here:
    down-weighting makes the short-history interpolation worse.
    """
    notes: List[str] = []
    n = len(y)
    if periods:
        try:
            from statsmodels.tsa.seasonal import MSTL

            cycles = n / float(min(periods))
            windows = [max(7, (2 * (n // p) + 1) | 1) for p in periods]
            res = MSTL(y, periods=periods, windows=windows,
                       stl_kwargs={"seasonal_deg": 0, "robust": False}).fit()
            seasonal = np.asarray(res.seasonal, dtype=float)
            if seasonal.ndim == 2:
                seasonal = seasonal.sum(axis=1)
            expected = np.asarray(res.trend, dtype=float) + seasonal
            label = f"MSTL, m={','.join(str(p) for p in periods)}"
            longest_cycles = n / float(max(periods))
            correction = 1.0 / math.sqrt(max(1e-9, 1.0 - 1.0 / longest_cycles)) if longest_cycles > 1 else 1.0
            if longest_cycles < 3:
                notes.append(
                    f"Only {longest_cycles:.1f} cycles of the {max(periods)}-period season are available; "
                    "the seasonal pattern is averaged across them."
                )
            return expected, label, notes, correction
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Seasonal decomposition failed ({type(exc).__name__}); using a smoothed trend.")
    # Non-seasonal: robust LOWESS trend. frac shrinks with n so short series
    # keep local shape; the floor keeps it from tracing every point.
    from statsmodels.nonparametric.smoothers_lowess import lowess

    frac = float(min(0.6, max(0.25, 8.0 / max(n, 1))))
    x = np.arange(n, dtype=float)
    smoothed = lowess(y, x, frac=frac, it=2, return_sorted=False)
    notes.append("The expected line is a smoothed LOWESS trend (no seasonal term).")
    # A loess trend over ~frac·n points absorbs roughly 1/(frac·n) of the noise per point.
    correction = 1.0 / math.sqrt(max(1e-9, 1.0 - 1.0 / max(2.0, frac * n)))
    return np.asarray(smoothed, dtype=float), f"LOWESS trend (frac={frac:.2f})", notes, correction


def _robust_scale(resid: np.ndarray, y: np.ndarray, correction: float = 1.0) -> float:
    mad = float(np.median(np.abs(resid - np.median(resid))))
    scale = mad * MAD_TO_SIGMA * float(correction)
    floor = SCALE_FLOOR_SHARE * float(np.median(np.abs(y))) if y.size else 0.0
    if scale <= 0 or not np.isfinite(scale):
        scale = float(np.std(resid)) or floor
    return float(max(scale, floor, 1e-12))


def run(params: AnomalyParams, sf: SeriesFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    ctx = ctx or RunContext()
    guard_results = guard_results or []
    y = sf.y_filled().to_numpy(dtype=float)
    idx = sf.frame.index
    observed = sf.frame["observed"].to_numpy(dtype=bool)
    n = len(y)

    confirmed_periods = list(sf.seasonal_periods)
    if params.method == "trend":
        periods_for_line: List[int] = []
    elif params.method == "seasonal":
        # An explicit seasonal choice is honoured even when the strength test
        # did not confirm a period: fall back to the grain's candidate so MSTL
        # has something to decompose; _expected_line drops to a trend on its own
        # only when that too is impossible.
        periods_for_line = confirmed_periods or list(sf.candidate_periods)
    else:  # auto, sigma3
        periods_for_line = confirmed_periods

    expected, method_label, notes, correction = _expected_line(y, periods_for_line)
    resid = y - expected
    modelled_seasonal = method_label.startswith("MSTL")

    if params.method == "seasonal" and not modelled_seasonal:
        if not sf.candidate_periods:
            notes.append("A seasonal model was requested, but this grain and history length leave no testable "
                         "seasonal period; a smoothed trend was used instead.")
        else:
            notes.append("A seasonal model was requested, but the seasonal decomposition could not run; "
                         "a smoothed trend was used instead.")

    if params.method == "sigma3":
        sigma = float(np.std(resid)) or 1e-12
        k = 3.0
        scale = sigma
        method_used = f"3-sigma on residuals ({method_label})"
        notes.append("3-sigma uses the ordinary standard deviation, which the outliers themselves inflate; it ignores sensitivity.")
        implied = 2 * (1 - _stats.norm.cdf(3.0))
        expected_outside = implied
    else:
        scale = _robust_scale(resid, y, correction)
        k = float(_stats.norm.ppf(0.5 + params.sensitivity / 2.0))
        method_used = f"{method_label}, robust residual band"
        expected_outside = 1.0 - params.sensitivity

    lower = expected - k * scale
    upper = expected + k * scale
    z = resid / scale
    is_anomaly = (y < lower) | (y > upper)

    cov, cov_n = coverage(y, lower, upper)
    fit_metric_ok = wape_applicable(y)
    fit_wape = wape(y, expected) if fit_metric_ok else None

    # ── Rows (an ordinary result table) ──────────────────────────────────
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        rows.append({
            "ts": idx[i].date().isoformat(),
            "actual": float(y[i]),
            "expected": float(expected[i]),
            "lower": float(lower[i]),
            "upper": float(upper[i]),
            "score": float(z[i]),
            "is_anomaly": bool(is_anomaly[i]),
            "observed": bool(observed[i]),
        })
    columns = ["ts", "actual", "expected", "lower", "upper", "score", "is_anomaly", "observed"]

    # ── Facts the narration may restate ──────────────────────────────────
    flagged: List[Dict[str, Any]] = []
    for i in np.flatnonzero(is_anomaly):
        exp_v = float(expected[i])
        dev = (float(y[i]) - exp_v) / abs(exp_v) if exp_v else None
        flagged.append({
            "ts": idx[i].date().isoformat(),
            "period": format_period(idx[i], sf.grain),
            "actual": num(y[i]),
            "expected": num(exp_v),
            "deviation_pct": num(dev),
            "direction": "above" if y[i] > exp_v else "below",
            "score": num(z[i], 2),
            "observed": bool(observed[i]),
        })
    flagged.sort(key=lambda f: abs(f["score"] or 0), reverse=True)

    n_flagged = int(is_anomaly.sum())
    unit = sf.period_label(plural=n_flagged != 1)
    unit_all = sf.period_label(plural=True)
    if n_flagged == 0:
        headline = f"All {n} {unit_all} sit inside the expected range."
    else:
        headline = f"{n_flagged} of {n} {unit_all} fall outside the expected range."
    if n_flagged:
        biggest = flagged[0]
        headline_detail = (
            f"Largest deviation: {biggest['period']} came in "
            f"{fmt_pct(abs(biggest['deviation_pct'])) if biggest['deviation_pct'] is not None else 'n/a'} "
            f"{biggest['direction']} expectation."
        )
    else:
        headline_detail = ""

    # Seasonality is reported independently of the method: whether a period was
    # *detected* (a confirmed strength) and whether the expected line *modelled*
    # it. This is the single authoritative statement — the reader never has to
    # infer what ``auto`` decided.
    grain_unit = sf.period_label(plural=False)
    if confirmed_periods:
        m = confirmed_periods[0]
        strength = sf.seasonal_strength.get(m)
        strength_txt = f" (strength {strength:.2f})" if isinstance(strength, (int, float)) else ""
        if modelled_seasonal:
            season_caveat = f"A {m}-{grain_unit} seasonal pattern was detected{strength_txt} and is modelled in the expected line."
        else:
            season_caveat = f"A {m}-{grain_unit} seasonal pattern was detected{strength_txt}, but the chosen model ignores it."
    elif modelled_seasonal:
        m = periods_for_line[0] if periods_for_line else None
        season_caveat = (f"No seasonal pattern was confirmed; the {m}-{grain_unit} candidate was used "
                         "because a seasonal model was requested.")
    else:
        season_caveat = "No seasonal pattern was detected in this history."

    caveats: List[str] = [season_caveat]
    if cov is not None:
        caveats.append(
            f"{fmt_pct(cov)} of history sits inside the band; sensitivity {params.sensitivity:.2f} "
            f"expects about {fmt_pct(1 - expected_outside)}."
        )
    if any(not o for o in observed):
        caveats.append(f"{sf.periods_filled} of {n} {unit_all} had no rows and were filled ({sf.missing_policy.split(':')[0].lower()}).")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the flags as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "anomaly_detection",
        "measure": sf.request.measure_label,
        "grain": sf.grain,
        "n_points": n,
        "n_flagged": n_flagged,
        "flagged": flagged[:10],
        "band_coverage": num(cov),
        "expected_coverage": num(1 - expected_outside),
        "sensitivity": params.sensitivity,
        "method": method_used,
        "seasonal_periods": list(sf.seasonal_periods),
        "seasonal_detected": bool(confirmed_periods),
        "seasonal_modelled": bool(modelled_seasonal),
        "span_start": idx[0].date().isoformat() if n else None,
        "span_end": idx[-1].date().isoformat() if n else None,
        "fit_wape": num(fit_wape),
        "headline_detail": headline_detail,
    }

    validation = make_validation(
        "WAPE" if fit_metric_ok else "MASE",
        fit_wape if fit_metric_ok else None,
        basis="in-sample fit of the expected line against actuals",
        cov=cov,
        cov_n=cov_n,
        extras={"expected_coverage": 1 - expected_outside, "k": k, "scale": scale},
    )
    if not fit_metric_ok:
        validation.basis = "series has zeros or negatives; fit error reported as band coverage only"

    details = ModelDetails(
        method_used=method_used,
        seasonal_periods=list(sf.seasonal_periods),
        candidates=[
            CandidateScore(name=method_label, metric="fit WAPE", value=num(fit_wape), selected=True),
        ],
        params_used={
            "sensitivity": params.sensitivity,
            "method": params.method,
            "k": round(k, 3),
            "scale": round(scale, 4),
            "seasonal_periods_used": periods_for_line if modelled_seasonal else [],
            "seasonal_strength": {str(m): v for m, v in sf.seasonal_strength.items()},
        },
        notes=notes,
    )

    chart = ChartSpec(
        chart_type="band",
        x_column="ts",
        series=[
            ChartSeriesSpec(role="actual", label="Actual", column="actual"),
            ChartSeriesSpec(role="expected", label="Expected", column="expected"),
            ChartSeriesSpec(role="interval", label=f"{int(round(params.sensitivity * 100))}% band",
                            lower_column="lower", upper_column="upper"),
            ChartSeriesSpec(role="flagged", label="Flagged", column="actual", flag_column="is_anomaly"),
        ],
        y_label=sf.request.measure_label,
    )

    return ResultEnvelope(
        skill="anomaly_detection",
        params=params.model_dump(mode="json"),
        method_used=method_used,
        columns=columns,
        rows=rows_json_safe(rows),
        chart_spec=chart,
        validation=validation,
        guard_results=guard_results,
        low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=n, columns=["ts", "value"]),
        engine=engine_info(ENGINE_NAME, _statsmodels_version(), modules=["anomaly_detection", "common"], runner=ctx.runner),
        provenance=Provenance(
            query_ts=ctx.query_ts or now_iso(),
            sql=ctx.sql,
            filters_summary=ctx.filters_summary,
            missing_policy=sf.missing_policy,
            grain=sf.grain,
            span_start=facts["span_start"],
            span_end=facts["span_end"],
            periods_observed=sf.periods_observed,
            periods_filled=sf.periods_filled,
        ),
        details=details,
        facts=facts,
        headline=headline,
        caveats=caveats,
    )
