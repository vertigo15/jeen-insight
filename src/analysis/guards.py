"""Guards: pure functions that decide whether a skill may run on a series.

Every guard returns a :class:`GuardResult` whose ``detail`` names the shortfall
in numbers ("62 of 104 periods"), never adjectives, and whose ``exits`` are
executable: a ``params_patch`` the browser can post straight back, or an
``override`` that runs anyway and marks the result low-confidence.

Two stages exist. The *pre-SQL* guards (catalog + span probe) live with the
planner because they need the catalog; the *post-SQL* guards here run on the
fetched aggregate series and are shared by every runner.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from src.analysis.contracts import GuardExit, GuardResult
from src.analysis.series import SeriesFrame, min_points_for_period

SERIES_LENGTH_MIN = 12
GAP_RATIO_MAX = 0.20
ZERO_SHARE_MAX = 0.50

_COARSER: Dict[str, Optional[str]] = {"day": "week", "week": "month", "month": None}


def _override_exit(what: str = "Run it anyway") -> GuardExit:
    return GuardExit(
        kind="override",
        label=what,
        description="The result will carry a low-confidence flag that travels into pins, exports and history.",
    )


def _coarser_exit(sf: SeriesFrame, *, recommended: bool) -> Optional[GuardExit]:
    coarser = _COARSER.get(sf.grain)
    if not coarser:
        return None
    return GuardExit(
        kind="patch",
        label=f"Use a {coarser}ly grain instead" if coarser != "day" else "Use a daily grain instead",
        description=f"Rolls the same measure up to one point per {coarser}.",
        params_patch={"series": {"grain": coarser}},
        recommended=recommended,
    )


def _wider_window_exit(sf: SeriesFrame, needed: int) -> GuardExit:
    return GuardExit(
        kind="patch",
        label=f"Look back {needed} {sf.period_label(plural=True)}",
        description="Widens the analysed window so the model has more history.",
        params_patch={"window": int(needed)},
    )


# ── Individual guards ─────────────────────────────────────────────────────────


def series_length(sf: SeriesFrame) -> GuardResult:
    n = sf.n
    ok = n >= SERIES_LENGTH_MIN
    exits: List[GuardExit] = []
    if not ok:
        exits.append(_wider_window_exit(sf, max(SERIES_LENGTH_MIN * 2, 24)))
        finer = {"week": "day", "month": "week"}.get(sf.grain)
        if finer:
            exits.append(GuardExit(
                kind="patch",
                label=f"Use a {finer}ly grain instead" if finer != "day" else "Use a daily grain instead",
                description=f"One point per {finer} gives the model more periods to learn from.",
                params_patch={"series": {"grain": finer}},
                recommended=True,
            ))
        exits.append(_override_exit())
    return GuardResult(
        name="series_length",
        passed=ok,
        detail=f"{n} of {SERIES_LENGTH_MIN} {sf.period_label(plural=True)} needed",
        observed=float(n),
        required=float(SERIES_LENGTH_MIN),
        exits=exits,
    )


def gap_ratio(sf: SeriesFrame) -> GuardResult:
    ratio = sf.gap_ratio
    ok = ratio <= GAP_RATIO_MAX
    exits: List[GuardExit] = []
    if not ok:
        coarser = _coarser_exit(sf, recommended=True)
        if coarser:
            exits.append(coarser)
        exits.append(_override_exit())
    return GuardResult(
        name="gap_ratio",
        passed=ok,
        detail=(
            f"{sf.periods_filled} of {sf.n} {sf.period_label(plural=True)} had no rows "
            f"({ratio:.0%}); the limit is {GAP_RATIO_MAX:.0%}"
        ),
        observed=round(ratio, 4),
        required=GAP_RATIO_MAX,
        exits=exits,
    )


def min_history(sf: SeriesFrame) -> GuardResult:
    """Informational unless there is not even a candidate cycle to test.

    A seasonal term needs more than two full cycles (``2·period + 1`` points).
    When history is shorter the engines fit without seasonality — that is a
    note on the result, not a refusal.
    """
    n = sf.n
    if not sf.candidate_periods:
        # Work out what the grain *would* need so the detail still carries numbers.
        expected = {"day": 7, "week": 52, "month": 12}.get(sf.grain)
        needed = min_points_for_period(expected) if expected else SERIES_LENGTH_MIN
        return GuardResult(
            name="min_history",
            passed=True,
            detail=(
                f"{n} of {needed} {sf.period_label(plural=True)} for a seasonal term "
                f"(period {expected}); fitting without seasonality"
                if expected else f"{n} {sf.period_label(plural=True)}"
            ),
            observed=float(n),
            required=float(needed),
            overridable=False,
        )
    longest = max(sf.candidate_periods)
    confirmed = ", ".join(str(m) for m in sf.seasonal_periods) or "none confirmed"
    return GuardResult(
        name="min_history",
        passed=True,
        detail=(
            f"{n} {sf.period_label(plural=True)} ≥ {min_points_for_period(longest)} needed for period {longest}; "
            f"seasonal periods: {confirmed}"
        ),
        observed=float(n),
        required=float(min_points_for_period(longest)),
        overridable=False,
    )


def horizon_cap(sf: SeriesFrame) -> int:
    """Longest defensible horizon: a third of the history, and at most two of
    the longest confirmed cycle.

    An incomplete trailing period that was set aside still counts as history
    the window covered: setting it aside is a data-quality step and must not
    flip this guard for the window the user chose."""
    n = sf.n + (1 if sf.partial_tail is not None else 0)
    cap = max(1, n // 3)
    if sf.longest_period:
        cap = min(cap, 2 * sf.longest_period)
    return int(cap)


def max_horizon(sf: SeriesFrame, horizon: int) -> GuardResult:
    cap = horizon_cap(sf)
    ok = horizon <= cap
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(
            kind="patch",
            label=f"Forecast {cap} {sf.period_label(plural=cap != 1)} instead",
            description=f"The longest horizon {sf.n} {sf.period_label(plural=True)} of history can support.",
            params_patch={"horizon": cap},
            recommended=True,
        ))
        exits.append(_wider_window_exit(sf, max(3 * horizon, sf.n + horizon)))
        exits.append(_override_exit(f"Forecast {horizon} anyway"))
    return GuardResult(
        name="max_horizon",
        passed=ok,
        detail=(
            f"{horizon} {sf.period_label(plural=True)} requested; "
            f"{sf.n} {sf.period_label(plural=True)} of history support at most {cap}"
        ),
        observed=float(horizon),
        required=float(cap),
        exits=exits,
    )


def intermittent(sf: SeriesFrame) -> GuardResult:
    share = sf.zero_share
    ok = share <= ZERO_SHARE_MAX
    exits: List[GuardExit] = []
    if not ok:
        coarser = _coarser_exit(sf, recommended=True)
        if coarser:
            exits.append(coarser)
        exits.append(_override_exit())
    return GuardResult(
        name="intermittent",
        passed=ok,
        detail=(
            f"{share:.0%} of {sf.period_label(plural=True)} are exactly zero; "
            f"the limit is {ZERO_SHARE_MAX:.0%}"
        ),
        observed=round(share, 4),
        required=ZERO_SHARE_MAX,
        exits=exits,
    )


# ── Multi-series / contribution / entity guards ───────────────────────────────

SERIES_COUNT_MAX = 12
SLICES_MIN = 2
SLICE_ROWS_MAX = 2000
CARDINALITY_MIN = 50
FEATURE_COUNT_MIN, FEATURE_COUNT_MAX = 2, 8
COHORT_MIN = 2
COHORT_PERIODS_MIN = 2
ARM_COUNT = 2  # v1 compares exactly two arms
GROUP_SIZE_MIN = 30  # per arm, below which the normal approximation is shaky


def series_count(n_groups: int, group_by: Optional[str]) -> GuardResult:
    ok = n_groups <= SERIES_COUNT_MAX
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(
            kind="patch", label="Analyse the total instead of one series per value",
            description=f"Drops the split by {group_by}.", params_patch={"series": {"group_by": None}}, recommended=True,
        ))
        exits.append(_override_exit(f"Run the {SERIES_COUNT_MAX} largest series"))
    return GuardResult(
        name="series_count", passed=ok,
        detail=f"{n_groups} distinct values of {group_by or 'the split column'}; the limit is {SERIES_COUNT_MAX}",
        observed=float(n_groups), required=float(SERIES_COUNT_MAX), exits=exits,
    )


def slices(n_slices: int, n_rows: int, *, dimensions: List[str]) -> GuardResult:
    """Contribution needs at least two slices to decompose a delta, and a bounded table."""
    ok = n_slices >= SLICES_MIN and n_rows <= SLICE_ROWS_MAX
    exits: List[GuardExit] = []
    if n_slices < SLICES_MIN:
        detail = f"{n_slices} slice{'s' if n_slices != 1 else ''} across {', '.join(dimensions)}; at least {SLICES_MIN} needed"
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
    elif n_rows > SLICE_ROWS_MAX:
        detail = f"{n_rows:,} slice rows; the limit is {SLICE_ROWS_MAX:,} — pick a coarser dimension"
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead"))
        exits.append(_override_exit(f"Keep the {SLICE_ROWS_MAX:,} largest slices"))
    else:
        detail = f"{n_slices} slices across {', '.join(dimensions)}"
    return GuardResult(name="slices", passed=ok, detail=detail, observed=float(n_slices),
                       required=float(SLICES_MIN), exits=exits)


def cardinality(n_entities: int, row_cap: int) -> GuardResult:
    ok = CARDINALITY_MIN <= n_entities <= row_cap
    exits: List[GuardExit] = []
    if n_entities < CARDINALITY_MIN:
        detail = f"{n_entities} entities; at least {CARDINALITY_MIN} needed"
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
        exits.append(_override_exit())
    elif n_entities > row_cap:
        detail = f"{n_entities:,} entities; the row cap is {row_cap:,}"
        exits.append(GuardExit(kind="patch", label=f"Analyse the first {row_cap:,} entities", params_patch={},
                               description="The query is capped at row_cap rows, ordered by the entity key.",
                               recommended=True))
    else:
        detail = f"{n_entities:,} entities within the {row_cap:,} row cap"
    return GuardResult(name="cardinality", passed=ok, detail=detail, observed=float(n_entities),
                       required=float(CARDINALITY_MIN), exits=exits)


def feature_count(usable: List[str], requested: List[str], reasons: Optional[Dict[str, str]] = None) -> GuardResult:
    """``reasons`` names why a requested feature was dropped (``"not numeric"``
    or ``"41% filled"``); a feature with no reason is reported as not numeric."""
    n = len(usable)
    ok = FEATURE_COUNT_MIN <= n <= FEATURE_COUNT_MAX
    dropped = [f for f in requested if f not in usable]
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
    detail = f"{n} numeric feature{'s' if n != 1 else ''} usable of {len(requested)} requested"
    if dropped:
        reasons = reasons or {}
        not_numeric = [f for f in dropped if reasons.get(f, "not numeric") == "not numeric"]
        sparse = [f"{f} {reasons[f]}" for f in dropped if f not in not_numeric]
        parts = []
        if not_numeric:
            parts.append(f"not numeric: {', '.join(not_numeric)}")
        if sparse:
            parts.append(f"mostly empty: {', '.join(sparse)}")
        detail += f" ({'; '.join(parts)})"
    return GuardResult(name="feature_count", passed=ok, detail=detail, observed=float(n),
                       required=float(FEATURE_COUNT_MIN), overridable=False, exits=exits)


def cohort_size(n_cohorts: int) -> GuardResult:
    """Cohort retention needs at least two signup cohorts to show a curve."""
    ok = n_cohorts >= COHORT_MIN
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
    return GuardResult(
        name="cohort_size", passed=ok,
        detail=(f"{n_cohorts} signup cohorts" if ok else f"{n_cohorts} signup cohort{'s' if n_cohorts != 1 else ''}; at least {COHORT_MIN} needed"),
        observed=float(n_cohorts), required=float(COHORT_MIN), exits=exits,
    )


def retention_history(n_offsets: int) -> GuardResult:
    """Needs at least two period offsets (0 and 1+) or the curve is a single point."""
    ok = n_offsets >= COHORT_PERIODS_MIN
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
    return GuardResult(
        name="retention_history", passed=ok,
        detail=(f"{n_offsets} period offsets observed" if ok else f"only {n_offsets} period offset; at least {COHORT_PERIODS_MIN} needed for a curve"),
        observed=float(n_offsets), required=float(COHORT_PERIODS_MIN), exits=exits,
    )


def arm_count(n_arms: int) -> GuardResult:
    """v1 tests exactly two arms; a control vs a single treatment."""
    ok = n_arms == ARM_COUNT
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
    return GuardResult(
        name="arm_count", passed=ok, overridable=False,
        detail=(f"{n_arms} arms" if ok else f"{n_arms} arm{'s' if n_arms != 1 else ''}; this test compares exactly {ARM_COUNT}"),
        observed=float(n_arms), required=float(ARM_COUNT), exits=exits,
    )


def group_size(min_arm_n: int) -> GuardResult:
    """Both arms need enough observations for the normal approximation to hold."""
    ok = min_arm_n >= GROUP_SIZE_MIN
    exits: List[GuardExit] = []
    if not ok:
        exits.append(GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True))
        exits.append(_override_exit())
    return GuardResult(
        name="group_size", passed=ok,
        detail=(f"smallest arm has {min_arm_n:,} observations" if ok
                else f"smallest arm has {min_arm_n:,}; at least {GROUP_SIZE_MIN} needed per arm"),
        observed=float(min_arm_n), required=float(GROUP_SIZE_MIN), exits=exits,
    )


# ── Runner ────────────────────────────────────────────────────────────────────

_GUARDS = {
    "series_length": series_length,
    "gap_ratio": gap_ratio,
    "min_history": min_history,
    "intermittent": intermittent,
}


def run_post_sql_guards(
    sf: SeriesFrame,
    *,
    guard_names: List[str],
    horizon: Optional[int] = None,
) -> List[GuardResult]:
    """Evaluate the skill's declared guards in order. Never raises."""
    results: List[GuardResult] = []
    for name in guard_names:
        if name == "max_horizon":
            if horizon is not None:
                results.append(max_horizon(sf, horizon))
            continue
        fn = _GUARDS.get(name)
        if fn is None:
            continue
        results.append(fn(sf))
    return results


def failed(results: List[GuardResult]) -> List[GuardResult]:
    return [g for g in results if not g.passed]
