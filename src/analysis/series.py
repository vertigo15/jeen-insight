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

The trailing period is the one the calendar cannot vouch for: a month whose
data stops on the 16th is a month-to-date figure, not a month. Such a period
is *set aside* — kept on the frame as :attr:`SeriesFrame.partial_tail` so the
result can name it, but removed from what the guards and engines see — rather
than fed to a model that would take the shortfall for a collapse.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.analysis.contracts import DEFAULT_WINDOW_PERIODS, SeriesRequest
from src.analysis.engines.common import fmt_value

logger = logging.getLogger(__name__)

# Seasonal strength (Hyndman & Athanasopoulos) below which a candidate period
# is treated as absent. 0.3 is deliberately permissive: a weak-but-real weekly
# cycle should still shape the expected line.
SEASONAL_STRENGTH_MIN = 0.30

# A trailing week or month is incomplete when its data stops more than this
# many days before the period's last day. The slack keeps a Friday-ending week
# or a month that ends on a weekend from being set aside for a business that
# is closed at weekends.
PARTIAL_TAIL_SLACK_DAYS = 2
# Without a data-end timestamp, an additive tail (SUM/COUNT) is taken as
# incomplete when it is below this share of the typical recent period while the
# period before it was ordinary — the signature of a load that stopped
# mid-period. A genuine collapse looks the same for one period; the caveat
# names the numbers either way so the reader can tell.
PARTIAL_TAIL_VALUE_SHARE = 0.25
PARTIAL_TAIL_PRIOR_SHARE_MIN = 0.50
PARTIAL_TAIL_REFERENCE_MIN = 6
PARTIAL_TAIL_REFERENCE_MAX = 12

_PERIOD_LABEL = {"day": "day", "week": "week", "month": "month"}
_PERIOD_LABEL_PLURAL = {"day": "days", "week": "weeks", "month": "months"}


@dataclass(frozen=True)
class PartialTail:
    """A trailing period set aside because its data does not cover the whole period.

    ``value`` is the aggregate observed so far (the to-date figure); ``reason``
    carries the numbers behind the decision; ``basis`` is ``"date"`` when the
    data-end timestamp proved it and ``"value"`` when only the shortfall did.
    """

    ts: pd.Timestamp
    value: Optional[float]
    reason: str
    basis: str


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
    # The incomplete trailing period that was set aside, if any. It is not in
    # ``frame``; ``n`` and the guards count complete periods only.
    partial_tail: Optional[PartialTail] = None

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


# ── Incomplete trailing period ────────────────────────────────────────────────


def period_last_day(ts: pd.Timestamp, freq: str) -> pd.Timestamp:
    """The final calendar day of the period that starts at ``ts``."""
    one = pd.tseries.frequencies.to_offset(freq)
    return (pd.Timestamp(ts) + one) - pd.Timedelta(days=1)


def _fmt_day(ts: pd.Timestamp) -> str:
    return f"{ts.day} {ts.strftime('%b %Y')}"


def parse_data_end(data_end: Optional[Any]) -> Optional[pd.Timestamp]:
    """The newest source timestamp as a naive midnight, or None when unreadable."""
    if data_end is None:
        return None
    try:
        end = pd.Timestamp(data_end)
    except (TypeError, ValueError):
        logger.debug("parse_data_end: unreadable data_end %r ignored", data_end)
        return None
    if pd.isna(end):
        return None
    if end.tzinfo is not None:
        end = end.tz_localize(None)
    return end.normalize()


# Same-phase lag per grain for the seasonal check of the value rule.
_SEASON_LAG = {"month": 12, "week": 52, "day": 7}


def _same_phase_was_low(prior: pd.Series, grain: str, share: float) -> bool:
    """True when the period one cycle before the tail was itself under ``share``
    of the periods around it — the tail is then a season, not a stub."""
    lag = _SEASON_LAG.get(grain)
    if lag is None or len(prior) < lag + PARTIAL_TAIL_REFERENCE_MIN:
        return False
    same = float(prior.iloc[-lag])
    # Its neighbourhood: up to a cycle before it and everything after it up to the tail.
    around = pd.concat([prior.iloc[-lag - PARTIAL_TAIL_REFERENCE_MAX:-lag], prior.iloc[-lag + 1:]])
    reference = float(np.median(around.to_numpy(dtype=float))) if len(around) else 0.0
    return reference > 0 and same < share * reference


def detect_partial_tail(
    frame: pd.DataFrame,
    grain: str,
    freq: str,
    request: SeriesRequest,
    data_end: Optional[Any] = None,
) -> Optional[PartialTail]:
    """Decide whether the last period of ``frame`` is incomplete.

    Two rules:

    * **date** — ``data_end`` (the newest source timestamp) falls inside the
      last week/month and stops more than :data:`PARTIAL_TAIL_SLACK_DAYS`
      before its final day. A day is atomic at date resolution and is never
      partial by this rule.
    * **value** — for a SUM/COUNT of a non-negative history, the tail is under
      :data:`PARTIAL_TAIL_VALUE_SHARE` of the typical recent period while the
      period before it was at least half of typical, and the same period one
      cycle earlier was not itself that low (a seasonal trough or a holiday
      shutdown is a season, not a stub). Daily series compare with the same
      weekday so a quiet Sunday is not mistaken for a partial day.

    The value rule runs even when ``data_end`` shows the period's days are
    covered: a load that stops mid-period is not the only way a period ends up
    incomplete — the AdventureWorks sample has rows on every day of its final
    month at 3% of the usual total. The reason then says so, and the caveat
    calls the period a *likely* partial load rather than a fact.
    """
    if len(frame) < 2:
        return None
    ts = pd.Timestamp(frame.index[-1])
    observed = bool(frame["observed"].iloc[-1])
    raw = frame["y"].iloc[-1]
    value = None if pd.isna(raw) else float(raw)

    end = parse_data_end(data_end) if grain != "day" else None
    days_covered = False
    if end is not None:
        last_day = period_last_day(ts, freq)
        if ts <= end < last_day:
            missing = int((last_day - end).days)
            if missing > PARTIAL_TAIL_SLACK_DAYS:
                total = int((last_day - ts).days) + 1
                covered = int((end - ts).days) + 1
                return PartialTail(
                    ts=ts, value=value, basis="date",
                    reason=f"data ends {_fmt_day(end)}, {covered} of {total} days",
                )
        days_covered = end >= ts

    if not (request.is_additive and observed and value is not None and value >= 0):
        return None
    prior = frame["y"].iloc[:-1].astype(float).fillna(0.0)
    if len(prior) < PARTIAL_TAIL_REFERENCE_MIN or bool((prior < 0).any()):
        return None
    if grain == "day" and len(prior) >= 3 * 7:
        # Same weekday, most recent first: ``previous`` is a week ago, not yesterday.
        reference_pool = prior.iloc[-7::-7].iloc[:PARTIAL_TAIL_REFERENCE_MAX]
        previous = float(reference_pool.iloc[0])
    else:
        reference_pool = prior.iloc[-PARTIAL_TAIL_REFERENCE_MAX:]
        previous = float(prior.iloc[-1])
    reference = float(np.median(reference_pool.to_numpy(dtype=float)))
    if reference <= 0 or value >= PARTIAL_TAIL_VALUE_SHARE * reference:
        return None
    if previous < PARTIAL_TAIL_PRIOR_SHARE_MIN * reference:
        return None  # already falling before the tail: a trend, not a partial load
    if grain != "day" and _same_phase_was_low(prior, grain, PARTIAL_TAIL_PRIOR_SHARE_MIN):
        return None  # this period is low every cycle: a season, not a partial load
    reason = f"{fmt_value(value)} against a typical {fmt_value(reference)}"
    reason += f", although its data runs to {_fmt_day(end)}" if days_covered and end is not None else " so far"
    return PartialTail(ts=ts, value=value, basis="value", reason=reason)


def partial_tail_sentence(sf: "SeriesFrame") -> str:
    """One caveat sentence naming the set-aside period and why."""
    tail = sf.partial_tail
    if tail is None:
        return ""
    period = format_period(tail.ts, sf.grain)
    ends = f"; the history used ends at {format_period(sf.frame.index[-1], sf.grain)}" if sf.n else ""
    if tail.basis == "date":
        return f"{period} is incomplete ({tail.reason}) and was set aside{ends}."
    return f"{period} looks incomplete ({tail.reason}) and was set aside as a likely partial load{ends}."


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
    data_end: Optional[Any] = None,
) -> SeriesFrame:
    """Turn aggregate rows into a regular, complete :class:`SeriesFrame`.

    The calendar spans ``[request.start, request.end)`` when both are given,
    otherwise the observed span. Periods without a source row get
    ``observed=False`` and the fill the aggregate warrants.

    ``extra_values`` are ``(column, additive)`` pairs for further measures that
    ride along on the same calendar (correlation's second series); they land
    in ``frame[column]`` with the same fill rule as the primary.

    ``data_end`` is the newest source timestamp (the span probe's ``max_ts``);
    with it an incomplete trailing period is proven rather than inferred. See
    :func:`detect_partial_tail`.
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
    newest = parse_data_end(data_end)
    if newest is not None:
        # A requested range that runs past the newest source row would add
        # periods with no data yet; those are not zeros and never enter the
        # calendar. The period holding the newest row stays and is judged below.
        newest_period = floor_to_grain(pd.Series([newest]), grain, request.week_start).iloc[0]
        if newest_period < last:
            last = max(newest_period, grouped.index.max())

    calendar = pd.date_range(start=first, end=last, freq=freq, name="ts")
    y = grouped.reindex(calendar)
    observed = y.notna()
    if request.is_additive:
        y = y.fillna(0.0)

    frame = pd.DataFrame({"y": y.astype(float), "observed": observed.astype(bool)}, index=calendar)
    for (name, additive), series in zip(extra_values, extra_grouped.values()):
        col = series.reindex(calendar).astype(float)
        frame[name] = col.fillna(0.0) if additive else col

    partial = detect_partial_tail(frame, grain, freq, request, data_end)
    if partial is not None:
        frame = frame.iloc[:-1]

    filled = int((~frame["observed"]).sum())
    if request.is_additive:
        policy = f"{request.agg.upper()} of an empty {grain} is zero: {filled} of {len(frame)} periods zero-filled"
    else:
        policy = f"{request.agg.upper()} has no value for an empty {grain}: {filled} of {len(frame)} periods left empty"
    if partial is not None:
        policy += f"; {format_period(partial.ts, grain)} set aside as incomplete ({partial.reason})"
    sf = SeriesFrame(frame=frame, grain=grain, freq=freq, request=request, missing_policy=policy,
                     partial_tail=partial)

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
