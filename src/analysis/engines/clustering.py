"""CLUSTERING engine — tier B.

Standardised features → k-means (k chosen by silhouette over 2..8 unless
given) or HDBSCAN. The engine returns cluster ids, per-cluster profiles in the
original units and the silhouette; it names nothing. Naming the segments from
their centroids is the narration node's job — unnamed clusters are useless to
a business user, and a model has no business inventing names.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.analysis.contracts import (
    CandidateScore,
    ChartSpec,
    ClusteringParams,
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

ENGINE_NAME = "scikit-learn"
SILHOUETTE_SAMPLE = 4000
K_RANGE = range(2, 9)


def _sklearn_version() -> str:
    try:
        import sklearn

        return str(sklearn.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def run(params: ClusteringParams, table: pd.DataFrame, ctx: Optional[RunContext] = None,
        guard_results: Optional[List[GuardResult]] = None, *, features: Optional[List[str]] = None) -> ResultEnvelope:
    """``table``: entity_key + the usable numeric feature columns (already filtered by the guards)."""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    ctx = ctx or RunContext()
    guard_results = guard_results or []
    features = features or [f for f in params.entity.features if f in table.columns]
    df = table[["entity_key", *features]].copy()
    for f in features:
        df[f] = pd.to_numeric(df[f], errors="coerce")
    dropped = int(df[features].isna().any(axis=1).sum())
    df = df.dropna(subset=features).reset_index(drop=True)
    n = len(df)
    X = StandardScaler().fit_transform(df[features].to_numpy(dtype=float))
    rng = np.random.default_rng(0)
    sample = rng.choice(n, size=min(n, SILHOUETTE_SAMPLE), replace=False) if n > SILHOUETTE_SAMPLE else None
    notes: List[str] = []
    if dropped:
        notes.append(f"{dropped:,} entities with a missing feature value were left out.")

    candidates: List[CandidateScore] = []
    labels: Optional[np.ndarray] = None
    method_used = ""
    silhouette = float("nan")
    k_used = 0

    if params.method == "hdbscan":
        from sklearn.cluster import HDBSCAN

        model = HDBSCAN(min_cluster_size=max(15, n // 50))
        labels = model.fit_predict(X)
        k_used = int(len(set(labels)) - (1 if -1 in labels else 0))
        mask = labels >= 0
        if k_used >= 2 and mask.sum() > k_used:
            silhouette = float(silhouette_score(X[mask], labels[mask], sample_size=min(mask.sum(), SILHOUETTE_SAMPLE), random_state=0))
        method_used = f"HDBSCAN (min_cluster_size={max(15, n // 50)})"
        candidates.append(CandidateScore(name=method_used, metric="silhouette", value=num(silhouette), selected=True))
        noise = int((labels < 0).sum())
        if noise:
            notes.append(f"{noise:,} entities ({fmt_pct(noise / n)}) did not fit any cluster and are labelled -1.")
    else:
        ks = [params.k] if params.k else list(K_RANGE)
        best = None
        for k in ks:
            if k >= n:
                continue
            km = KMeans(n_clusters=k, n_init=4, random_state=0).fit(X)
            score = float(silhouette_score(X, km.labels_, sample_size=len(sample) if sample is not None else None, random_state=0))
            candidates.append(CandidateScore(name=f"k-means k={k}", metric="silhouette", value=num(score)))
            if best is None or score > best[0]:
                best = (score, k, km.labels_)
        if best is None:
            raise ValueError("not enough entities to form two clusters")
        silhouette, k_used, labels = best
        for c in candidates:
            c.selected = c.name == f"k-means k={k_used}"
        method_used = f"k-means, k={k_used} ({'given' if params.k else 'chosen by silhouette'})"

    df["cluster"] = labels.astype(int)
    profiles: List[Dict[str, Any]] = []
    for cid, part in df.groupby("cluster", sort=True):
        centroid = {f: num(float(part[f].mean())) for f in features}
        overall = {f: float(df[f].mean()) for f in features}
        # Which features set this cluster apart (largest standardised distance from the overall mean).
        stds = {f: float(df[f].std() or 1.0) for f in features}
        distinct = sorted(features, key=lambda f: -abs((centroid[f] or 0) - overall[f]) / stds[f])[:2]
        profiles.append({
            "cluster": int(cid), "n": int(len(part)), "share": num(len(part) / n, 4), "centroid": centroid,
            "distinguishing": [{"feature": f, "value": centroid[f], "vs_mean_pct": num(((centroid[f] or 0) - overall[f]) / abs(overall[f]) if overall[f] else None)}
                               for f in distinct],
        })
    profiles.sort(key=lambda p: -p["n"])

    rows = df.to_dict(orient="records")
    columns = ["entity_key", "cluster", *features]
    largest, smallest = profiles[0], profiles[-1]
    headline = (f"{k_used} segments across {n:,} {params.entity.table} entities; the largest holds {fmt_pct(largest['share'])} "
                f"and the smallest {fmt_pct(smallest['share'])} (silhouette {silhouette:.2f}).")
    validation = make_validation("silhouette", silhouette, basis=f"silhouette on {'a sample of ' + str(SILHOUETTE_SAMPLE) if sample is not None else 'all'} entities")
    validation.band = "good" if silhouette >= 0.5 else "fair" if silhouette >= 0.25 else "poor"
    caveats = [
        "Cluster numbers are arbitrary labels; the segment names in the summary are descriptions of the centroids, not facts in the data.",
        f"Features were standardised before clustering; {', '.join(features)} count equally.",
    ]
    if ctx.low_confidence:
        caveats.append("A guard was overridden for this run; treat the segmentation as indicative.")
    caveats.extend(notes)

    facts: Dict[str, Any] = {
        "skill": "clustering", "table": params.entity.table, "entity_key": params.entity.entity_key,
        "features": features, "n_entities": n, "k": k_used, "silhouette": num(silhouette, 3), "profiles": profiles,
        "method": method_used,
    }
    details = ModelDetails(method_used=method_used, candidates=candidates,
                           params_used={"k": params.k, "method": params.method, "features": features, "row_cap": params.entity.row_cap},
                           notes=notes)
    chart = ChartSpec(chart_type="scatter", x_column=features[0], y_columns=[features[1]], series_column="cluster",
                      x_label=features[0], y_label=features[1])
    return ResultEnvelope(
        skill="clustering", params=params.model_dump(mode="json"), method_used=method_used,
        columns=columns, rows=rows_json_safe(rows), chart_spec=chart, validation=validation,
        guard_results=guard_results, low_confidence=ctx.low_confidence,
        egress=Egress(tier="B", rows_sent_to_model=n, columns=["entity_key", *features]),
        engine=engine_info(ENGINE_NAME, _sklearn_version(), modules=["clustering", "common"], runner=ctx.runner),
        provenance=table_provenance(ctx, rows_in=int(len(table)), note=f"{len(table):,} entity rows read (row cap {params.entity.row_cap:,}); {dropped} dropped for missing features"),
        details=details, facts=facts, headline=headline, caveats=caveats,
    )
