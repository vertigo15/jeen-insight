"""EXPERIMENT_TEST engine — tier A. "Did B beat A, and is it real?"

A frequentist two-arm comparison built entirely from per-arm summary
statistics (count, first and second moment), so no row-level data is needed.

* **binary** outcomes (a 0/1 conversion): a two-proportion z-test. The p-value
  uses the pooled-variance null; the confidence interval on the difference uses
  the unpooled Wald variance (the standard pairing).
* **continuous** outcomes (a numeric metric per row): Welch's t-test, which does
  not assume equal variances between the arms.

The result reports the point estimate per arm, the absolute difference and
relative lift versus a *stated* control, a confidence interval and the p-value.
Significance is decided at ``1 - confidence``. A statistical test, not a model.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from scipy import stats

from src.analysis.contracts import (
    ChartSpec,
    Egress,
    ExperimentTestParams,
    GuardResult,
    ModelDetails,
    ResultEnvelope,
)
from src.analysis.engines.common import (
    RunContext,
    engine_info,
    fmt_pct,
    make_validation,
    num,
    rows_json_safe,
    table_provenance,
)

ENGINE_NAME = "scipy-experiment"
_CONTROL_HINTS = ("control", "baseline", "a", "0", "off", "c", "ctrl", "holdout")
NOT_CAUSAL = (
    "A/B results are only trustworthy if assignment was random and the arms differ "
    "only by the treatment; this test cannot detect a biased split."
)


def _scipy_version() -> str:
    try:
        import scipy  # noqa: PLC0415

        return str(scipy.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _pick_control(arms: List[str], stated: Optional[str]) -> str:
    if stated:
        for a in arms:
            if str(a).strip().lower() == str(stated).strip().lower():
                return a
    for a in arms:
        if str(a).strip().lower() in _CONTROL_HINTS:
            return a
    return sorted(arms, key=lambda x: str(x))[0]


def _fmt_estimate(value: Optional[float], binary: bool) -> str:
    if value is None:
        return "n/a"
    return fmt_pct(value, 1) if binary else f"{value:,.2f}"


def run(params: ExperimentTestParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    ctx = ctx or RunContext()
    guard_results = guard_results or []
    exp = params.experiment
    binary = exp.outcome_type == "binary"
    alpha = 1.0 - params.confidence

    df = table.copy()
    df["arm"] = df["arm"].astype(str)
    for c in ("n", "sum_x", "sum_x2"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["n"]).reset_index(drop=True)

    stat: Dict[str, Dict[str, float]] = {}
    for r in df.itertuples():
        n = float(r.n)
        mean = float(r.sum_x) / n if n else float("nan")
        if binary:
            var = mean * (1.0 - mean)  # Bernoulli variance of the rate
        else:
            var = (float(r.sum_x2) - n * mean * mean) / (n - 1.0) if n > 1 else float("nan")
        stat[str(r.arm)] = {"n": n, "mean": mean, "var": max(var, 0.0), "conv": float(r.sum_x)}

    arms = list(stat.keys())
    control = _pick_control(arms, exp.control)
    treatment = next((a for a in arms if a != control), control)
    c, t = stat[control], stat[treatment]

    diff = t["mean"] - c["mean"]
    rel_lift = (diff / c["mean"]) if c["mean"] not in (0.0, None) else None

    if binary:
        test_name = "Two-proportion z-test"
        n_c, n_t, x_c, x_t = c["n"], t["n"], c["conv"], t["conv"]
        p_pool = (x_c + x_t) / (n_c + n_t) if (n_c + n_t) else float("nan")
        se_pool = math.sqrt(p_pool * (1 - p_pool) * (1 / n_c + 1 / n_t)) if 0 < p_pool < 1 else 0.0
        statistic = diff / se_pool if se_pool else float("nan")
        p_value = float(2 * stats.norm.sf(abs(statistic))) if se_pool else float("nan")
        se_diff = math.sqrt(c["mean"] * (1 - c["mean"]) / n_c + t["mean"] * (1 - t["mean"]) / n_t)
        crit = float(stats.norm.ppf(1 - alpha / 2))
    else:
        test_name = "Welch's t-test (unequal variances)"
        n_c, n_t = c["n"], t["n"]
        res = stats.ttest_ind_from_stats(
            mean1=t["mean"], std1=math.sqrt(t["var"]), nobs1=int(n_t),
            mean2=c["mean"], std2=math.sqrt(c["var"]), nobs2=int(n_c), equal_var=False,
        )
        statistic, p_value = float(res.statistic), float(res.pvalue)
        se_diff = math.sqrt(t["var"] / n_t + c["var"] / n_c) if (n_t and n_c) else float("nan")
        # Welch–Satterthwaite degrees of freedom.
        num_df = (t["var"] / n_t + c["var"] / n_c) ** 2
        den_df = ((t["var"] / n_t) ** 2 / (n_t - 1) if n_t > 1 else 0) + ((c["var"] / n_c) ** 2 / (n_c - 1) if n_c > 1 else 0)
        dof = num_df / den_df if den_df else max(n_t + n_c - 2, 1)
        crit = float(stats.t.ppf(1 - alpha / 2, dof))

    ci_low = diff - crit * se_diff if se_diff == se_diff else None  # noqa: PLR0124 (NaN check)
    ci_high = diff + crit * se_diff if se_diff == se_diff else None
    significant = bool(p_value == p_value and p_value < alpha)

    def _arm_ci(s: Dict[str, float]) -> Tuple[Optional[float], Optional[float]]:
        if binary:
            se = math.sqrt(s["mean"] * (1 - s["mean"]) / s["n"]) if s["n"] else 0.0
            z = float(stats.norm.ppf(1 - alpha / 2))
            return s["mean"] - z * se, s["mean"] + z * se
        if s["n"] > 1:
            se = math.sqrt(s["var"] / s["n"])
            z = float(stats.t.ppf(1 - alpha / 2, s["n"] - 1))
            return s["mean"] - z * se, s["mean"] + z * se
        return None, None

    rows: List[Dict[str, Any]] = []
    for arm in [control, treatment]:
        s = stat[arm]
        lo, hi = _arm_ci(s)
        rows.append({
            "arm": arm, "n": int(s["n"]), "estimate": num(s["mean"], 6),
            "ci_low": num(lo, 6), "ci_high": num(hi, 6), "is_control": arm == control,
        })
    columns = ["arm", "n", "estimate", "ci_low", "ci_high", "is_control"]

    unit = "pp" if binary else ""
    diff_disp = f"{diff * 100:+.1f}pp" if binary else f"{diff:+,.2f}"
    lift_disp = f"{rel_lift * 100:+.0f}%" if rel_lift is not None else "n/a"
    verb = "converts at" if binary else "averages"
    headline = (
        f"{treatment} {verb} {_fmt_estimate(t['mean'], binary)} vs {_fmt_estimate(c['mean'], binary)} "
        f"for {control}: {diff_disp} ({lift_disp}), p={p_value:.3f} — "
        f"{'significant' if significant else 'not significant'} at {fmt_pct(params.confidence, 0)}."
    )

    validation = make_validation(
        "p-value", num(p_value, 4), basis=f"{test_name}, {fmt_pct(params.confidence, 0)} confidence",
        extras={"rel_lift": rel_lift if rel_lift is not None else float("nan"),
                "abs_diff": num(diff, 6) if diff == diff else float("nan"),
                "statistic": num(statistic, 4) if statistic == statistic else float("nan")},
    )
    caveats = [
        NOT_CAUSAL,
        "A significant p-value is not the same as a meaningful effect — read the interval on the difference.",
    ]
    if binary and min(c["conv"], t["conv"], c["n"] - c["conv"], t["n"] - t["conv"]) < 10:
        caveats.append("Some cells have fewer than 10 outcomes, so the normal approximation is rough; treat the p-value as indicative.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the result as indicative.")

    facts: Dict[str, Any] = {
        "skill": "experiment_test", "table": exp.table, "outcome_type": exp.outcome_type,
        "test": test_name, "control": control, "treatment": treatment,
        "n_control": int(c["n"]), "n_treatment": int(t["n"]),
        "estimate_control": num(c["mean"], 6), "estimate_treatment": num(t["mean"], 6),
        "abs_diff": num(diff, 6), "rel_lift": num(rel_lift, 6),
        "diff_ci_low": num(ci_low, 6), "diff_ci_high": num(ci_high, 6),
        "p_value": num(p_value, 6), "statistic": num(statistic, 6),
        "confidence": params.confidence, "significant": significant,
    }
    details = ModelDetails(
        method_used=test_name, candidates=[],
        params_used={"outcome_type": exp.outcome_type, "group_column": exp.group_column,
                     "outcome_column": exp.outcome_column, "control": control, "confidence": params.confidence,
                     "table": exp.table},
        notes=[],
    )
    chart = ChartSpec(chart_type="bar", x_column="arm", y_columns=["estimate"],
                      x_label="arm", y_label=("conversion rate" if binary else "mean outcome"))
    return ResultEnvelope(
        skill="experiment_test", params=params.model_dump(mode="json"), method_used=test_name,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=int(len(table)), columns=["arm", "n", "sum_x", "sum_x2"]),
        engine=engine_info(ENGINE_NAME, _scipy_version(), modules=["experiment_test", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(table)),
                                    note=f"{len(table):,} per-arm summary rows"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )
