"""DRIVER_ANALYSIS engine — tier B. "What predicts X?"

A gradient-boosted regressor (scikit-learn ``HistGradientBoostingRegressor``)
fitted on a train split and scored on a held-out split (R²). Attribution is
*permutation importance on the held-out split*: how much worse the model
gets when one feature is shuffled. Direction comes from the Spearman rank
correlation of each feature with the target. This answers "which features
predict the target", which is not the same question as "why did the target
change" — that one is ``contribution``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    DriverAnalysisParams,
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

ENGINE_NAME = "scikit-learn-hgb"


def _sklearn_version() -> str:
    try:
        import sklearn

        return str(sklearn.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


_ENGINE_LABELS = {"hgb": "HistGradientBoosting", "xgboost": "XGBoost", "lightgbm": "LightGBM"}


def _build_estimator(key: str):
    """Return a fresh regressor for ``key`` or ``None`` if its library is absent."""
    if key == "hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.06, early_stopping=True, random_state=0)
    if key == "xgboost":
        try:
            from xgboost import XGBRegressor
        except Exception:  # noqa: BLE001
            return None
        return XGBRegressor(n_estimators=300, learning_rate=0.06, max_depth=4, subsample=0.9,
                            random_state=0, verbosity=0, n_jobs=1, tree_method="hist")
    if key == "lightgbm":
        try:
            from lightgbm import LGBMRegressor
        except Exception:  # noqa: BLE001
            return None
        return LGBMRegressor(n_estimators=300, learning_rate=0.06, random_state=0, verbose=-1, n_jobs=1)
    return None


def _select_model(method, X_tr, y_tr, X_te, y_te, r2_score):
    """Fit the requested gradient-boosting engine(s) and pick one.

    ``auto`` fits every installed engine and keeps the best held-out R²; a named
    engine that is not installed falls back to ``hgb`` with a note. Returns
    ``(fitted_model, engine_label, [(label, held_out_r2), ...], note)``."""
    wanted = ["hgb", "xgboost", "lightgbm"] if method == "auto" else [method]
    fitted: list[tuple[str, Any, float]] = []
    note = ""
    for key in wanted:
        est = _build_estimator(key)
        if est is None:
            if method != "auto":
                note = f"{_ENGINE_LABELS.get(key, key)} is not installed in this image; used HistGradientBoosting instead."
            continue
        est.fit(X_tr, y_tr)
        score = float(r2_score(y_te, est.predict(X_te))) if len(y_te) > 1 else float("nan")
        fitted.append((_ENGINE_LABELS[key], est, score))
    if not fitted:  # requested engine missing, or method != auto and unavailable → hgb
        hgb = _build_estimator("hgb")
        hgb.fit(X_tr, y_tr)
        score = float(r2_score(y_te, hgb.predict(X_te))) if len(y_te) > 1 else float("nan")
        fitted.append((_ENGINE_LABELS["hgb"], hgb, score))
    # auto keeps the best score; a named engine keeps the (single) one it fitted.
    best = max(fitted, key=lambda t: (t[2] if t[2] == t[2] else float("-inf"))) if method == "auto" else fitted[0]
    candidates = [(name, score) for name, _, score in sorted(fitted, key=lambda t: -(t[2] if t[2] == t[2] else float("-inf")))]
    return best[1], best[0], candidates, note


def run(params: DriverAnalysisParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None, *, features: Optional[List[str]] = None) -> ResultEnvelope:
    from scipy import stats
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import r2_score
    from sklearn.model_selection import train_test_split

    ctx = ctx or RunContext()
    guard_results = guard_results or []
    features = features or [f for f in params.entity.features if f in table.columns]
    df = table[[*features, "target"]].copy()
    for c in [*features, "target"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    dropped = int(df.isna().any(axis=1).sum())
    df = df.dropna().reset_index(drop=True)
    n = len(df)
    X = df[features].to_numpy(dtype=float)
    y = df["target"].to_numpy(dtype=float)
    notes: List[str] = []
    if dropped:
        notes.append(f"{dropped:,} rows with a missing value were left out.")

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=params.holdout, random_state=0)
    model, model_name, model_candidates, method_note = _select_model(
        params.method, X_tr, y_tr, X_te, y_te, r2_score,
    )
    if method_note:
        notes.append(method_note)
    r2 = float(r2_score(y_te, model.predict(X_te))) if len(y_te) > 1 else float("nan")
    baseline_r2 = 0.0  # predicting the mean
    perm = permutation_importance(model, X_te, y_te, n_repeats=10, random_state=0, scoring="r2")
    importances = np.maximum(perm.importances_mean, 0.0)
    total = float(importances.sum()) or 1.0

    contributors: List[Dict[str, Any]] = []
    for i, f in enumerate(features):
        rho = stats.spearmanr(X[:, i], y).correlation if np.std(X[:, i]) > 0 else float("nan")
        contributors.append({
            "feature": f, "importance": float(importances[i]), "importance_share": float(importances[i] / total),
            "importance_std": float(perm.importances_std[i]),
            "direction": "positive" if (rho or 0) > 0.05 else "negative" if (rho or 0) < -0.05 else "mixed",
            "spearman": float(rho) if rho is not None and not np.isnan(rho) else None,
        })
    contributors.sort(key=lambda c: -c["importance"])
    rows = [{k: v for k, v in c.items()} for c in contributors]
    columns = ["feature", "importance", "importance_share", "importance_std", "direction", "spearman"]

    top = contributors[0] if contributors else None
    target_label = params.entity.target or "target"
    if np.isnan(r2) or r2 <= 0.05:
        headline = f"{', '.join(features)} do not usefully predict {target_label} (held-out R² {r2:.2f})."
    else:
        headline = (f"{target_label} is {fmt_pct(r2, 0)} explained by the features; {top['feature']} matters most "
                    f"({fmt_pct(top['importance_share'], 0)} of the importance, {top['direction']} relationship).")
    validation = make_validation("R2", r2, basis=f"held-out split of {fmt_pct(params.holdout, 0)} ({len(y_te):,} rows)",
                                 extras={"baseline_r2": baseline_r2, "train_rows": float(len(y_tr))})
    validation.band = "good" if r2 >= 0.5 else "fair" if r2 >= 0.2 else "poor"
    caveats = [
        "Predictive, not causal: a feature can rank high because it moves with the true driver.",
        "Importance is measured on rows the model never saw; a share of 0 means shuffling that feature did not hurt.",
    ]
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the ranking as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "driver_analysis", "table": params.entity.table, "target": target_label, "features": features,
        "n_rows": n, "r2_holdout": num(r2, 3), "contributors": [{k: (num(v) if isinstance(v, float) else v) for k, v in c.items()} for c in contributors],
    }
    method_used = f"{model_name} + permutation importance (held-out)"
    candidates = [CandidateScore(name=name, metric="R2", value=num(score), selected=(name == model_name))
                  for name, score in model_candidates]
    candidates.append(CandidateScore(name="mean baseline", metric="R2", value=0.0, is_baseline=True))
    details = ModelDetails(
        method_used=method_used,
        candidates=candidates,
        params_used={"holdout": params.holdout, "features": features, "target": target_label, "row_cap": params.entity.row_cap,
                     "method": params.method, "engine": model_name, "iterations": int(getattr(model, "n_iter_", 0))},
        notes=notes,
    )
    chart = ChartSpec(chart_type="horizontal_bar", x_column="feature", y_columns=["importance_share"],
                      x_label="feature", y_label="share of importance")
    return ResultEnvelope(
        skill="driver_analysis", params=params.model_dump(mode="json"), method_used=details.method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="B", rows_sent_to_model=n, columns=[*features, "target"]),
        engine=engine_info(ENGINE_NAME, _sklearn_version(), modules=["driver_analysis", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(table)), note=f"{len(table):,} entity rows read (row cap {params.entity.row_cap:,}); {dropped} dropped for missing values"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )
