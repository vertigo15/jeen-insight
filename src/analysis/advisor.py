"""Adjustments to try: executable follow-ups for a finished analysis.

A completed result already says, in numbers, where it is weak — a poor
validation band, a band that under-covers, no seasonal term because the
history was too short, a baseline that no model could beat, a guard that was
overridden. This module turns those numbers into a short, ranked list of
``params_patch`` dicts the browser can post straight to ``/api/analysis/rerun``
(the same allowlisted patch the "Edit setup" card and guard exits use), so a
disappointing answer leads somewhere with one click.

Rules, not a model: every suggestion is a deterministic function of the
envelope, and every patch is validated with :func:`merge_params_patch` before
it is offered — anything unknown, denied, out of bounds, or a no-op against
the run's own parameters is dropped rather than shown. Labels are returned as
a ``code`` plus ``args`` so the browser renders them in the user's language.

Dependency-free on purpose (no pandas, no ``series.py``): it runs in the API
process on every ML answer and in unit tests without the analytics stack.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from src.analysis.contracts import get_skill, merge_params_patch
from src.analysis.grains import COARSER as _COARSER
from src.analysis.grains import WINDOW_BOUNDS, grain_patch

MAX_ADJUSTMENTS = 4
# Below this many held-out points a coverage figure cannot tell 62% from 80%.
COVERAGE_MIN_N = 16
# The band is called under-covering when it misses its level by more than this.
COVERAGE_SHORTFALL = 0.10
# Periods needed before a seasonal term of ``m`` may be tested (mirrors
# ``series.min_points_for_period`` without importing pandas).
_SEASON_PERIOD = {"day": 7, "week": 52, "month": 12}
# Daily data needs 731 points to test a yearly season — beyond the sandbox
# budget — so the daily advice is a weekly grain, not a wider window.
_YEARLY_DAILY_POINTS = 2 * 365 + 1


def _min_points_for_period(m: int) -> int:
    return 2 * int(m) + 1


def _adjustment(code: str, patch: Dict[str, Any], *, recommended: bool = False, **args: Any) -> Dict[str, Any]:
    return {"code": code, "args": args, "params_patch": patch, "recommended": bool(recommended)}


def _canonical(params: Dict[str, Any]) -> str:
    """Params with the probe-filled range removed, for no-op detection."""
    trimmed = json.loads(json.dumps(params, default=str))
    for key in ("series", "entity", "cohort", "experiment"):
        nested = trimmed.get(key)
        if isinstance(nested, dict):
            nested.pop("start", None)
            nested.pop("end", None)
    return json.dumps(trimmed, sort_keys=True)


def _validate(skill: str, base: Dict[str, Any], patch: Dict[str, Any]) -> bool:
    """True when the patch validates and changes something the engine reads."""
    if not patch:
        return False
    try:
        merged = merge_params_patch(skill, base, patch).model_dump(mode="json")
    except (ValidationError, KeyError, TypeError, ValueError):
        return False
    return _canonical(merged) != _canonical(base)


def _window_was_the_limit(facts: Dict[str, Any], params: Dict[str, Any]) -> bool:
    """The engine saw as many periods as the window asked for, so more history
    may exist; when it saw fewer, the source ran out and a wider window would
    re-run the same series and offer the same chip again."""
    window = params.get("window")
    n = facts.get("n_points")
    if window is None or n is None:
        return False
    seen = int(n) + (1 if facts.get("partial_tail") else 0)
    return seen >= int(window)


def _forecast_rules(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    facts: Dict[str, Any] = analysis.get("facts") or {}
    params: Dict[str, Any] = analysis.get("params") or {}
    validation: Dict[str, Any] = analysis.get("validation") or {}
    series: Dict[str, Any] = params.get("series") or {}
    grain = str(facts.get("grain") or series.get("grain") or "")
    horizon = params.get("horizon")
    window = params.get("window")
    band = validation.get("band")
    out: List[Dict[str, Any]] = []

    # 1. Poor validation → roll up to a coarser grain (the horizon and the
    #    look-back move with it so the calendar span is unchanged).
    coarser = _COARSER.get(grain)
    if band == "poor" and coarser:
        out.append(_adjustment(
            "coarser_grain", grain_patch(grain, coarser, horizon=horizon, window=window),
            recommended=True, grain=coarser,
        ))

    # 2. No seasonal term because the window could not hold a full cycle (the
    #    season was never *testable*, as opposed to tested and rejected), and
    #    the window (not the source) was the limit → look back far enough.
    period = _SEASON_PERIOD.get(grain)
    n = facts.get("n_points")
    if period and n is not None and not facts.get("seasonal_periods") and _window_was_the_limit(facts, params):
        if grain == "day":
            # The weekly season (7) is testable at 15+ days; what is missing is
            # the yearly one, which needs 731 daily points — beyond the sandbox
            # budget — so advise the weekly grain with enough weeks (105) to
            # test it, not merely the same span in weeks.
            if int(n) < _YEARLY_DAILY_POINTS and coarser:
                patch = grain_patch(grain, coarser, horizon=horizon, window=window)
                weekly_needed = _min_points_for_period(_SEASON_PERIOD["week"])
                patch["window"] = min(max(int(patch.get("window") or 0), weekly_needed), WINDOW_BOUNDS[1])
                out.append(_adjustment("coarser_grain_for_season", patch, grain=coarser))
        elif int(n) < _min_points_for_period(period):
            needed = _min_points_for_period(period)
            wider = max(needed, int(round(int(n) * 1.5)))
            wider = min(max(wider, WINDOW_BOUNDS[0]), WINDOW_BOUNDS[1])
            if window is None or wider > int(window):
                out.append(_adjustment(
                    "wider_window_for_season", {"window": wider},
                    recommended=band in ("poor", "fair"), window=wider, unit=grain,
                ))

    # 3. The band under-covers its level on enough held-out points → more
    #    history so the interval can be calibrated (never a different level:
    #    asking for 95% is a different question, not a fix).
    cov = validation.get("coverage")
    cov_n = validation.get("coverage_n") or 0
    level = facts.get("interval") or params.get("interval")
    if (cov is not None and level is not None and int(cov_n) >= COVERAGE_MIN_N
            and float(cov) < float(level) - COVERAGE_SHORTFALL and _window_was_the_limit(facts, params)
            and window is not None):
        wider = min(max(int(round(int(window) * 1.5)), WINDOW_BOUNDS[0]), WINDOW_BOUNDS[1])
        if wider > int(window):
            out.append(_adjustment(
                "wider_window_for_band", {"window": wider},
                window=wider, unit=grain, coverage=round(float(cov) * 100), level=round(float(level) * 100),
            ))

    # 4. No shortlisted model beat the baseline in auto mode → Theta is the
    #    one method outside the shortlist; say so rather than promise a gain.
    selection = facts.get("selection") or {}
    if (selection.get("mode") == "auto" and selection.get("baseline_won") and selection.get("validated")
            and params.get("method") == "auto"):
        out.append(_adjustment("try_theta", {"method": "theta"}, baseline=facts.get("baseline") or "baseline"))

    return out


def _guard_replay(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Exits of guards that were overridden: the executable ``patch`` kind only,
    once per distinct patch (pre- and post-SQL runs, and per-series copies,
    repeat the same guard)."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for guard in analysis.get("guard_results") or []:
        if not isinstance(guard, dict) or guard.get("passed"):
            continue
        name = str(guard.get("name") or "")
        base_name = name.split("[", 1)[0]
        for exit_ in guard.get("exits") or []:
            if not isinstance(exit_, dict) or exit_.get("kind") != "patch":
                continue
            patch = exit_.get("params_patch") or {}
            if not patch:
                continue
            key = json.dumps(patch, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            out.append(_adjustment(
                "guard_exit", patch, recommended=bool(exit_.get("recommended")),
                guard=base_name, label=str(exit_.get("label") or ""), description=str(exit_.get("description") or ""),
            ))
    return out


def suggest_adjustments(analysis: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ranked, validated adjustments for one serialized ``ResultEnvelope``.

    Empty for skills that cannot be re-run with a patch (cohort, experiment),
    for multi-series forecasts (their facts sit per series), and whenever no
    rule fires. Never raises.
    """
    analysis = analysis or {}
    skill = str(analysis.get("skill") or (analysis.get("facts") or {}).get("skill") or "")
    params: Dict[str, Any] = analysis.get("params") or {}
    try:
        spec = get_skill(skill)
    except KeyError:
        return []
    if spec.family not in ("series", "entity"):
        return []
    if not (params.get("series") or params.get("entity")):
        return []

    candidates: List[Dict[str, Any]] = []
    try:
        facts = analysis.get("facts") or {}
        multi_series = bool(facts.get("series")) or bool((params.get("series") or {}).get("group_by"))
        if skill == "forecast" and not multi_series:
            candidates.extend(_forecast_rules(analysis))
        candidates.extend(_guard_replay(analysis))
    except Exception:  # noqa: BLE001 — advice must never break the answer
        return []

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in candidates:
        patch = item["params_patch"]
        key = json.dumps(patch, sort_keys=True, default=str)
        if key in seen or not _validate(skill, params, patch):
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda a: (not a["recommended"]))
    return out[:MAX_ADJUSTMENTS]
