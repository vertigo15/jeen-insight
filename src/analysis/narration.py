"""ML-aware narration: per-skill deterministic findings + follow-up questions.

Each analysis engine (``src/analysis/engines/*``) already returns a structured
``facts`` dict plus a ``headline`` and ``caveats``. This module turns those
facts into:

  * ``findings`` — key-insight strings grounded in the exact numbers the engine
    computed (never invented), and
  * ``followups`` — clickable follow-up questions that reference the concrete
    entities the analysis surfaced (the flagged period, the top driver, the
    changepoint date, the winning arm, ...).

The functions are pure over plain dicts — no engine / sandbox / LLM / numpy
dependency — so they are cheap to run in the API process and trivial to unit
test. The insights endpoint layers a single grounded LLM *summary* on top using
``facts_for_llm`` + ``llm_hint``; the LLM only writes prose, never numbers.

Design note (efficiency): adding a new skill's tailored treatment is one small
function registered in ``SKILL_NARRATORS`` — it reuses the facts the engine
already emits, so there is no second source of truth and no per-skill prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

MAX_FINDINGS = 5
MAX_FOLLOWUPS = 6


# ── Value formatting (dependency-free mirror of engines.common) ──────────────

def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def fmt_value(value: Any) -> str:
    """Compact business formatting: 1.47M, 640k, 2,840, 0.71."""
    v = _to_float(value)
    if v is None:
        return "n/a"
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


def fmt_pct(value: Any, digits: int = 1) -> str:
    """A ratio (0.31) rendered as a percentage (31%)."""
    v = _to_float(value)
    if v is None:
        return "n/a"
    return f"{v * 100:.{digits}f}%".replace(".0%", "%")


def fmt_signed_pct(value: Any, digits: int = 1) -> str:
    v = _to_float(value)
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v) * 100:.{digits}f}%".replace(".0%", "%")


def _period_word(grain: Any, plural: bool = False) -> str:
    g = str(grain or "period").lower()
    known = {"day", "week", "month", "quarter", "year", "hour"}
    w = g if g in known else "period"
    return f"{w}s" if plural else w


def _measure(facts: Dict[str, Any]) -> str:
    return str(facts.get("measure") or facts.get("target") or "the measure")


# ── Narration container + registry ───────────────────────────────────────────

@dataclass
class Narration:
    findings: List[str] = field(default_factory=list)
    followups: List[str] = field(default_factory=list)
    facts_for_llm: Dict[str, Any] = field(default_factory=dict)
    llm_hint: str = ""


Narrator = Callable[[Dict[str, Any], Dict[str, Any]], Narration]
SKILL_NARRATORS: Dict[str, Narrator] = {}


def _register(skill: str) -> Callable[[Narrator], Narrator]:
    def deco(fn: Narrator) -> Narrator:
        SKILL_NARRATORS[skill] = fn
        return fn
    return deco


def _dedupe_cap(items: List[Any], cap: int) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for raw in items:
        if raw is None:
            continue
        text = str(raw).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _slim(facts: Dict[str, Any], list_cap: int = 5) -> Dict[str, Any]:
    """Trim big list fields so the grounded-summary prompt stays cheap."""
    out: Dict[str, Any] = {}
    for k, v in (facts or {}).items():
        out[k] = v[:list_cap] if isinstance(v, list) else v
    return out


def narrate(analysis: Dict[str, Any]) -> Narration:
    """Build findings + follow-ups for one ML result envelope (dict form).

    ``analysis`` is the persisted/serialized ``ResultEnvelope``: it carries
    ``skill``, ``facts``, ``params``, ``caveats`` and ``headline``. Unknown or
    missing skills fall back to a generic narrator; any narrator error degrades
    to the generic one rather than failing the request.
    """
    analysis = analysis or {}
    facts = analysis.get("facts") or {}
    params = analysis.get("params") or {}
    skill = analysis.get("skill") or facts.get("skill") or ""
    caveats = [c for c in (analysis.get("caveats") or []) if c]

    fn = SKILL_NARRATORS.get(skill, _narrate_generic)
    try:
        result = fn(facts, params)
    except Exception:  # noqa: BLE001 — narration must never break the answer
        result = _narrate_generic(facts, params)

    headline_detail = facts.get("headline_detail")
    primary: List[str] = []
    if headline_detail:
        primary.append(str(headline_detail))
    primary.extend(result.findings)

    # Reserve up to two slots so the engine's caveats (causation / robustness /
    # coverage notes) are not crowded out by a long list of per-point findings.
    reserved = min(len(caveats), 2)
    primary = _dedupe_cap(primary, max(1, MAX_FINDINGS - reserved))
    result.findings = _dedupe_cap(primary + caveats, MAX_FINDINGS)
    result.followups = _dedupe_cap(result.followups, MAX_FOLLOWUPS)
    if not result.facts_for_llm:
        result.facts_for_llm = _slim(facts)
    if not result.llm_hint:
        result.llm_hint = (
            "Summarize the analysis in one or two sentences using only the "
            "provided numbers; do not invent values."
        )
    return result


# ── Time-series skills ───────────────────────────────────────────────────────

@_register("anomaly_detection")
def _narrate_anomaly(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    measure = _measure(facts)
    grain = facts.get("grain")
    n = facts.get("n_points")
    n_flagged = int(facts.get("n_flagged") or 0)
    flagged = [f for f in (facts.get("flagged") or []) if isinstance(f, dict)]
    findings: List[str] = []
    if n_flagged and flagged:
        above = sum(1 for f in flagged if f.get("direction") == "above")
        below = len(flagged) - above
        split = " and ".join(p for p in (f"{above} above" if above else "",
                                         f"{below} below" if below else "") if p)
        total = f" of {n}" if n is not None else ""  # avoid "3 of None months"
        findings.append(
            f"{n_flagged}{total} {_period_word(grain, True)} were flagged"
            + (f" ({split} expectation)." if split else ".")
        )
        for f in flagged[:3]:
            findings.append(
                f"{f.get('period')}: {fmt_value(f.get('actual'))} vs expected "
                f"{fmt_value(f.get('expected'))} ({fmt_signed_pct(f.get('deviation_pct'))})."
            )
    elif n is not None and n_flagged == 0:
        # Only assert "all clear" when we actually know the point count.
        findings.append(f"All {n} {_period_word(grain, True)} sat inside the expected range - no anomalies.")
    # Band coverage vs expected is surfaced by the engine caveat, so it is not
    # repeated here (narrate() appends the caveat when present).

    followups: List[str] = []
    top = flagged[0] if flagged else None
    if top and top.get("period"):
        followups.append(f"Why did {measure} move in {top.get('period')}?")
        followups.append(f"Break {top.get('period')} down by category")
    followups.append("Re-run the anomaly check at a higher sensitivity")
    followups.append(f"Forecast {measure} for the next few {_period_word(grain, True)}")
    if facts.get("seasonal_periods"):
        followups.append(f"Is there a seasonal pattern in {measure}?")

    hint = ("Summarize the anomaly scan: how many points fell outside the expected band "
            "and the single largest deviation. Use only the given numbers.")
    return Narration(findings, followups, _slim(facts), hint)


@_register("forecast")
def _narrate_forecast(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    measure = _measure(facts)
    grain = facts.get("grain")
    horizon = facts.get("horizon")
    end = facts.get("forecast_end") or {}
    findings: List[str] = []
    if end.get("value") is not None:
        findings.append(f"{measure} is projected at {fmt_value(end.get('value'))} by {end.get('period')}.")
        if end.get("lower") is not None and end.get("upper") is not None:
            findings.append(
                f"Interval at {end.get('period')}: {fmt_value(end.get('lower'))}-{fmt_value(end.get('upper'))} "
                f"({fmt_pct(facts.get('interval'), 0)})."
            )
    if facts.get("pct_change_vs_trailing") is not None:
        findings.append(
            f"The {horizon}-{_period_word(grain)} horizon total is {fmt_signed_pct(facts.get('pct_change_vs_trailing'))} "
            f"versus the trailing {horizon} {_period_word(grain, True)} "
            f"({fmt_value(facts.get('trailing_total'))} -> {fmt_value(facts.get('horizon_total'))})."
        )
    if facts.get("method"):
        findings.append(f"Model: {facts.get('method')} (validated by {facts.get('cv_metric') or 'CV'}).")

    followups = [
        f"Extend the forecast further out for {measure}",
        "Widen the prediction interval to 95%",
        f"What is driving the projected change in {measure}?",
        f"Compare the forecast with the same {_period_word(grain, True)} last year",
    ]
    hint = ("State the projected end value and how it compares to recent history. "
            "Use only the given numbers.")
    return Narration(findings, followups, _slim(facts), hint)


@_register("changepoint")
def _narrate_changepoint(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    measure = _measure(facts)
    grain = facts.get("grain")
    n = facts.get("n_points")
    cps = facts.get("changepoints") or []
    findings: List[str] = []
    if not cps:
        findings.append(f"No level shift found across {n} {_period_word(grain, True)}; {measure} holds one regime.")
    else:
        findings.append(f"{len(cps)} level shift(s) detected in {measure} across {n} {_period_word(grain, True)}.")
        for cp in cps[:3]:
            findings.append(
                f"{cp.get('period')}: shifted {cp.get('direction')} "
                f"{fmt_value(cp.get('level_before'))} -> {fmt_value(cp.get('level_after'))} "
                f"({fmt_signed_pct(cp.get('magnitude_pct'))})."
            )
    followups: List[str] = []
    top = cps[0] if cps else None
    if top and top.get("period"):
        followups.append(f"What changed around {top.get('period')}?")
        followups.append(f"Break the {top.get('period')} shift down by segment")
    followups.append(f"Show {measure} before and after the shift")
    followups.append(f"Forecast {measure} from the latest regime")
    hint = "Describe how many level shifts occurred and the largest one. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("seasonality")
def _narrate_seasonality(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    measure = _measure(facts)
    grain = facts.get("grain")
    periods = facts.get("seasonal_periods") or []
    strength = _to_float(facts.get("strength"))
    findings: List[str] = []
    if periods:
        word = "strong" if (strength or 0) >= 0.6 else "moderate" if (strength or 0) >= 0.3 else "weak"
        findings.append(
            f"{measure} has a {word} {periods[0]}-{_period_word(grain)} cycle "
            f"(strength {facts.get('strength')})."
        )
        peak = facts.get("peak") or {}
        trough = facts.get("trough") or {}
        if peak.get("label") or trough.get("label"):
            findings.append(f"Peaks around {peak.get('label')}, troughs around {trough.get('label')}.")
        if facts.get("trend_growth_per_year") is not None:
            findings.append(
                f"Underlying trend: {fmt_signed_pct(facts.get('trend_growth_per_year'))} per year "
                f"after removing seasonality."
            )
    else:
        findings.append(f"No seasonal cycle could be confirmed in {measure}.")
    peak = facts.get("peak") or {}
    followups = [
        f"Forecast {measure} using this seasonality",
        f"Show the deseasonalized trend of {measure}",
    ]
    if peak.get("label"):
        followups.append(f"What drives the {peak.get('label')} peak in {measure}?")
    followups.append(f"Compare {measure} across years")
    hint = "State whether a seasonal cycle exists, its period and strength, and the peak/trough. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("correlation")
def _narrate_correlation(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    a = _measure(facts)
    b = str(facts.get("other_measure") or "the other measure")
    best = facts.get("best") or {}
    r = _to_float(best.get("r"))
    lag = best.get("lag")
    findings: List[str] = []
    if r is None:
        findings.append(f"{a} and {b} show no measurable correlation.")
    else:
        strength = "strong" if abs(r) >= 0.6 else "moderate" if abs(r) >= 0.3 else "weak"
        sign = "positive" if r >= 0 else "negative"
        lag_txt = ""
        if lag not in (None, 0):
            leader = b if lag > 0 else a
            lag_txt = f", with {leader} leading by {abs(int(lag))} {_period_word(facts.get('grain'), abs(int(lag)) != 1)}"
        findings.append(f"{a} and {b} have a {strength} {sign} correlation (r = {r:.2f}){lag_txt}.")
    findings.append("Correlation is not causation; a shared driver can explain both.")

    followups = [
        f"Does {a} lead {b} or the other way around?",
        f"Plot {a} against {b} over time",
        f"What could jointly drive {a} and {b}?",
    ]
    hint = "State the correlation strength, sign and any lead/lag. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("contribution")
def _narrate_contribution(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    measure = _measure(facts)
    findings: List[str] = []
    if facts.get("delta_pct") is not None:
        findings.append(f"{measure} changed {fmt_signed_pct(facts.get('delta_pct'))} ({fmt_value(facts.get('delta'))}) between the two periods.")
    slices = facts.get("top_slices") or []
    for s in slices[:3]:
        findings.append(
            f"{s.get('slice')} ({s.get('dimension')}) accounts for "
            f"{fmt_pct(s.get('share_of_change'))} of the change."
        )
    followups: List[str] = []
    top = slices[0] if slices else None
    if top and top.get("slice"):
        followups.append(f"Why did {top.get('slice')} change?")
        followups.append(f"Show the trend for {top.get('slice')}")
    if facts.get("best_dimension"):
        followups.append(f"Break the change down further within {facts.get('best_dimension')}")
    followups.append(f"Which slices offset the change in {measure}?")
    hint = "State the total change and the biggest contributing slice. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


# ── Entity / cross-section skills ────────────────────────────────────────────

@_register("clustering")
def _narrate_clustering(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    table = facts.get("table") or "entities"
    k = facts.get("k")
    profiles = facts.get("profiles") or []
    findings: List[str] = []
    findings.append(
        f"{k} segments across {fmt_value(facts.get('n_entities'))} {table} "
        f"(silhouette {facts.get('silhouette')})."
    )
    ordered = sorted(profiles, key=lambda p: _to_float(p.get("share")) or 0, reverse=True)
    for p in ordered[:2]:
        findings.append(f"Segment {p.get('cluster')} holds {fmt_pct(p.get('share'))} of {table}.")
    followups: List[str] = []
    if ordered:
        top = ordered[0]
        followups.append(f"What distinguishes segment {top.get('cluster')}?")
        followups.append(f"Name segment {top.get('cluster')}")
        followups.append(f"Show the {table} in segment {top.get('cluster')}")
    followups.append(f"Re-run the clustering with a different number of segments")
    hint = "State how many segments were found, their separation, and the largest segment's share. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


def _top_features(facts: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    return facts.get(key) or []


@_register("driver_analysis")
def _narrate_driver(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    target = str(facts.get("target") or "the target")
    findings: List[str] = []
    if facts.get("r2_holdout") is not None:
        findings.append(f"{target} is {fmt_pct(facts.get('r2_holdout'), 0)} explained by the features (held-out).")
    contributors = _top_features(facts, "contributors")
    for c in contributors[:3]:
        share = c.get("importance_share")
        findings.append(
            f"{c.get('feature')}: {fmt_pct(share, 0)} of the importance, {c.get('direction')} relationship."
            if share is not None else
            f"{c.get('feature')}: {c.get('direction')} relationship with {target}."
        )
    followups: List[str] = []
    top = contributors[0] if contributors else None
    if top and top.get("feature"):
        followups.append(f"How does {top.get('feature')} affect {target}?")
        followups.append(f"Show {target} by {top.get('feature')}")
    followups.append(f"Which features least affect {target}?")
    hint = "State how much the features explain the target and the single most important driver. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("regression")
def _narrate_regression(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    target = str(facts.get("target") or "the target")
    coefs = _top_features(facts, "coefficients")
    n_sig = sum(1 for c in coefs if c.get("significant"))
    findings: List[str] = []
    if facts.get("r2_holdout") is not None:
        findings.append(
            f"{target}: R^2 {facts.get('r2_holdout')} on held-out data; "
            f"{n_sig} of {len(coefs)} features are significant (p<0.05)."
        )
    for c in coefs[:2]:
        findings.append(
            f"{c.get('feature')}: {c.get('direction')} effect, "
            f"{'significant' if c.get('significant') else 'not significant'}."
        )
    followups: List[str] = []
    top = coefs[0] if coefs else None
    if top and top.get("feature"):
        followups.append(f"How does {top.get('feature')} affect {target}?")
    followups.append(f"Predict {target} for a specific scenario")
    followups.append(f"Which features have no significant effect on {target}?")
    hint = "State the model fit (R^2) and the strongest significant feature. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("classification")
def _narrate_classification(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    target = str(facts.get("target") or "the target")
    pos = facts.get("positive_class")
    coefs = _top_features(facts, "coefficients")
    findings: List[str] = []
    if facts.get("auc") is not None:
        findings.append(
            f"{target}={pos} is predicted with AUC {facts.get('auc')} "
            f"(base rate {fmt_pct(facts.get('positive_rate'), 0)})."
        )
    for c in coefs[:2]:
        odds = _to_float(c.get("odds_ratio"))
        findings.append(
            f"{c.get('feature')}: odds x{odds:.2f} per SD, {c.get('direction')}."
            if odds is not None else
            f"{c.get('feature')}: {c.get('direction')} relationship with {target}={pos}."
        )
    followups: List[str] = []
    top = coefs[0] if coefs else None
    if top and top.get("feature"):
        followups.append(f"How does {top.get('feature')} change the odds of {target}={pos}?")
    followups.append(f"Which records are most likely {target}={pos}?")
    followups.append(f"What lowers the odds of {target}={pos}?")
    hint = "State the AUC and the feature that moves the odds most. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("cohort_retention")
def _narrate_cohort(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    grain = facts.get("grain")
    findings: List[str] = []
    if facts.get("retention_at_1") is not None:
        findings.append(
            f"{fmt_pct(facts.get('retention_at_1'), 0)} return one {_period_word(grain)} after signup "
            f"across {facts.get('n_cohorts')} cohorts."
        )
    if facts.get("retention_at_last") is not None and facts.get("max_offset"):
        findings.append(
            f"By {_period_word(grain)} {facts.get('max_offset')} retention settles at "
            f"{fmt_pct(facts.get('retention_at_last'), 0)}."
        )
    followups = [
        "Compare retention across signup cohorts",
        "Which cohort retains best?",
        f"Break retention down by segment",
    ]
    hint = "State period-1 retention and where the curve settles. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


@_register("experiment_test")
def _narrate_experiment(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    binary = facts.get("outcome_type") == "binary"
    control = str(facts.get("control") or "control")
    treatment = str(facts.get("treatment") or "treatment")

    def est(v: Any) -> str:
        return fmt_pct(v) if binary else fmt_value(v)

    findings: List[str] = []
    p_value = _to_float(facts.get("p_value"))
    sig = facts.get("significant")
    findings.append(
        f"{treatment} {est(facts.get('estimate_treatment'))} vs {control} {est(facts.get('estimate_control'))}: "
        f"{fmt_signed_pct(facts.get('rel_lift'))} lift, p={p_value if p_value is None else round(p_value, 3)} "
        f"({'significant' if sig else 'not significant'})."
    )
    if facts.get("diff_ci_low") is not None and facts.get("diff_ci_high") is not None:
        findings.append(
            f"Difference CI: {est(facts.get('diff_ci_low'))} to {est(facts.get('diff_ci_high'))} "
            f"({fmt_pct(facts.get('confidence'), 0)})."
        )
    findings.append(
        f"Samples: {fmt_value(facts.get('n_treatment'))} in {treatment}, "
        f"{fmt_value(facts.get('n_control'))} in {control}."
    )
    followups = [
        f"Segment the lift of {treatment} by group",
        f"Is the {treatment} effect consistent across regions?",
        "How large a sample would a decisive result need?",
    ]
    hint = "State which arm won, the lift, and whether it is statistically significant. Use only the given numbers."
    return Narration(findings, followups, _slim(facts), hint)


# ── Generic fallback ─────────────────────────────────────────────────────────

def _narrate_generic(facts: Dict[str, Any], params: Dict[str, Any]) -> Narration:
    findings: List[str] = []
    measure = facts.get("measure") or facts.get("target")
    if measure:
        findings.append(f"Analysis of {measure} completed.")
    return Narration(findings, [], _slim(facts),
                     "Summarize the analysis result in one sentence using only the provided numbers.")
