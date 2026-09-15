"""CLASSIFICATION engine — tier B. "Who is likely to X?" (X is yes/no)

An interpretable logistic-regression model of a *binary* target on 2–8 numeric
features. Coefficients are fitted on standardized features so they are directly
comparable, and reported as **odds ratios per one standard deviation** with a
confidence interval and p-value. Discrimination is a held-out ROC AUC (fit on a
train split, scored on a held-out split); calibration is the Brier score. This
answers "how does each feature relate to the odds of the outcome", which is
associative, not causal.

statsmodels ``Logit`` gives the odds ratios, CIs and p-values; when the data are
perfectly separated (Logit cannot converge) the engine falls back to scikit-learn
``LogisticRegression`` for the coefficients and says so.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    ClassificationParams,
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

ENGINE_NAME = "statsmodels-logit"


def _statsmodels_version() -> str:
    try:
        import statsmodels

        return str(statsmodels.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def run(params: ClassificationParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None, *, features: Optional[List[str]] = None) -> ResultEnvelope:
    import statsmodels.api as sm
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
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

    # Map the two target values to 0/1; the higher value is the positive class.
    classes = sorted(pd.unique(df["target"]))
    pos_value = classes[-1]
    y = (df["target"].to_numpy() == pos_value).astype(int)
    positive_rate = float(y.mean()) if n else float("nan")

    X_raw = df[features].to_numpy(dtype=float)
    x_std = X_raw.std(axis=0)
    keep = [i for i in range(len(features)) if x_std[i] > 0]
    if len(keep) < len(features):
        for i in range(len(features)):
            if i not in keep:
                notes.append(f"{features[i]} is constant across rows and was dropped.")
    features = [features[i] for i in keep]
    X_raw = X_raw[:, keep]
    x_std = X_raw.std(axis=0)
    x_mean = X_raw.mean(axis=0)
    Xz = (X_raw - x_mean) / x_std  # standardized: coefficients are per-SD

    target_label = params.entity.target or "target"

    # Discrimination on a held-out split (sklearn: robust, no convergence issues).
    auc = pr_auc = brier = float("nan")
    n_test = 0
    if n >= 8 and len(features) >= 1:
        Xtr, Xte, ytr, yte = train_test_split(Xz, y, test_size=params.holdout, random_state=0, stratify=y if 0 < positive_rate < 1 else None)
        n_test = int(len(yte))
        if len(np.unique(ytr)) == 2 and len(np.unique(yte)) == 2:
            clf = LogisticRegression(max_iter=1000, C=1e6)
            clf.fit(Xtr, ytr)
            proba = clf.predict_proba(Xte)[:, 1]
            auc = float(roc_auc_score(yte, proba))
            pr_auc = float(average_precision_score(yte, proba))
            brier = float(brier_score_loss(yte, proba))

    # Odds ratios / p-values from a full-data Logit fit; fall back on separation.
    method_used = "Logistic regression (statsmodels Logit), standardized; held-out AUC"
    engine_name = ENGINE_NAME
    engine_version = _statsmodels_version()
    coefs = bse = zvals = pvals = None
    conf = None
    try:
        logit = sm.Logit(y, sm.add_constant(Xz, has_constant="add")).fit(disp=0, maxiter=200)
        coefs = np.asarray(logit.params, dtype=float)
        bse = np.asarray(logit.bse, dtype=float)
        zvals = np.asarray(logit.tvalues, dtype=float)
        pvals = np.asarray(logit.pvalues, dtype=float)
        conf = np.asarray(logit.conf_int(alpha=0.05), dtype=float)
        if not np.all(np.isfinite(coefs)):
            raise ValueError("non-finite coefficients")
    except Exception:  # noqa: BLE001 — separation / non-convergence
        from sklearn.linear_model import LogisticRegression as _LR

        clf = _LR(max_iter=1000, C=1e6)
        clf.fit(Xz, y)
        coefs = np.concatenate([clf.intercept_, clf.coef_.ravel()]).astype(float)
        bse = np.full(len(coefs), np.nan)
        zvals = np.full(len(coefs), np.nan)
        pvals = np.full(len(coefs), np.nan)
        conf = np.full((len(coefs), 2), np.nan)
        method_used = "Logistic regression (scikit-learn; classes were separable, so no p-values)"
        engine_name = "sklearn-logreg"
        from sklearn import __version__ as _skv

        engine_version = str(_skv)
        notes.append("The classes were perfectly (or near-perfectly) separable; coefficients are "
                     "regularized estimates without p-values.")

    intercept = float(coefs[0])
    rows: List[Dict[str, Any]] = []
    for j, f in enumerate(features):
        i = j + 1
        coef_std = float(coefs[i])
        rows.append({
            "feature": f,
            "coef_std": coef_std,                       # log-odds per 1 SD
            "odds_ratio": float(np.exp(coef_std)),      # odds multiplier per 1 SD
            "raw_coef": float(coef_std / x_std[j]),     # log-odds per unit
            "std_err": float(bse[i]) if np.isfinite(bse[i]) else None,
            "z": float(zvals[i]) if np.isfinite(zvals[i]) else None,
            "p_value": float(pvals[i]) if np.isfinite(pvals[i]) else None,
            "or_ci_low": float(np.exp(conf[i, 0])) if np.isfinite(conf[i, 0]) else None,
            "or_ci_high": float(np.exp(conf[i, 1])) if np.isfinite(conf[i, 1]) else None,
            "significant": bool(np.isfinite(pvals[i]) and pvals[i] < 0.05),
            "direction": "increases odds" if coef_std > 0 else "decreases odds" if coef_std < 0 else "no effect",
        })
    rows.sort(key=lambda r: -abs(r["coef_std"]))
    columns = ["feature", "coef_std", "odds_ratio", "raw_coef", "std_err", "z", "p_value",
               "or_ci_low", "or_ci_high", "significant", "direction"]

    top = rows[0] if rows else None
    sig = [r for r in rows if r["significant"]]
    if np.isnan(auc):
        headline = (f"Could not score how well the features predict {target_label}={pos_value} "
                    f"(too few rows or one class missing from the held-out split).")
    elif auc <= 0.55:
        headline = f"The features barely predict {target_label}={pos_value} (held-out AUC {auc:.2f})."
    elif top is not None:
        headline = (f"{target_label}={pos_value} is predicted with AUC {auc:.2f}; {top['feature']} "
                    f"{top['direction']} most (odds ×{top['odds_ratio']:.2f} per SD). "
                    f"{len(sig)} of {len(rows)} features are significant.")
    else:
        headline = f"No usable features remained to model {target_label}."

    basis = f"held-out split of {fmt_pct(params.holdout, 0)} ({n_test:,} rows)" if n_test else f"in-sample on {n:,} rows"
    validation = make_validation(
        "AUC", auc, basis=basis,
        extras={"pr_auc": pr_auc, "brier": brier, "positive_rate": positive_rate,
                "train_rows": float(n - n_test) if n_test else float(n)},
    )
    validation.band = "good" if (not np.isnan(auc) and auc >= 0.8) else "fair" if (not np.isnan(auc) and auc >= 0.65) else "poor"

    caveats = [
        "Associative, not causal: an odds ratio is how the odds of the outcome change with a feature "
        "holding the others fixed — not proof the feature causes the outcome.",
        "Odds ratios are per one standard deviation of the feature, so features on different scales "
        "can be compared directly.",
    ]
    if not np.isnan(positive_rate) and (positive_rate < 0.05 or positive_rate > 0.95):
        caveats.append(f"Classes are imbalanced ({fmt_pct(positive_rate, 1)} positive); AUC is robust to "
                       "this but treat the Brier/calibration with care.")
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the estimates as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "classification", "table": params.entity.table, "target": target_label,
        "positive_class": _json_scalar(pos_value), "classes": [_json_scalar(c) for c in classes],
        "features": features, "n_rows": n, "positive_rate": num(positive_rate, 4),
        "auc": num(auc, 3), "pr_auc": num(pr_auc, 3), "brier": num(brier, 4), "intercept": num(intercept),
        "coefficients": [{k: (num(v) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
    }
    details = ModelDetails(
        method_used=method_used,
        candidates=[CandidateScore(name="logistic regression", metric="AUC", value=num(auc), selected=True),
                    CandidateScore(name="random baseline", metric="AUC", value=0.5, is_baseline=True)],
        params_used={"holdout": params.holdout, "features": features, "target": target_label,
                     "row_cap": params.entity.row_cap},
        notes=notes,
    )
    chart = ChartSpec(chart_type="horizontal_bar", x_column="feature", y_columns=["coef_std"],
                      x_label="feature", y_label="log-odds per SD")
    return ResultEnvelope(
        skill="classification", params=params.model_dump(mode="json"), method_used=method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="B", rows_sent_to_model=n, columns=[*features, "target"]),
        engine=engine_info(engine_name, engine_version, modules=["classification", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(table)),
                                    note=f"{len(table):,} entity rows read (row cap {params.entity.row_cap:,}); "
                                         f"{dropped} dropped for missing values"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )


def _json_scalar(v: Any) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return str(v)
