"""Grain arithmetic shared by the guards and the advisor.

Dependency-free (no pandas): the advisor runs in the API process on every ML
answer, and the guards' grain-changing exits need the same conversion, so the
two read one definition.
"""

from __future__ import annotations

from typing import Dict, Optional

COARSER: Dict[str, Optional[str]] = {"day": "week", "week": "month", "month": None}
FINER: Dict[str, Optional[str]] = {"week": "day", "month": "week", "day": None}

# Calendar days per period, for converting counts between grains.
DAYS_PER_PERIOD: Dict[str, float] = {"day": 1.0, "week": 7.0, "month": 365.25 / 12}
# ``ForecastParams.horizon`` / ``*Params.window`` bounds; a converted count must land inside them.
HORIZON_BOUNDS = (1, 104)
WINDOW_BOUNDS = (12, 1500)


def convert_periods(count: Optional[int], from_grain: str, to_grain: str,
                    bounds: Optional[tuple] = None) -> Optional[int]:
    """The same span of time counted in periods of another grain.

    ``horizon`` and ``window`` both count periods of ``series.grain``, so a
    patch that changes the grain and leaves them alone silently turns "8 weeks"
    into "8 months". Rounded to the nearest whole period, never below one,
    clamped to ``bounds`` when given.
    """
    if count is None:
        return None
    src = DAYS_PER_PERIOD.get(from_grain)
    dst = DAYS_PER_PERIOD.get(to_grain)
    if not src or not dst:
        return int(count)
    value = max(1, int(round(int(count) * src / dst)))
    if bounds:
        lo, hi = bounds
        value = min(max(value, int(lo)), int(hi))
    return value


def grain_patch(from_grain: str, to_grain: str, *, horizon: Optional[int] = None,
                window: Optional[int] = None, window_bounds: Optional[tuple] = None) -> Dict[str, object]:
    """A ``params_patch`` that changes the grain and keeps the horizon and the
    look-back covering the same stretch of calendar.

    ``window_bounds`` are the *skill's* bounds (seasonality needs at least 24
    periods, the others 12); a converted count is clamped into them so the
    patch always validates.
    """
    patch: Dict[str, object] = {"series": {"grain": to_grain}}
    if horizon is not None:
        patch["horizon"] = convert_periods(horizon, from_grain, to_grain, HORIZON_BOUNDS)
    if window is not None:
        patch["window"] = convert_periods(window, from_grain, to_grain, window_bounds or WINDOW_BOUNDS)
    return patch


def grain_label(grain: str) -> str:
    return "Use a daily grain instead" if grain == "day" else f"Use a {grain}ly grain instead"
