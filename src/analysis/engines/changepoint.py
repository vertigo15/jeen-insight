"""CHANGEPOINT engine — tier A.

Where did the *level* of the series shift? The seasonal cycle is removed
first (MSTL on the confirmed periods, else a LOWESS trend), then PELT
(``ruptures``) searches the trend for mean shifts with an L2 cost and a BIC
penalty scaled by the residual variance, so noisier series need a bigger step
before a break is declared. Each break reports the level before, the level
after, the relative jump and a rough confidence from the pooled residual scale.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from src.analysis.contracts import (
    CandidateScore,
    ChangepointParams,
    ChartSeriesSpec,
    ChartSpec,
    Egress,
    GuardResult,
    ModelDetails,
    ResultEnvelope,
)
from src.analysis.engines.common import (
    RunContext,
    engine_info,
    fmt_pct,
    fmt_value,
    make_validation,
    num,
    pct_change,
    rows_json_safe,
    series_provenance,
    wape,
    wape_applicable,
)
from src.analysis.series import SeriesFrame, format_period

ENGINE_NAME = "ruptures-pelt"


def _versions() -> str:
    try:
        import ruptures

        return str(ruptures.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _deseasonalise(y: np.ndarray, periods: List[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, List[str]]:
    """Return (deseasonalised series, trend, noise residual, label, notes).

    Breaks are searched on ``y - seasonal`` (not on a smoothed trend: smoothing
    turns a step into a ramp and PELT then reads the ramp as a staircase). The
    residual excludes the cycle so the penalty reflects noise, not season.
    """
    notes: List[str] = []
    n = len(y)
    if periods:
        try:
            from statsmodels.tsa.seasonal import MSTL

            windows = [max(7, (2 * (n // p) + 1) | 1) for p in periods]
            res = MSTL(y, periods=periods, windows=windows, stl_kwargs={"seasonal_deg": 0, "robust": False}).fit()
            seasonal = np.asarray(res.seasonal, dtype=float)
            if seasonal.ndim == 2:
                seasonal = seasonal.sum(axis=1)
            return (y - seasonal, np.asarray(res.trend, dtype=float), np.asarray(res.resid, dtype=float),
                    f"de-seasonalised series (MSTL, m={','.join(map(str, periods))})", notes)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Seasonal decomposition failed ({type(exc).__name__}); searching the raw series.")
    from statsmodels.nonparametric.smoothers_lowess import lowess

    frac = float(min(0.5, max(0.2, 8.0 / max(n, 1))))
    smoothed = np.asarray(lowess(y, np.arange(n, dtype=float), frac=frac, it=2, return_sorted=False), dtype=float)
    if not periods:
        notes.append("No seasonal pattern could be confirmed; breaks are searched on the raw series.")
    return y.copy(), smoothed, y - smoothed, "raw series", notes


def _drop_seasonal_echoes(breaks: List[int], signal: np.ndarray, periods: List[int], n: int, win: int):
    """With fewer than four cycles the periodic seasonal estimate leaks a level
    shift into the same phase of the neighbouring cycles, as a shift of the
    opposite sign. Two breaks exactly one period (±3) apart cannot be told
    apart from that leak, so the smaller one is dropped and the caveat says so."""
    if not periods or not breaks:
        return breaks, []
    m = max(periods)
    if n / float(m) >= 4:
        return breaks, []

    def jump(b: int) -> float:
        return abs(float(signal[b:min(n, b + win)].mean()) - float(signal[max(0, b - win):b].mean()))

    jumps = {b: jump(b) for b in breaks}
    echoes = []
    for b in breaks:
        for other in breaks:
            if other == b or other in echoes:
                continue
            if abs(abs(b - other) - m) <= 3 and jumps[other] >= jumps[b]:
                echoes.append(b)
                break
    return [b for b in breaks if b not in echoes], echoes


def run(params: ChangepointParams, sf: SeriesFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    import ruptures as rpt

    ctx = ctx or RunContext()
    guard_results = guard_results or []
    y = sf.y_filled().to_numpy(dtype=float)
    idx = sf.frame.index
    n = len(y)
    observed = sf.frame["observed"].to_numpy(dtype=bool)

    deseason, trend, resid, method_label, notes = _deseasonalise(y, list(sf.seasonal_periods))
    # Robust noise scale from the residual (MAD → sigma), inflated for the
    # variance the periodic seasonal average absorbed when cycles are few.
    mad = float(np.median(np.abs(resid - np.median(resid)))) * 1.4826
    sigma = mad or float(np.std(resid)) or float(np.std(np.diff(trend))) or 1e-9
    cycles = (n / float(max(sf.seasonal_periods))) if sf.seasonal_periods else None
    if cycles and cycles > 1:
        sigma /= math.sqrt(max(1e-9, 1.0 - 1.0 / cycles))
    if cycles and cycles < 4:
        notes.append(
            f"Only {cycles:.1f} cycles of the {max(sf.seasonal_periods)}-period season are available; a shift and a "
            "seasonal swing are hard to tell apart at this length, so treat breaks near a cycle boundary with care."
        )
    # Piecewise-*linear* cost: each regime has its own intercept and slope, so a
    # steady ramp is one regime and a step (same slope, new level) is two. A
    # mean-shift cost would read the ramp as a staircase. BIC-style penalty: a
    # break must save more than a log(n) multiple of the noise variance.
    penalty = 3.0 * math.log(max(n, 2)) * sigma ** 2
    min_size = max(3, min(int(params.min_segment), max(3, n // 4)))
    x = np.arange(n, dtype=float) / max(n, 1)
    signal = np.column_stack([deseason, x, np.ones(n)])
    algo = rpt.Pelt(model="linear", min_size=min_size, jump=1).fit(signal)
    breaks = [b for b in algo.predict(pen=penalty) if b < n]
    breaks, echoes = _drop_seasonal_echoes(breaks, deseason, list(sf.seasonal_periods), n, min_size)
    if echoes:
        notes.append(
            f"{len(echoes)} smaller break{'s' if len(echoes) != 1 else ''} exactly one cycle away from a larger one "
            "was dropped as a seasonal echo (few cycles of history make the seasonal estimate leak a shift)."
        )
    if len(breaks) > params.max_changepoints:
        # Keep the largest level shifts.
        def _jump(b: int) -> float:
            left = deseason[max(0, b - min_size):b].mean()
            right = deseason[b:min(n, b + min_size)].mean()
            return abs(right - left)
        breaks = sorted(sorted(breaks, key=_jump, reverse=True)[: params.max_changepoints])
        notes.append(f"More than {params.max_changepoints} breaks were found; the {params.max_changepoints} largest are reported.")

    # Per-segment linear fit (in the de-seasonalised series) gives the level
    # line the chart draws and the before/after levels at each break.
    bounds = [0] + breaks + [n]
    segment_of = np.zeros(n, dtype=int)
    level = np.zeros(n, dtype=float)
    for s, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:])):
        segment_of[lo:hi] = s
        if hi - lo >= 2:
            xs = np.arange(lo, hi, dtype=float)
            slope, intercept = np.polyfit(xs, deseason[lo:hi], 1)
            level[lo:hi] = slope * xs + intercept
        else:
            level[lo:hi] = deseason[lo:hi]
    is_cp = np.zeros(n, dtype=bool)
    changepoints: List[Dict[str, Any]] = []
    for b in breaks:
        is_cp[b] = True
        before = float(level[b - 1]) if b > 0 else float("nan")
        after = float(level[b])
        magnitude = after - before
        conf = 1.0 - math.exp(-abs(magnitude) / (sigma * math.sqrt(2.0 / max(min_size, 1))) ) if sigma > 0 else 1.0
        changepoints.append({
            "ts": idx[b].date().isoformat(),
            "period": format_period(idx[b], sf.grain),
            "direction": "up" if magnitude > 0 else "down",
            "level_before": num(before),
            "level_after": num(after),
            "magnitude": num(magnitude),
            "magnitude_pct": pct_change(after, before),
            "confidence": num(min(0.99, max(0.0, conf)), 2),
        })

    # Add the seasonal component back so the level line sits on the actuals.
    level_plot = level + (y - deseason)
    rows = [{
        "ts": idx[i].date().isoformat(), "actual": float(y[i]), "trend": float(trend[i]),
        "level": float(level_plot[i]), "segment": int(segment_of[i]), "is_changepoint": bool(is_cp[i]),
        "observed": bool(observed[i]),
    } for i in range(n)]
    columns = ["ts", "actual", "trend", "level", "segment", "is_changepoint", "observed"]

    unit_all = sf.period_label(plural=True)
    if not changepoints:
        headline = f"No level shift found across {n} {unit_all}; the series holds one regime."
    elif len(changepoints) == 1:
        cp = changepoints[0]
        headline = (f"The level shifted {cp['direction']} in the {cp['period']}: "
                    f"from {fmt_value(cp['level_before'])} to {fmt_value(cp['level_after'])}"
                    + (f" ({fmt_pct(abs(cp['magnitude_pct']))})." if cp["magnitude_pct"] is not None else "."))
    else:
        biggest = max(changepoints, key=lambda c: abs(c["magnitude"] or 0))
        headline = (f"{len(changepoints)} level shifts across {n} {unit_all}; the largest is the {biggest['period']} "
                    f"({biggest['direction']} to {fmt_value(biggest['level_after'])}).")

    use_wape = wape_applicable(y)
    fit = wape(y, level_plot) if use_wape else None
    validation = make_validation(
        "WAPE" if use_wape else "MASE", fit,
        basis="in-sample fit of the per-regime lines against actuals",
        extras={"penalty": penalty, "sigma": sigma},
    )
    caveats = [
        "A break marks the first period of the new regime; timing is uncertain by a few periods when the shift is gradual.",
    ] + notes
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the breaks as indicative.")

    facts = {
        "skill": "changepoint", "measure": sf.request.measure_label, "grain": sf.grain, "n_points": n,
        "n_changepoints": len(changepoints), "changepoints": changepoints,
        "seasonal_periods": list(sf.seasonal_periods), "method": method_label,
        "span_start": idx[0].date().isoformat(), "span_end": idx[-1].date().isoformat(),
    }
    details = ModelDetails(
        method_used=f"PELT (piecewise linear) on {method_label}",
        seasonal_periods=list(sf.seasonal_periods),
        candidates=[CandidateScore(name="PELT piecewise-linear", metric="fit WAPE", value=num(fit), selected=True)],
        params_used={"min_segment": min_size, "penalty": round(penalty, 4), "max_changepoints": params.max_changepoints},
        notes=notes,
    )
    chart = ChartSpec(
        chart_type="band", x_column="ts",
        series=[
            ChartSeriesSpec(role="actual", label="Actual", column="actual"),
            ChartSeriesSpec(role="expected", label="Segment level", column="level"),
            ChartSeriesSpec(role="flagged", label="Changepoint", column="actual", flag_column="is_changepoint"),
        ],
        y_label=sf.request.measure_label,
    )
    return ResultEnvelope(
        skill="changepoint", params=params.model_dump(mode="json"), method_used=details.method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=n, columns=["ts", "value"]),
        engine=engine_info(ENGINE_NAME, _versions(), modules=["changepoint", "common"], runner=ctx.runner),
        provenance=series_provenance(sf, ctx), details=details, facts=facts, headline=headline, caveats=caveats,
    )
