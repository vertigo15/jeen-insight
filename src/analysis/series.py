"""Series preparation: the aggregate rows become one regular, complete series.

The SQL only groups by ``DATE_TRUNC``; periods with no source rows are simply
absent. Here they become explicit rows with an ``observed=False`` mask so the
engines see a regular calendar and the guards can judge how much was missing.

Fill policy follows the aggregate: SUM/COUNT of nothing is genuinely zero, so
additive measures are zero-filled; an AVG/MIN/MAX of nothing has no defensible
value and stays NaN (the ``gap_ratio`` guard then decides).

Seasonal periods are *candidates* by grain and are only kept when a
seasonal-strength test confirms them on at least two full cycles. Grain alone
never establishes seasonality.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.analysis.contracts import DEFAULT_WINDOW_PERIODS, SeriesRequest

logger = logging.getLogger(__name__)

# Seasonal strength (Hyndman & Athanasopoulos) below which a candidate period
# is treated as absent. 0.3 is deliberately permissive: a weak-but-real weekly
# cycle should still shape the expected line.
SEASONAL_STRENGTH_MIN = 0.30

_PERIOD_LABEL = {"day": "day", "week": "week", "month": "month"}
_PERIOD_LABEL_PLURAL = {"day": "days", "week": "weeks", "month": "months"}


@dataclass
class SeriesFrame:
    """A regular, complete series plus everything the guards and engines ask about."""

    frame: pd.DataFrame  # index: DatetimeIndex (complete calendar); columns: y, observed
    grain: str
    freq: str
    request: SeriesRequest
    candidate_periods: List[int] = field(default_factory=list)
    seasonal_periods: List[int] = field(default_factory=list)
    seasonal_strength: Dict[int, float] = field(default_factory=dict)
    missing_policy: str = ""

    # ── Derived counts ────────────────────────────────────────────────────
    @property
    def n(self) -> int:
        return int(len(self.frame))

    @property
    def periods_observed(self) -> int:
        return int(self.frame["observed"].sum())

    @property
    def periods_filled(self) -> int:
        return self.n - self.periods_observed

    @property
    def gap_ratio(self) -> float:
        return (self.periods_filled / self.n) if self.n else 1.0

    @property
    def zero_share(self) -> float:
        y = self.frame["y"]
        if self.n == 0:
            return 0.0
        return float((y.fillna(0.0) == 0.0).sum() / self.n)

    @property
    def span_start(self) -> Optional[pd.Timestamp]:
        return self.frame.index[0] if self.n else None

    @property
    def span_end(self) -> Optional[pd.Timestamp]:
        return self.frame.index[-1] if self.n else None

    @property
    def longest_period(self) -> Optional[int]:
        return max(self.seasonal_periods) if self.seasonal_periods else None

    def y_filled(self) -> pd.Series:
        """The series with any remaining NaN interpolated (engines need a full vector)."""
        y = self.frame["y"].astype(float)
        if y.isna().any():
            y = y.interpolate(limit_direction="both")
            y = y.fillna(0.0)
        return y

    def period_label(self, plural: bool = False) -> str:
        table = _PERIOD_LABEL_PLURAL if plural else _PERIOD_LABEL
        return table.get(self.grain, self.grain)


# ── Calendar helpers ──────────────────────────────────────────────────────────


def pandas_freq(grain: str, week_start: str = "monday") -> str:
    if grain == "day":
        return "D"
    if grain == "week":
        return "W-MON" if week_start == "monday" else "W-SUN"
    if grain == "month":
        return "MS"
    raise ValueError(f"unsupported grain {grain!r}")


def floor_to_grain(ts: pd.Series, grain: str, week_start: str = "monday") -> pd.Series:
    """Snap timestamps to the start of their period so they align with the calendar."""
    ts = pd.to_datetime(ts, errors="coerce")
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    ts = ts.dt.normalize()
    if grain == "day":
        return ts
    if grain == "week":
        offset = ts.dt.weekday if week_start == "monday" else (ts.dt.weekday + 1) % 7
        return ts - pd.to_timedelta(offset, unit="D")
    if grain == "month":
        return ts.dt.to_period("M").dt.to_timestamp()
    raise ValueError(f"unsupported grain {grain!r}")


def default_window(grain: str) -> int:
    return DEFAULT_WINDOW_PERIODS.get(grain, 26)


def min_points_for_period(period: int) -> int:
    """Observations needed before ``period`` may be tested or decomposed.

    STL needs strictly more than two full cycles (statsmodels rejects a period
    of exactly ``n / 2``), so every seasonal engine shares this one boundary
    rather than degrading differently at ``n == 2 * period``.
    """
    return 2 * period + 1


def candidate_periods(grain: str, n: int) -> List[int]:
    """Periods worth testing for this grain given ``n`` observations."""
    if grain == "day":
        cands = [7]
        if n >= min_points_for_period(365):
            cands.append(365)
    elif grain == "week":
        cands = [52]
    elif grain == "month":
        cands = [12]
    else:
        cands = []
    return [m for m in cands if n >= min_points_for_period(m)]


def seasonal_strength(y: Sequence[float], period: int) -> float:
    """Hyndman's seasonal strength F_s = 1 - Var(R) / Var(S + R), noise-adjusted.

    The seasonal component is estimated *periodically* (one value per phase,
    averaged over the ``c = n / period`` cycles). Averaging pure noise over
    ``c`` cycles still yields a seasonal component with variance σ²/c, so a
    white-noise series scores about ``1 / c`` — 0.4 with two and a half
    cycles. That floor is subtracted; what remains is strength the noise
    cannot explain. Robust STL is off for the same reason as in the anomaly
    engine: with few cycles it interpolates rather than averages.
    """
    arr = np.asarray(y, dtype=float)
    n = len(arr)
    if n < 2 * period or period < 2:
        return 0.0
    if np.allclose(arr, arr[0]):
        return 0.0
    try:
        from statsmodels.tsa.seasonal import STL

        cycles = n / float(period)
        window = max(7, (2 * (n // period) + 1) | 1)
        res = STL(arr, period=period, seasonal=window, seasonal_deg=0, robust=False).fit()
        resid = np.asarray(res.resid, dtype=float)
        seasonal = np.asarray(res.seasonal, dtype=float)
        denom = float(np.var(seasonal + resid))
        if denom <= 0:
            return 0.0
        raw = 1.0 - float(np.var(resid)) / denom
        return float(max(0.0, raw - 1.0 / cycles))
    except Exception:  # noqa: BLE001 — a failed test means "not confirmed"
        logger.debug("seasonal_strength failed for period=%s", period, exc_info=True)
        return 0.0


# ── Row parsing ───────────────────────────────────────────────────────────────


def _rows_to_frame(
    rows: Iterable[Any],
    columns: Optional[Sequence[str]],
    ts_key: str,
    value_key: str,
    extra_keys: Sequence[str] = (),
) -> pd.DataFrame:
    """Accept dict rows or positional rows (with ``columns``).

    ``extra_keys`` are further numeric columns carried through (a second
    measure for correlation). A ``series_id`` column is kept when present.
    """
    records: List[Dict[str, Any]] = []
    cols = list(columns or [])
    for row in rows or []:
        if isinstance(row, dict):
            rec = row
        elif isinstance(row, (list, tuple)) and cols:
            rec = dict(zip(cols, row))
        elif hasattr(row, "keys"):
            rec = dict(row)
        else:
            continue
        ts = rec.get(ts_key)
        if ts is None and cols:
            ts = rec.get(cols[0])
        val = rec.get(value_key)
        if val is None and len(cols) > 1 and value_key not in rec:
            val = rec.get(cols[-1])
        record: Dict[str, Any] = {"ts": ts, "value": val}
        for key in extra_keys:
            record[key] = rec.get(key)
        if "series_id" in rec:
            record["series_id"] = rec.get("series_id")
        records.append(record)
    frame_cols = ["ts", "value", *extra_keys] + (["series_id"] if records and "series_id" in records[0] else [])
    df = pd.DataFrame.from_records(records, columns=frame_cols)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    for key in extra_keys:
        df[key] = pd.to_numeric(df[key], errors="coerce")
    return df.dropna(subset=["ts"])


# ── Public entry point ────────────────────────────────────────────────────────


def prepare_series(
    rows: Iterable[Any],
    request: SeriesRequest,
    *,
    columns: Optional[Sequence[str]] = None,
    ts_key: str = "ts",
    value_key: str = "value",
    detect_seasonality: bool = True,
    extra_values: Sequence[Tuple[str, bool]] = (),
) -> SeriesFrame:
    """Turn aggregate rows into a regular, complete :class:`SeriesFrame`.

    The calendar spans ``[request.start, request.end)`` when both are given,
    otherwise the observed span. Periods without a source row get
    ``observed=False`` and the fill the aggregate warrants.

    ``extra_values`` are ``(column, additive)`` pairs for further measures that
    ride along on the same calendar (correlation's second series); they land
    in ``frame[column]`` with the same fill rule as the primary.
    """
    grain = request.grain
    freq = pandas_freq(grain, request.week_start)
    extra_keys = [name for name, _ in extra_values]
    raw = _rows_to_frame(rows, columns, ts_key, value_key, extra_keys=extra_keys)

    if raw.empty:
        empty = pd.DataFrame({"y": pd.Series(dtype=float), "observed": pd.Series(dtype=bool)})
        empty.index = pd.DatetimeIndex([], name="ts")
        return SeriesFrame(frame=empty, grain=grain, freq=freq, request=request,
                           missing_policy="no rows")

    raw["ts"] = floor_to_grain(raw["ts"], grain, request.week_start)
    # Defensive: the SQL already grouped by period, but a driver/timezone quirk
    # can split one period in two. Additive → sum, otherwise mean.
    agg_fn = "sum" if request.is_additive else "mean"
    grouped = raw.groupby("ts", sort=True)["value"].agg(agg_fn)
    extra_grouped = {
        name: raw.groupby("ts", sort=True)[name].agg("sum" if additive else "mean")
        for name, additive in extra_values
    }

    # Calendar bounds: explicit request range wins; end is exclusive.
    first = grouped.index.min()
    last = grouped.index.max()
    if request.start is not None:
        start_ts = floor_to_grain(pd.Series([pd.Timestamp(request.start)]), grain, request.week_start).iloc[0]
        first = min(first, start_ts) if not pd.isna(start_ts) else first
    if request.end is not None:
        end_excl = floor_to_grain(pd.Series([pd.Timestamp(request.end)]), grain, request.week_start).iloc[0]
        # Only extend the tail when the requested end lands after the last observed period.
        one = pd.tseries.frequencies.to_offset(freq)
        tail_candidate = end_excl - one if end_excl > last else last
        last = max(last, tail_candidate)

    calendar = pd.date_range(start=first, end=last, freq=freq, name="ts")
    y = grouped.reindex(calendar)
    observed = y.notna()

    if request.is_additive:
        y = y.fillna(0.0)
        policy = (
            f"{request.agg.upper()} of an empty {grain} is zero: "
            f"{int((~observed).sum())} of {len(calendar)} periods zero-filled"
        )
    else:
        policy = (
            f"{request.agg.upper()} has no value for an empty {grain}: "
            f"{int((~observed).sum())} of {len(calendar)} periods left empty"
        )

    frame = pd.DataFrame({"y": y.astype(float), "observed": observed.astype(bool)}, index=calendar)
    for (name, additive), series in zip(extra_values, extra_grouped.values()):
        col = series.reindex(calendar).astype(float)
        frame[name] = col.fillna(0.0) if additive else col
    sf = SeriesFrame(frame=frame, grain=grain, freq=freq, request=request, missing_policy=policy)

    sf.candidate_periods = candidate_periods(grain, sf.n)
    if detect_seasonality and sf.candidate_periods:
        yf = sf.y_filled().to_numpy()
        for m in sf.candidate_periods:
            strength = seasonal_strength(yf, m)
            sf.seasonal_strength[m] = round(strength, 3)
            if strength >= SEASONAL_STRENGTH_MIN:
                sf.seasonal_periods.append(m)
    return sf


MAX_SERIES_GROUPS = 12


def split_series_groups(
    rows: Iterable[Any],
    columns: Optional[Sequence[str]] = None,
    *,
    key: str = "series_id",
) -> Dict[str, List[Dict[str, Any]]]:
    """Split aggregate rows by ``series_id`` (multi-series requests).

    Preserves first-seen order so the caller can cap deterministically; rows
    without a series id fall into the ``""`` group.
    """
    cols = list(columns or [])
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows or []:
        if isinstance(row, dict):
            rec = row
        elif isinstance(row, (list, tuple)) and cols:
            rec = dict(zip(cols, row))
        elif hasattr(row, "keys"):
            rec = dict(row)
        else:
            continue
        sid = rec.get(key)
        groups.setdefault("" if sid is None else str(sid), []).append(rec)
    return groups


def series_payload(sf: SeriesFrame) -> Dict[str, Any]:
    """JSON-safe rows for the runner boundary (ISO timestamps, floats, bools)."""
    rows = []
    for ts, row in sf.frame.iterrows():
        val = row["y"]
        rows.append({
            "ts": ts.date().isoformat(),
            "value": None if (val is None or (isinstance(val, float) and math.isnan(val))) else float(val),
            "observed": bool(row["observed"]),
        })
    return {"columns": ["ts", "value", "observed"], "rows": rows}


def format_period(ts: pd.Timestamp, grain: str) -> str:
    """Human label for one period: '18 Feb 2026', 'week of 18 Feb 2026', 'Feb 2026'."""
    ts = pd.Timestamp(ts)
    if grain == "month":
        return ts.strftime("%b %Y")
    if grain == "week":
        return f"week of {ts.day} {ts.strftime('%b %Y')}"
    return f"{ts.day} {ts.strftime('%b %Y')}"
