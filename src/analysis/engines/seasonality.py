"""SEASONALITY engine — tier A.

MSTL decomposition on the confirmed periods: trend + seasonal + remainder.
Reports seasonal strength (Hyndman), the phases with the highest and lowest
seasonal effect, the amplitude relative to the level, and the underlying
trend growth once the cycle is removed — the "is Q4 momentum or calendar"
question. A series with no confirmed cycle is a finding too; the engine says
so rather than inventing one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    Egress,
    GuardResult,
    ModelDetails,
    ResultEnvelope,
    SeasonalityParams,
)
from src.analysis.engines.common import (
    RunContext,
    engine_info,
    fmt_pct,
    make_validation,
    num,
    rows_json_safe,
    series_provenance,
    wape,
    wape_applicable,
)
from src.analysis.series import SeriesFrame, format_period

ENGINE_NAME = "statsmodels-mstl"
_PHASE_LABEL = {"month": "month", "week": "week", "day": "weekday"}


def _statsmodels_version() -> str:
    try:
        import statsmodels

        return str(statsmodels.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _phase_name(ts, grain: str, period: int) -> str:
    if grain == "month" and period == 12:
        return ts.strftime("%B")
    if grain == "day" and period == 7:
        return ts.strftime("%A")
    if grain == "week" and period == 52:
        return f"week {int(ts.strftime('%V'))}"
    return format_period(ts, grain)


def run(params: SeasonalityParams, sf: SeriesFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    from statsmodels.nonparametric.smoothers_lowess import lowess
    from statsmodels.tsa.seasonal import MSTL

    ctx = ctx or RunContext()
    guard_results = guard_results or []
    y = sf.y_filled().to_numpy(dtype=float)
    idx = sf.frame.index
    n = len(y)
    observed = sf.frame["observed"].to_numpy(dtype=bool)
    periods = list(sf.seasonal_periods)
    notes: List[str] = []

    if periods:
        windows = [max(7, (2 * (n // p) + 1) | 1) for p in periods]
        res = MSTL(y, periods=periods, windows=windows, stl_kwargs={"seasonal_deg": 0, "robust": False}).fit()
        trend = np.asarray(res.trend, dtype=float)
        seasonal = np.asarray(res.seasonal, dtype=float)
        if seasonal.ndim == 2:
            seasonal = seasonal.sum(axis=1)
        resid = np.asarray(res.resid, dtype=float)
        method_used = f"MSTL, m={','.join(map(str, periods))}"
    else:
        frac = float(min(0.5, max(0.2, 8.0 / max(n, 1))))
        trend = np.asarray(lowess(y, np.arange(n, dtype=float), frac=frac, it=2, return_sorted=False), dtype=float)
        seasonal = np.zeros(n)
        resid = y - trend
        method_used = f"LOWESS trend, no seasonal term (frac={frac:.2f})"
        notes.append("No seasonal pattern could be confirmed on this history; the seasonal component is zero.")

    denom = float(np.var(seasonal + resid))
    strength = float(max(0.0, 1.0 - float(np.var(resid)) / denom)) if denom > 0 else 0.0
    if periods:
        cycles = n / float(max(periods))
        strength = max(0.0, strength - 1.0 / cycles) if cycles > 1 else strength

    level = float(np.mean(np.abs(y))) or 1.0
    amplitude = float(seasonal.max() - seasonal.min()) if periods else 0.0
    peak_i = int(np.argmax(seasonal)) if periods else None
    trough_i = int(np.argmin(seasonal)) if periods else None
    main_period = max(periods) if periods else None
    # Trend growth per year from the first to the last trend value.
    years = max(1e-9, (idx[-1] - idx[0]).days / 365.25)
    growth = (trend[-1] / trend[0]) ** (1.0 / years) - 1.0 if trend[0] not in (0.0,) and trend[0] > 0 and trend[-1] > 0 else None

    rows = [{
        "ts": idx[i].date().isoformat(), "actual": float(y[i]), "trend": float(trend[i]),
        "seasonal": float(seasonal[i]), "residual": float(resid[i]), "observed": bool(observed[i]),
    } for i in range(n)]
    columns = ["ts", "actual", "trend", "seasonal", "residual", "observed"]

    if periods:
        peak_name = _phase_name(idx[peak_i], sf.grain, main_period)
        trough_name = _phase_name(idx[trough_i], sf.grain, main_period)
        headline = (
            f"{sf.request.measure_label} has a {'strong' if strength >= 0.6 else 'moderate' if strength >= 0.3 else 'weak'} "
            f"{main_period}-{sf.period_label()} cycle (strength {strength:.2f}); peaks around {peak_name}, troughs around {trough_name}."
        )
    else:
        headline = f"No seasonal cycle could be confirmed in {sf.request.measure_label} over {n} {sf.period_label(plural=True)}."

    use_wape = wape_applicable(y)
    fit = wape(y, trend + seasonal) if use_wape else None
    validation = make_validation(
        "WAPE" if use_wape else "MASE", fit, basis="in-sample fit of trend + seasonal against actuals",
        extras={"seasonal_strength": strength, "amplitude_share": amplitude / level},
    )
    caveats: List[str] = []
    if periods and n / float(main_period) < 3:
        caveats.append(f"Only {n / float(main_period):.1f} cycles are available; the seasonal shape is averaged across them.")
    if growth is not None:
        caveats.append(f"Once the cycle is removed the trend moves {fmt_pct(growth)} a year.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the decomposition as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "seasonality", "measure": sf.request.measure_label, "grain": sf.grain, "n_points": n,
        "seasonal_periods": periods, "strength": num(strength, 3), "amplitude": num(amplitude),
        "amplitude_share_of_level": num(amplitude / level, 3),
        "peak": {"ts": idx[peak_i].date().isoformat(), "label": _phase_name(idx[peak_i], sf.grain, main_period),
                 "effect": num(seasonal[peak_i])} if periods else None,
        "trough": {"ts": idx[trough_i].date().isoformat(), "label": _phase_name(idx[trough_i], sf.grain, main_period),
                   "effect": num(seasonal[trough_i])} if periods else None,
        "trend_growth_per_year": num(growth),
        "span_start": idx[0].date().isoformat(), "span_end": idx[-1].date().isoformat(),
    }
    details = ModelDetails(
        method_used=method_used, seasonal_periods=periods,
        candidates=[CandidateScore(name=method_used, metric="fit WAPE", value=num(fit), selected=True)],
        params_used={"seasonal_strength": {str(k): v for k, v in sf.seasonal_strength.items()}},
        notes=notes,
    )
    chart = ChartSpec(chart_type="line", x_column="ts", y_columns=["actual", "trend", "seasonal"],
                      y_label=sf.request.measure_label)
    return ResultEnvelope(
        skill="seasonality", params=params.model_dump(mode="json"), method_used=method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=n, columns=["ts", "value"]),
        engine=engine_info(ENGINE_NAME, _statsmodels_version(), modules=["seasonality", "common"], runner=ctx.runner),
        provenance=series_provenance(sf, ctx), details=details, facts=facts, headline=headline, caveats=caveats,
    )
