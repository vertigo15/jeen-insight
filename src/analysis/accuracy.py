"""Realized accuracy of a captured forecast, once its periods have elapsed.

Pure over plain values (no pandas): which forecast periods are fully elapsed
given the newest source date, and how the stored points compare with the
actuals that later arrived — the same metric rule the engine validated with
(WAPE for a non-negative series, MASE on the frozen scale otherwise), MAE in
business units, and the share of actuals inside the band the user was shown.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from src.analysis.engines.common import band_for

# Calendar helpers (date-only; the series builder truncates to the grain).


def parse_day(value: Any) -> Optional[date]:
    """A calendar day from a date, a datetime/Timestamp (drivers return these
    for DATE_TRUNC), or an ISO string. Always a plain ``date`` so keys match."""
    if value is None:
        return None
    if isinstance(value, datetime):  # before date: datetime is a date subclass
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def period_end(start: date, grain: str) -> date:
    """The last calendar day of the period beginning on ``start``."""
    if grain == "day":
        return start
    if grain == "week":
        return start + timedelta(days=6)
    if grain == "month":
        nxt = date(start.year + (start.month // 12), start.month % 12 + 1, 1)
        return nxt - timedelta(days=1)
    return start


def split_elapsed(points: List[Dict[str, Any]], grain: str, data_end: Optional[date]) -> Dict[str, List[Dict[str, Any]]]:
    """Points whose whole period lies at or before ``data_end`` are ``elapsed``;
    the rest (including a period the newest row falls inside) are ``pending``.
    The incomplete current period is never scored — a month with a week of
    data is not a low actual."""
    elapsed: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for p in points:
        ts = parse_day(p.get("ts"))
        if ts is None or data_end is None or period_end(ts, grain) > data_end:
            pending.append(p)
        else:
            elapsed.append(p)
    return {"elapsed": elapsed, "pending": pending}


def collect_actuals(rows: List[Any], columns: List[str], *, additive: bool, agg: str = "sum") -> Dict[str, float]:
    """``{iso_day: value}`` from the actuals query. Several rows for one period
    (a driver returning the truncated timestamp with a time part, say) are
    combined the way the measure itself was aggregated: summed for SUM/COUNT,
    averaged for AVG, min/max for MIN/MAX."""
    sums: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    lows: Dict[str, float] = {}
    highs: Dict[str, float] = {}
    for r in rows or []:
        rec = dict(r) if not isinstance(r, dict) and hasattr(r, "keys") else r
        if isinstance(rec, (list, tuple)) and columns:
            rec = dict(zip(columns, rec))
        if not isinstance(rec, dict):
            continue
        ts = parse_day(rec.get("ts"))
        if ts is None or rec.get("value") is None:
            continue
        try:
            value = float(rec["value"])
        except (TypeError, ValueError):
            continue
        key = ts.isoformat()
        sums[key] = sums.get(key, 0.0) + value
        counts[key] = counts.get(key, 0) + 1
        lows[key] = min(lows.get(key, value), value)
        highs[key] = max(highs.get(key, value), value)
    if additive:
        return sums
    kind = str(agg or "").lower()
    if kind == "min":
        return lows
    if kind == "max":
        return highs
    return {k: sums[k] / counts[k] for k in sums}


def realized_accuracy(
    points: List[Dict[str, Any]],
    actuals: Dict[str, float],
    *,
    metric: Optional[str],
    mase_scale: Optional[float],
    interval_level: float,
    additive: bool = True,
) -> Dict[str, Any]:
    """Score ``points`` (already filtered to elapsed periods) against ``actuals``
    keyed by ISO date. A period with no actual row counts as zero for an
    additive measure (SUM/COUNT: no rows genuinely means zero — the engine's
    own series preparation did the same); for AVG/MIN/MAX it has no defensible
    value and is left unscored."""
    rows: List[Dict[str, Any]] = []
    abs_err: List[float] = []
    abs_actual: List[float] = []
    inside: List[bool] = []
    skipped: List[str] = []
    for p in points:
        ts = parse_day(p.get("ts"))
        key = ts.isoformat() if ts else str(p.get("ts"))
        if key not in actuals and not additive:
            skipped.append(key)
            continue
        actual = float(actuals.get(key, 0.0))
        forecast = float(p["forecast"])
        lower, upper = p.get("lower"), p.get("upper")
        err = abs(actual - forecast)
        abs_err.append(err)
        abs_actual.append(abs(actual))
        within = None
        if lower is not None and upper is not None:
            within = float(lower) <= actual <= float(upper)
            inside.append(bool(within))
        rows.append({
            "ts": key, "forecast": forecast, "lower": lower, "upper": upper, "actual": actual,
            "error": err, "pct_error": (err / abs(actual)) if actual else None, "inside": within,
        })
    n = len(rows)
    out: Dict[str, Any] = {
        "n": n, "points": rows, "metric": None, "value": None, "band": "n/a",
        "mae": None, "coverage": None, "coverage_n": len(inside), "interval_level": interval_level,
        "unscored_periods": skipped,
    }
    if not n:
        return out
    mae = sum(abs_err) / n
    out["mae"] = round(mae, 4)
    if metric == "WAPE" and sum(abs_actual) > 0:
        out["metric"], out["value"] = "WAPE", round(sum(abs_err) / sum(abs_actual), 4)
    elif mase_scale:
        out["metric"], out["value"] = "MASE", round(mae / float(mase_scale), 4)
    else:
        out["metric"] = "MAE"
        out["value"] = out["mae"]
    out["band"] = band_for(out["metric"], out["value"]) if out["metric"] in ("WAPE", "MASE") else "n/a"
    if inside:
        out["coverage"] = round(sum(1 for i in inside if i) / len(inside), 4)
    return out
