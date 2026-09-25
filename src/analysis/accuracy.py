"""Realized accuracy of a captured forecast, once its periods have elapsed.

Pure over plain values (no pandas): which forecast periods are fully elapsed
given the newest source date, and how the stored points compare with the
actuals that later arrived — the same metric rule the engine validated with
(WAPE for a non-negative series, MASE on the frozen scale otherwise), MAE in
business units, and the share of actuals inside the band the user was shown.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from src.analysis.engines.common import band_for

# Calendar helpers (date-only; the series builder truncates to the grain).


def parse_day(value: Any) -> Optional[date]:
    if value is None:
        return None
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


def realized_accuracy(
    points: List[Dict[str, Any]],
    actuals: Dict[str, float],
    *,
    metric: Optional[str],
    mase_scale: Optional[float],
    interval_level: float,
) -> Dict[str, Any]:
    """Score ``points`` (already filtered to elapsed periods) against ``actuals``
    keyed by ISO date. A period with no actual row counts as zero for an
    additive measure — that is what the engine's own series preparation did."""
    rows: List[Dict[str, Any]] = []
    abs_err: List[float] = []
    abs_actual: List[float] = []
    inside: List[bool] = []
    for p in points:
        ts = parse_day(p.get("ts"))
        key = ts.isoformat() if ts else str(p.get("ts"))
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
