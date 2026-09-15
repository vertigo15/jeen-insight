"""COHORT_RETENTION engine — tier A. "How many keep coming back?"

Pure aggregation, no model. The SQL returns, per signup cohort, the number of
distinct entities active in each later period plus the cohort's size (the
NULL-period row). This engine turns those counts into retention curves: the
share of each cohort still active 0, 1, 2 … periods after signup, plus a
size-weighted average curve across cohorts.

Only aggregated cohort×period counts ever leave the database, so nothing
row-level is exposed (tier A).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    ChartSpec,
    CohortRetentionParams,
    Egress,
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

ENGINE_NAME = "cohort-retention"


def _pandas_version() -> str:
    try:
        return str(pd.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _offset(cohort: pd.Timestamp, period: pd.Timestamp, grain: str) -> int:
    if grain == "month":
        return (period.year - cohort.year) * 12 + (period.month - cohort.month)
    if grain == "week":
        return int((period.normalize() - cohort.normalize()).days // 7)
    return int((period.normalize() - cohort.normalize()).days)


def run(params: CohortRetentionParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None) -> ResultEnvelope:
    ctx = ctx or RunContext()
    guard_results = guard_results or []
    grain = params.cohort.grain
    max_periods = params.cohort.max_periods

    df = table.copy()
    df["cohort"] = pd.to_datetime(df["cohort"], errors="coerce")
    df["period"] = pd.to_datetime(df["period"], errors="coerce")
    df["active"] = pd.to_numeric(df["active"], errors="coerce").fillna(0)
    df = df.dropna(subset=["cohort"]).reset_index(drop=True)

    # NULL-period rows carry each cohort's size (the retention denominator).
    sizes = {row.cohort: int(row.active) for row in df[df["period"].isna()].itertuples()}
    activity = df[df["period"].notna()].copy()
    activity["offset"] = [_offset(c, p, grain) for c, p in zip(activity["cohort"], activity["period"])]
    activity = activity[(activity["offset"] >= 0) & (activity["offset"] <= max_periods)]

    # Fall back to offset-0 activity as the size when a NULL row is missing.
    for c in activity["cohort"].unique():
        if c not in sizes:
            base = activity[(activity["cohort"] == c) & (activity["offset"] == 0)]["active"]
            sizes[c] = int(base.iloc[0]) if len(base) else int(activity[activity["cohort"] == c]["active"].max())

    rows: List[Dict[str, Any]] = []
    per_cohort_max: Dict[Any, int] = {}
    for c in sorted(activity["cohort"].unique()):
        size = sizes.get(c, 0) or 0
        sub = activity[activity["cohort"] == c].sort_values("offset")
        per_cohort_max[c] = int(sub["offset"].max()) if len(sub) else 0
        clabel = pd.Timestamp(c).date().isoformat()
        for r in sub.itertuples():
            rows.append({
                "cohort": clabel,
                "period_offset": int(r.offset),
                "active": int(r.active),
                "cohort_size": int(size),
                "retention_pct": float(r.active / size) if size else None,
            })

    # Size-weighted average curve: a cohort counts toward offset k only if it is
    # old enough to have reached k (its max observed offset ≥ k).
    max_offset = max((o for o in activity["offset"]), default=0)
    avg_rows: List[Dict[str, Any]] = []
    avg_curve: List[Dict[str, Any]] = []
    for k in range(0, int(max_offset) + 1):
        eligible = [c for c, m in per_cohort_max.items() if m >= k]
        denom = sum(sizes.get(c, 0) for c in eligible)
        numer = float(activity[(activity["offset"] == k) & (activity["cohort"].isin(eligible))]["active"].sum())
        ret = float(numer / denom) if denom else None
        avg_rows.append({"cohort": "All cohorts", "period_offset": k, "active": int(numer),
                         "cohort_size": int(denom), "retention_pct": ret})
        avg_curve.append({"offset": k, "retention": num(ret, 4)})
    rows = avg_rows + rows
    columns = ["cohort", "period_offset", "active", "cohort_size", "retention_pct"]

    n_cohorts = int(activity["cohort"].nunique())
    ret1 = next((c["retention"] for c in avg_curve if c["offset"] == 1), None)
    ret_last = avg_curve[-1]["retention"] if avg_curve else None
    if ret1 is None:
        headline = f"{n_cohorts} {grain}ly cohorts, but no activity one {grain} after signup to measure return."
    else:
        tail = f"; by {grain} {int(max_offset)} it settles at {fmt_pct(ret_last, 0)}" if ret_last is not None and max_offset > 1 else ""
        headline = f"Across {n_cohorts} {grain}ly cohorts, {fmt_pct(ret1, 0)} return one {grain} after signup{tail}."

    validation = make_validation("retention@1", ret1, basis=f"size-weighted over {n_cohorts} cohorts",
                                 extras={"retention_last": ret_last if ret_last is not None else float("nan"),
                                         "max_offset": float(max_offset)})
    caveats = [
        "Retention is the share of a cohort active in a later period, counting distinct entities; "
        "it is descriptive, not a forecast.",
        "Recent cohorts have fewer observed periods, so the tail of the average curve rests on the "
        "older cohorts only.",
    ]
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the curve as indicative.")

    facts: Dict[str, Any] = {
        "skill": "cohort_retention", "table": params.cohort.table, "grain": grain,
        "n_cohorts": n_cohorts, "max_offset": int(max_offset),
        "retention_at_1": num(ret1, 4), "retention_at_last": num(ret_last, 4),
        "avg_curve": avg_curve,
    }
    details = ModelDetails(
        method_used=f"Cohort retention by {grain} (distinct active entities ÷ cohort size)",
        candidates=[],
        params_used={"grain": grain, "max_periods": max_periods, "entity_key": params.cohort.entity_key,
                     "cohort_date": params.cohort.cohort_date, "activity_date": params.cohort.activity_date,
                     "table": params.cohort.table},
        notes=[],
    )
    chart = ChartSpec(chart_type="line", x_column="period_offset", y_columns=["retention_pct"],
                      series_column="cohort", x_label=f"{grain}s since signup", y_label="retention")
    return ResultEnvelope(
        skill="cohort_retention", params=params.model_dump(mode="json"), method_used=details.method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="A", rows_sent_to_model=int(len(table)), columns=["cohort", "period", "active"]),
        engine=engine_info(ENGINE_NAME, _pandas_version(), modules=["cohort_retention", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(table)),
                                    note=f"{len(table):,} aggregated cohort×period rows"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )
