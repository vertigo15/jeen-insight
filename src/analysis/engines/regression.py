"""REGRESSION engine — tier B. "What is the relationship between X and Y?"

An ordinary-least-squares linear model (statsmodels ``OLS``) of a numeric
target on 2–8 candidate features. Unlike ``driver_analysis`` — which fits a
black-box gradient-boosted model and ranks features by held-out importance —
this returns the *equation*: a signed coefficient per feature with a standard
error, t-statistic, p-value and 95% confidence interval, plus a standardized
effect size (beta) so features on different scales can be compared. Fit quality
is reported two ways: an honest held-out R² (fit on a train split, scored on a
held-out split) and the in-sample adjusted R². This answers "how does each
feature relate to the target", which is associative, not causal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    Egress,
    GuardResult,
    ModelDetails,
    RegressionParams,
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

ENGINE_NAME = "statsmodels-ols"


def _statsmodels_version() -> str:
    try:
        import statsmodels

        return str(statsmodels.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def run(params: RegressionParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None, *, features: Optional[List[str]] = None) -> ResultEnvelope:
    import statsmodels.api as sm
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
    notes: List[str] = []
    if dropped:
        notes.append(f"{dropped:,} rows with a missing value were left out.")

    X = df[features].to_numpy(dtype=float)
    y = df["target"].to_numpy(dtype=float)
    target_label = params.entity.target or "target"

    # Drop features with no variance — a constant column has no coefficient to fit.
    x_std = X.std(axis=0)
    keep = [i for i in range(len(features)) if x_std[i] > 0]
    if len(keep) < len(features):
        for i in range(len(features)):
            if i not in keep:
                notes.append(f"{features[i]} is constant across rows and was dropped.")
    features = [features[i] for i in keep]
    X = X[:, keep]
    x_std = X.std(axis=0)
    y_std = float(y.std())

    # Honest generalization score: fit on a train split, score on a held-out split.
    r2_holdout = float("nan")
    n_test = 0
    if n >= 8 and len(features) >= 1:
        X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=params.holdout, random_state=0)
        n_test = int(len(y_te))
        if n_test > 1:
            m_tr = sm.OLS(y_tr, sm.add_constant(X_tr, has_constant="add")).fit()
            pred = m_tr.predict(sm.add_constant(X_te, has_constant="add"))
            ss_res = float(np.sum((y_te - pred) ** 2))
            ss_tot = float(np.sum((y_te - y_te.mean()) ** 2)) or float("nan")
            r2_holdout = 1.0 - ss_res / ss_tot if ss_tot and not np.isnan(ss_tot) else float("nan")

    # Reported coefficients come from a fit on all rows: the most representative
    # estimate of the relationship, with confidence intervals and p-values.
    model = sm.OLS(y, sm.add_constant(X, has_constant="add")).fit()
    coefs = np.asarray(model.params, dtype=float)
    bse = np.asarray(model.bse, dtype=float)
    tvals = np.asarray(model.tvalues, dtype=float)
    pvals = np.asarray(model.pvalues, dtype=float)
    conf = np.asarray(model.conf_int(alpha=0.05), dtype=float)  # shape (k+1, 2), row 0 = const
    r2_full = float(model.rsquared)
    adj_r2 = float(model.rsquared_adj)
    f_pvalue = float(model.f_pvalue) if model.f_pvalue is not None else float("nan")
    intercept = float(coefs[0])

    # Variance inflation flags multicollinearity, which makes individual
    # coefficients unstable even when the model as a whole fits well.
    vifs = _vif(X)

    rows: List[Dict[str, Any]] = []
    for j, f in enumerate(features):
        i = j + 1  # offset past the intercept
        std_coef = float(coefs[i] * (x_std[j] / y_std)) if y_std > 0 else None
        rows.append({
            "feature": f,
            "coefficient": float(coefs[i]),
            "std_coef": std_coef,
            "std_err": float(bse[i]),
            "t": float(tvals[i]),
            "p_value": float(pvals[i]),
            "ci_low": float(conf[i, 0]),
            "ci_high": float(conf[i, 1]),
            "significant": bool(pvals[i] < 0.05),
            "direction": "positive" if coefs[i] > 0 else "negative" if coefs[i] < 0 else "flat",
            "vif": vifs[j],
        })
    rows.sort(key=lambda r: -(abs(r["std_coef"]) if r["std_coef"] is not None else abs(r["coefficient"])))
    columns = ["feature", "coefficient", "std_coef", "std_err", "t", "p_value",
               "ci_low", "ci_high", "significant", "direction", "vif"]

    top = rows[0] if rows else None
    sig = [r for r in rows if r["significant"]]
    score = r2_holdout if not np.isnan(r2_holdout) else r2_full
    if np.isnan(score) or score <= 0.05:
        headline = (f"The features do not usefully explain {target_label} "
                    f"(R² {score:.2f}); no reliable linear relationship.")
    elif top is not None:
        headline = (f"{target_label} is {fmt_pct(score, 0)} explained; {top['feature']} has the strongest "
                    f"effect ({top['direction']}, {'significant' if top['significant'] else 'not significant'} "
                    f"at p<0.05). {len(sig)} of {len(rows)} features are significant.")
    else:
        headline = f"No usable features remained to model {target_label}."

    basis = (f"held-out split of {fmt_pct(params.holdout, 0)} ({n_test:,} rows)"
             if not np.isnan(r2_holdout) else f"in-sample fit on {n:,} rows")
    validation = make_validation(
        "R2", score, basis=basis,
        extras={"r2_full": r2_full, "adj_r2": adj_r2, "f_pvalue": f_pvalue,
                "train_rows": float(n - n_test) if n_test else float(n)},
    )
    validation.band = "good" if score >= 0.5 else "fair" if score >= 0.2 else "poor"

    caveats = [
        "Associative, not causal: a coefficient is the average change in the target per unit of a "
        "feature holding the others fixed — not proof that the feature causes the change.",
        "Standardized effect (std_coef) lets you compare features on different scales; the raw "
        "coefficient is in the feature's own units.",
    ]
    high_vif = [r["feature"] for r in rows if isinstance(r["vif"], float) and r["vif"] and r["vif"] >= 10]
    if high_vif:
        caveats.append(f"High multicollinearity (VIF ≥ 10) for {', '.join(high_vif)}; their individual "
                       "coefficients are unstable — read them together, not in isolation.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the estimates as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "regression", "table": params.entity.table, "target": target_label, "features": features,
        "n_rows": n, "r2_holdout": num(r2_holdout, 3), "r2_full": num(r2_full, 3), "adj_r2": num(adj_r2, 3),
        "f_pvalue": num(f_pvalue, 4), "intercept": num(intercept),
        "coefficients": [{k: (num(v) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
    }
    details = ModelDetails(
        method_used="OLS linear regression (statsmodels), standardized effects; held-out R²",
        candidates=[CandidateScore(name="OLS", metric="R2", value=num(score), selected=True),
                    CandidateScore(name="mean baseline", metric="R2", value=0.0, is_baseline=True)],
        params_used={"holdout": params.holdout, "features": features, "target": target_label,
                     "row_cap": params.entity.row_cap},
        notes=notes,
    )
    chart = ChartSpec(chart_type="horizontal_bar", x_column="feature", y_columns=["std_coef"],
                      x_label="feature", y_label="standardized effect")
    return ResultEnvelope(
        skill="regression", params=params.model_dump(mode="json"), method_used=details.method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="B", rows_sent_to_model=n, columns=[*features, "target"]),
        engine=engine_info(ENGINE_NAME, _statsmodels_version(), modules=["regression", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(table)),
                                    note=f"{len(table):,} entity rows read (row cap {params.entity.row_cap:,}); "
                                         f"{dropped} dropped for missing values"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )


def _vif(X: np.ndarray) -> List[Optional[float]]:
    """Variance inflation factor per column. Returns None where it can't be
    computed (single feature, or perfect collinearity giving a non-finite value)."""
    k = X.shape[1]
    if k < 2:
        return [None] * k
    try:
        from statsmodels.stats.outliers_influence import variance_inflation_factor

        design = np.column_stack([np.ones(len(X)), X])
        out: List[Optional[float]] = []
        for j in range(k):
            v = float(variance_inflation_factor(design, j + 1))
            out.append(v if np.isfinite(v) else None)
        return out
    except Exception:  # noqa: BLE001
        return [None] * k
