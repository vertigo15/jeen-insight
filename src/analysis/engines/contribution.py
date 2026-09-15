"""CONTRIBUTION engine — tier A. "Why did revenue drop?"

Arithmetic, not a model. For each dimension the before/after totals per slice
give a delta and a *share of change*; Adtributor-style ranking adds a
surprise score (Jensen–Shannon divergence between the before and after mix)
so a slice that grew its share matters even when its delta is small.

Dimension ranking (Adtributor's two criteria, made explicit): the winner is
the dimension whose top ``top_n`` slices explain the largest share of the
total delta (explanatory power); ties within 5 points go to the dimension
whose surprise is *more concentrated* in those same top slices (a change that
sits in a few slices is a sharper story than one smeared across many). The
composite is recorded per dimension in ``facts.dimension_scores.score``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    ContributionParams,
    Egress,
    GuardResult,
    ModelDetails,
    ResultEnvelope,
)
from src.analysis.engines.common import (
    RunContext,
    engine_info,
    fmt_pct,
    fmt_value,
    make_validation,
    num,
    pct_change,
    rows_json_safe,
    table_provenance,
)

ENGINE_NAME = "adtributor-arithmetic"


def _js_surprise(p: float, q: float) -> float:
    """Pointwise Jensen–Shannon contribution for one slice's share before (p) and after (q)."""
    m = (p + q) / 2.0
    out = 0.0
    if p > 0 and m > 0:
        out += 0.5 * p * math.log(p / m)
    if q > 0 and m > 0:
        out += 0.5 * q * math.log(q / m)
    return out


def run(params: ContributionParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    """``table`` columns: dimension, slice, period ('before'|'after'), value."""
    ctx = ctx or RunContext()
    guard_results = guard_results or []
    df = table.copy()
    df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0.0)
    df["slice"] = df["slice"].fillna("(null)").astype(str)
    df = df[df["period"].isin(["before", "after"])]

    pivot = df.pivot_table(index=["dimension", "slice"], columns="period", values="value", aggfunc="sum", fill_value=0.0)
    for col in ("before", "after"):
        if col not in pivot.columns:
            pivot[col] = 0.0
    pivot = pivot.reset_index()
    # Totals from the first dimension (every dimension sums to the same totals).
    first_dim = str(params.dimensions[0])
    base = pivot[pivot["dimension"] == first_dim] if (pivot["dimension"] == first_dim).any() else pivot
    total_before = float(base["before"].sum())
    total_after = float(base["after"].sum())
    delta_total = total_after - total_before

    rows: List[Dict[str, Any]] = []
    dim_scores: List[Dict[str, Any]] = []
    for dim, part in pivot.groupby("dimension", sort=False):
        b, a = part["before"].to_numpy(float), part["after"].to_numpy(float)
        sb, sa = float(b.sum()) or 1e-12, float(a.sum()) or 1e-12
        deltas = a - b
        shares = deltas / delta_total if abs(delta_total) > 1e-12 else np.zeros_like(deltas)
        surprises = np.array([_js_surprise(bi / sb, ai / sa) for bi, ai in zip(b, a)])
        order = np.argsort(-np.abs(deltas))
        for i in order:
            rows.append({
                "dimension": str(dim), "slice": str(part["slice"].iloc[i]),
                "before": float(b[i]), "after": float(a[i]), "delta": float(deltas[i]),
                "delta_pct": pct_change(float(a[i]), float(b[i])),
                "share_of_change": float(shares[i]), "surprise": float(surprises[i]),
            })
        # Adtributor: how much of the delta do the top few slices explain, and how concentrated is the surprise?
        top = order[: params.top_n]
        explained = float(np.abs(shares[top]).sum()) if abs(delta_total) > 1e-12 else 0.0
        total_surprise = float(surprises.sum())
        surprise_in_top = float(surprises[top].sum()) / total_surprise if total_surprise > 1e-12 else 0.0
        # Explanatory power dominates; concentration breaks near-ties (see module docstring).
        composite = round(min(explained, 5.0), 1) + 0.1 * surprise_in_top
        dim_scores.append({"dimension": str(dim), "explained_by_top": num(min(explained, 5.0), 3),
                           "total_surprise": num(total_surprise, 4), "surprise_in_top": num(surprise_in_top, 3),
                           "score": num(composite, 4), "slices": int(len(part))})
    dim_scores.sort(key=lambda d: -(d["score"] or 0))
    best_dim = dim_scores[0]["dimension"] if dim_scores else first_dim
    best_rows = [r for r in rows if r["dimension"] == best_dim][: params.top_n]
    top_share = sum(abs(r["share_of_change"]) for r in best_rows[:2])

    columns = ["dimension", "slice", "before", "after", "delta", "delta_pct", "share_of_change", "surprise"]
    direction = "fell" if delta_total < 0 else "rose" if delta_total > 0 else "did not change"
    label = params.series.measure_label
    if abs(delta_total) <= 1e-12:
        headline = f"{label} did not change between the two periods ({fmt_value(total_before)} both times)."
    else:
        lead = best_rows[0] if best_rows else None
        headline = (f"{label} {direction} {fmt_value(abs(delta_total))} ({fmt_pct(abs(pct_change(total_after, total_before) or 0))}). "
                    + (f"{lead['slice']} ({best_dim}) accounts for {fmt_pct(abs(lead['share_of_change']))} of the change." if lead else ""))

    validation = make_validation("explained", top_share, basis=f"share of the delta explained by the top 2 {best_dim} slices",
                                 extras={"delta_total": delta_total})
    validation.band = "good" if top_share >= 0.6 else "fair" if top_share >= 0.3 else "poor"
    caveats = [
        "This is a decomposition of the change, not a model of its causes: a slice can move because of mix, price or volume alike.",
    ]
    if len(params.dimensions) > 1:
        caveats.append("Each dimension is decomposed separately; the slices of different dimensions overlap and their shares do not add up across dimensions.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the shares as indicative.")

    facts: Dict[str, Any] = {
        "skill": "contribution", "measure": label, "dimensions": list(params.dimensions),
        "before": {"start": str(params.before_start), "end": str(params.before_end), "total": num(total_before)},
        "after": {"start": str(params.after_start), "end": str(params.after_end), "total": num(total_after)},
        "delta": num(delta_total), "delta_pct": pct_change(total_after, total_before),
        "best_dimension": best_dim, "dimension_scores": dim_scores,
        "top_slices": [{k: (num(v) if isinstance(v, float) else v) for k, v in r.items()} for r in best_rows[:5]],
        "n_slices": len(rows),
    }
    details = ModelDetails(
        method_used="delta decomposition + Adtributor surprise",
        candidates=[CandidateScore(name=d["dimension"], metric="surprise", value=d["total_surprise"],
                                   selected=(d["dimension"] == best_dim)) for d in dim_scores],
        params_used={"dimensions": list(params.dimensions), "top_n": params.top_n},
        notes=[],
    )
    chart = ChartSpec(chart_type="horizontal_bar", x_column="slice", y_columns=["delta"],
                      row_filter={"column": "dimension", "value": best_dim}, y_label=f"Δ {label}")
    return ResultEnvelope(
        skill="contribution", params=params.model_dump(mode="json"), method_used=details.method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=int(len(df)), columns=["dimension", "slice", "period", "value"]),
        engine=engine_info(ENGINE_NAME, "1", modules=["contribution", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(df)), note=f"{len(df)} slice×period totals; slices are compared before vs after"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )
