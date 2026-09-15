"""Shared helpers for the engines: metrics, bands, engine identity, row shaping."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.analysis.contracts import EngineInfo, Validation

# Band thresholds shown to a business user. WAPE is a share of total volume;
# MASE < 1 means "better than naive", so 'good' is comfortably below that.
WAPE_GOOD, WAPE_FAIR = 0.10, 0.25
MASE_GOOD, MASE_FAIR = 0.80, 1.20


@dataclass(frozen=True)
class RunContext:
    """What the engine is allowed to know beyond params + series."""

    sql: Optional[str] = None
    query_ts: Optional[str] = None
    filters_summary: str = ""
    runner: str = "in_process"
    low_confidence: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def module_hash(*modules: str) -> str:
    """Short hash of the engine source so Model details can name the exact code."""
    h = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for name in modules:
        path = root / f"{name}.py"
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def engine_info(name: str, version: str, *, modules: Sequence[str], runner: str) -> EngineInfo:
    return EngineInfo(name=name, version=version, module_hash=module_hash(*modules), runner=runner)


# ── Metrics ───────────────────────────────────────────────────────────────────


def wape(actual: Sequence[float], predicted: Sequence[float]) -> Optional[float]:
    a = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(p))
    a, p = a[mask], p[mask]
    denom = float(np.sum(np.abs(a)))
    if a.size == 0 or denom <= 0:
        return None
    return float(np.sum(np.abs(a - p)) / denom)


def mae(actual: Sequence[float], predicted: Sequence[float]) -> Optional[float]:
    a = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(p))
    if not mask.any():
        return None
    return float(np.mean(np.abs(a[mask] - p[mask])))


def naive_scale(history: Sequence[float], season: int = 1) -> Optional[float]:
    """In-sample MAE of the (seasonal) naive forecaster — the MASE denominator."""
    h = np.asarray(history, dtype=float)
    season = max(1, int(season))
    if h.size <= season:
        return None
    diffs = np.abs(h[season:] - h[:-season])
    scale = float(np.nanmean(diffs))
    return scale if scale > 0 else None


def mase(actual: Sequence[float], predicted: Sequence[float], history: Sequence[float], season: int = 1) -> Optional[float]:
    err = mae(actual, predicted)
    scale = naive_scale(history, season)
    if err is None or scale is None:
        return None
    return float(err / scale)


def wape_applicable(values: Sequence[float]) -> bool:
    """WAPE only makes sense for a non-negative series with a positive total."""
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    return v.size > 0 and bool(np.all(v >= 0)) and float(np.sum(v)) > 0


def score(
    actual: Sequence[float],
    predicted: Sequence[float],
    *,
    history: Sequence[float],
    season: int = 1,
    use_wape: bool,
) -> Tuple[str, Optional[float]]:
    if use_wape:
        return "WAPE", wape(actual, predicted)
    return "MASE", mase(actual, predicted, history, season)


def band_for(metric: str, value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if metric == "WAPE":
        return "good" if value < WAPE_GOOD else "fair" if value < WAPE_FAIR else "poor"
    if metric == "MASE":
        return "good" if value < MASE_GOOD else "fair" if value < MASE_FAIR else "poor"
    return "n/a"


def coverage(actual: Sequence[float], lower: Sequence[float], upper: Sequence[float]) -> Tuple[Optional[float], int]:
    a = np.asarray(actual, dtype=float)
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(lo) | np.isnan(hi))
    n = int(mask.sum())
    if n == 0:
        return None, 0
    inside = np.sum((a[mask] >= lo[mask]) & (a[mask] <= hi[mask]))
    return float(inside / n), n


def make_validation(
    metric: str,
    value: Optional[float],
    *,
    basis: str,
    cov: Optional[float] = None,
    cov_n: Optional[int] = None,
    extras: Optional[Dict[str, float]] = None,
) -> Validation:
    return Validation(
        metric=metric,
        value=_round(value),
        band=band_for(metric, value),  # type: ignore[arg-type]
        basis=basis,
        coverage=_round(cov),
        coverage_n=cov_n,
        extras={k: _round(v) for k, v in (extras or {}).items() if v is not None},  # type: ignore[misc]
    )


# ── Values ────────────────────────────────────────────────────────────────────


def _round(value: Any, digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def num(value: Any, digits: int = 4) -> Optional[float]:
    """JSON-safe float or None (NaN/inf collapse to None)."""
    return _round(value, digits)


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return _round((new - old) / abs(old), 4)


def fmt_value(value: Optional[float]) -> str:
    """Compact business formatting: 1.47M, 640k, 2,840, 0.71."""
    if value is None:
        return "n/a"
    v = float(value)
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 1e3:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def fmt_pct(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%".replace(".0%", "%")


def series_provenance(sf: Any, ctx: RunContext) -> "Provenance":
    """Provenance block for a SeriesFrame-based skill."""
    from src.analysis.contracts import Provenance  # noqa: PLC0415

    n = sf.n
    return Provenance(
        query_ts=ctx.query_ts or now_iso(),
        sql=ctx.sql,
        filters_summary=ctx.filters_summary,
        missing_policy=sf.missing_policy,
        grain=sf.grain,
        span_start=sf.frame.index[0].date().isoformat() if n else None,
        span_end=sf.frame.index[-1].date().isoformat() if n else None,
        periods_observed=sf.periods_observed,
        periods_filled=sf.periods_filled,
    )


def table_provenance(ctx: RunContext, *, rows_in: int, note: str = "") -> "Provenance":
    """Provenance block for a skill that consumes a plain table (contribution, tier B)."""
    from src.analysis.contracts import Provenance  # noqa: PLC0415

    return Provenance(
        query_ts=ctx.query_ts or now_iso(),
        sql=ctx.sql,
        filters_summary=ctx.filters_summary,
        missing_policy=note or f"{rows_in:,} rows as returned by the query",
        periods_observed=rows_in,
    )


def rows_json_safe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        clean: Dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, (np.floating, float)):
                clean[k] = None if (math.isnan(float(v)) or math.isinf(float(v))) else float(v)
            elif isinstance(v, (np.integer,)):
                clean[k] = int(v)
            elif isinstance(v, (np.bool_,)):
                clean[k] = bool(v)
            else:
                clean[k] = v
        out.append(clean)
    return out
