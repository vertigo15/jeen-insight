"""Versioned contracts for ML skills.

Everything downstream (the graph nodes, the answer UI, the sandbox service and
the persisted turn artifact) speaks these shapes. They deliberately import no
ML library so the API process, the sandbox image and the browser-facing models
can all depend on them without pulling in statsforecast.

Bump ``CONTRACT_VERSION`` whenever a field changes meaning or a params shape
gains a field: the sandbox refuses a mismatched version, which is what keeps
an API from sending a field an older sandbox's ``extra="forbid"`` model would
reject. The per-user "don't ask again" consent is scoped to it, so a contract
change re-prompts once, and pending proposals ask to be re-asked.

History: 1 → 2 gave ``ForecastParams`` a ``window`` (the look-back, until then
a private planner hint the cards could neither show nor change). 2 → 3 gave
it ``holidays``: an optional public-holiday calendar (country code) fitted as
a known-future regressor.

Rollout of a bump: pending proposals answer 409 (they expire in 15 min
anyway); "don't ask again" consent is keyed to the version, so every skill
re-prompts once; the API and the analytics sandbox are separate Deployments
and the sandbox refuses a mismatched version, so ship compatible images
together (API first is fine: it fails closed on the sandbox's 409 until the
sandbox follows).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional, Type, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "3"

Grain = Literal["day", "week", "month"]
Agg = Literal["sum", "count", "avg", "min", "max"]
Tier = Literal["A", "B"]
SkillName = Literal[
    "anomaly_detection", "forecast", "changepoint", "seasonality", "correlation",
    "contribution", "clustering", "driver_analysis", "regression", "classification",
    "cohort_retention", "experiment_test",
]
# What shape of data a skill consumes; drives the SQL builder, the series
# preparation and the pre-SQL guards.
#   series        one measure rolled up per period (tier A time series)
#   contribution  before/after totals per dimension slice (tier A, arithmetic)
#   entity        one row per entity with numeric features (tier B)
#   cohort        distinct-entity counts per signup cohort × activity period (tier A)
#   experiment    per-arm outcome summary for an A/B test (tier A)
Family = Literal["series", "contribution", "entity", "cohort", "experiment"]

# Additive aggregates: a period with no source rows genuinely means zero.
# Anything else (avg/min/max) has no defensible fill value and stays NaN.
ADDITIVE_AGGS = frozenset({"sum", "count"})

# Default look-back window per grain when the user gives none (periods).
DEFAULT_WINDOW_PERIODS: Dict[str, int] = {"day": 90, "week": 26, "month": 24}
# Forecasting needs more: a seasonal term is only testable after two full
# cycles plus one point (105 weekly / 25 monthly), and cross-validation then
# needs room to hold the horizon out on top of that. At the generic defaults a
# yearly season could never be confirmed and CV had at most one window.
FORECAST_WINDOW_PERIODS: Dict[str, int] = {"day": 120, "week": 130, "month": 36}
SKILL_WINDOW_PERIODS: Dict[str, Dict[str, int]] = {"forecast": FORECAST_WINDOW_PERIODS}


def default_window_for(skill: str, grain: str) -> int:
    """The look-back a skill starts from when the user names none."""
    return SKILL_WINDOW_PERIODS.get(skill, DEFAULT_WINDOW_PERIODS).get(grain, DEFAULT_WINDOW_PERIODS.get(grain, 26))


class FilterSpec(BaseModel):
    """One grounded predicate carried from ``filter_grounder`` into the SQL builder."""

    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    op: Literal["equals", "in", "gt", "gte", "lt", "lte", "between", "contains"] = "equals"
    value: Any = None


class SeriesRequest(BaseModel):
    """What to roll up: one measure over one date column at one grain.

    ``start``/``end`` form a half-open range ``[start, end)``. When absent the
    planner fills them from the span probe and the skill's default window.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    schema_name: Optional[str] = None
    catalog: Optional[str] = None
    date_column: str
    measure_column: str
    agg: Agg = "sum"
    grain: Grain = "week"
    start: Optional[date] = None
    end: Optional[date] = None
    filters: List[FilterSpec] = Field(default_factory=list)
    # v1 aggregates in the database's own calendar: timestamps are taken as
    # stored (no zone conversion) and weeks start on Monday, which is what
    # DATE_TRUNC('WEEK') yields on every supported dialect. The fields stay in
    # the contract so a future version can widen them without a shape change;
    # any other value is refused rather than silently mislabelled.
    timezone: Literal["UTC"] = "UTC"
    week_start: Literal["monday"] = "monday"
    # Multi-series: one categorical column splitting the measure into several
    # series (one per distinct value, capped by the ``series_count`` guard).
    group_by: Optional[str] = None

    @property
    def measure_label(self) -> str:
        return f"{self.agg.upper()}({self.measure_column})"

    @property
    def is_additive(self) -> bool:
        return self.agg in ADDITIVE_AGGS


class EntityRequest(BaseModel):
    """Tier B: one row per entity with numeric features (and optionally a target).

    Capped and audited: ``row_cap`` bounds what leaves the database, and every
    run records the column list.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    schema_name: Optional[str] = None
    catalog: Optional[str] = None
    entity_key: str
    features: List[str] = Field(min_length=2, max_length=8)
    target: Optional[str] = None
    filters: List[FilterSpec] = Field(default_factory=list)
    row_cap: int = Field(default=50_000, ge=100, le=200_000)


class AnomalyParams(BaseModel):
    """ANOMALY_DETECTION — tier A.

    ``sensitivity`` is the share of history expected to sit inside the band:
    0.95 flags roughly one point in twenty.

    ``method`` picks the expected line the residual band is built around:
    ``auto`` uses MSTL when a season is confirmed and a robust LOWESS trend
    otherwise; ``seasonal`` forces the MSTL seasonal decomposition (falling back
    to a trend only when no period is testable); ``trend`` forces the LOWESS
    trend. All three scale the residuals with a robust (MAD) estimate and set
    the band at the quantile implied by ``sensitivity``. ``sigma3`` is the
    explicit, labelled non-robust alternative: ``±3σ`` on the same residuals,
    ignoring ``sensitivity``.
    """

    model_config = ConfigDict(extra="forbid")

    series: SeriesRequest
    window: Optional[int] = Field(default=None, ge=12, le=1500)
    sensitivity: float = Field(default=0.95, ge=0.80, le=0.99)
    method: Literal["auto", "seasonal", "trend", "sigma3"] = "auto"


class ForecastParams(BaseModel):
    """FORECAST — tier A.

    ``horizon`` is in periods of ``series.grain``; the ``max_horizon`` guard caps
    it at ``min(n/3, 2 × longest seasonal period)``. ``interval`` is the
    prediction-interval level (0.80 → an 80% band). ``method`` ``auto`` runs
    the shortlist and keeps the baseline unless a model beats it; any other
    value is an explicit choice and is returned even when it does not.

    ``holidays`` names a public-holiday calendar (ISO 3166 country code, e.g.
    ``IL``). Its future is known, so it is the one regressor that can be
    fitted honestly: a holiday indicator (daily grain) or a holidays-per-period
    count (weekly / monthly) joins the ARIMA candidates. Business drivers such
    as price or spend are deliberately not accepted — their future values are
    unknown at forecast time.
    """

    model_config = ConfigDict(extra="forbid")

    series: SeriesRequest
    # Look-back in periods of ``series.grain``; the guard fills the forecast
    # default (day 120 / week 130 / month 36) from the span probe and records
    # it here so the cards can show and change it. The series range is what
    # the engine reads.
    window: Optional[int] = Field(default=None, ge=12, le=1500)
    horizon: int = Field(default=8, ge=1, le=104)
    interval: float = Field(default=0.80, ge=0.50, le=0.99)
    method: Literal["auto", "auto_arima", "auto_ets", "theta", "drift", "seasonal_naive"] = "auto"
    holidays: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")


class ChangepointParams(BaseModel):
    """CHANGEPOINT — tier A. PELT on the trend component: when did the level shift?"""

    model_config = ConfigDict(extra="forbid")

    series: SeriesRequest
    window: Optional[int] = Field(default=None, ge=12, le=1500)
    min_segment: int = Field(default=4, ge=2, le=52)
    max_changepoints: int = Field(default=5, ge=1, le=20)


class SeasonalityParams(BaseModel):
    """SEASONALITY — tier A. MSTL decomposition: how strong is the cycle, where are the peaks?"""

    model_config = ConfigDict(extra="forbid")

    series: SeriesRequest
    window: Optional[int] = Field(default=None, ge=24, le=1500)


class CorrelationParams(BaseModel):
    """CORRELATION — tier A. Lagged cross-correlation between two measures on one
    date column. Always rendered with the not-causation line."""

    model_config = ConfigDict(extra="forbid")

    series: SeriesRequest
    other_measure_column: str
    other_agg: Agg = "sum"
    max_lag: int = Field(default=8, ge=0, le=52)
    window: Optional[int] = Field(default=None, ge=12, le=1500)

    @property
    def other_label(self) -> str:
        return f"{self.other_agg.upper()}({self.other_measure_column})"


class ContributionParams(BaseModel):
    """CONTRIBUTION — tier A. "Why did X change": decompose the delta between two
    periods across the slices of each dimension. Arithmetic, not a model."""

    model_config = ConfigDict(extra="forbid")

    series: SeriesRequest  # table, date_column, measure_column, agg, filters (grain unused)
    dimensions: List[str] = Field(min_length=1, max_length=4)
    before_start: Optional[date] = None
    before_end: Optional[date] = None  # exclusive
    after_start: Optional[date] = None
    after_end: Optional[date] = None   # exclusive
    top_n: int = Field(default=10, ge=3, le=50)


class ClusteringParams(BaseModel):
    """CLUSTERING — tier B. k-means / HDBSCAN on standardised features. The engine
    returns cluster ids and profiles; naming them is the narration's job."""

    model_config = ConfigDict(extra="forbid")

    entity: EntityRequest
    k: Optional[int] = Field(default=None, ge=2, le=8)  # None = pick by silhouette
    method: Literal["kmeans", "hdbscan"] = "kmeans"


class DriverAnalysisParams(BaseModel):
    """DRIVER_ANALYSIS — tier B. Gradient-boosted fit of a numeric target on
    candidate features, scored on a held-out split; permutation importance
    for attribution. Predictive, not causal.

    ``method`` picks the gradient-boosting engine: ``hgb`` (scikit-learn
    HistGradientBoosting, always available), ``xgboost`` or ``lightgbm`` (used
    only when installed, else it falls back to ``hgb``), or ``auto`` (fit every
    available engine and keep the one with the best held-out R²)."""

    model_config = ConfigDict(extra="forbid")

    entity: EntityRequest  # entity.target is required
    holdout: float = Field(default=0.2, ge=0.1, le=0.5)
    method: Literal["hgb", "xgboost", "lightgbm", "auto"] = "hgb"

    @field_validator("entity")
    @classmethod
    def _needs_target(cls, v: EntityRequest) -> EntityRequest:
        if not v.target:
            raise ValueError("driver_analysis needs entity.target")
        return v


class RegressionParams(BaseModel):
    """REGRESSION — tier B. Interpretable OLS of a numeric target on 2–8 numeric
    features: signed coefficients, standardized betas, confidence intervals,
    p-values and held-out R². Unlike ``driver_analysis`` (black-box importance)
    it yields the equation. Associative, not causal."""

    model_config = ConfigDict(extra="forbid")

    entity: EntityRequest  # entity.target is required
    holdout: float = Field(default=0.2, ge=0.1, le=0.5)

    @field_validator("entity")
    @classmethod
    def _needs_target(cls, v: EntityRequest) -> EntityRequest:
        if not v.target:
            raise ValueError("regression needs entity.target")
        return v


class ClassificationParams(BaseModel):
    """CLASSIFICATION — tier B. Interpretable logistic regression predicting a
    binary target (e.g. churned yes/no) from 2–8 numeric features: per-feature
    odds ratios with confidence intervals and p-values, held-out ROC AUC and a
    calibration (Brier) score. Associative, not causal. Row-level: capped/audited."""

    model_config = ConfigDict(extra="forbid")

    entity: EntityRequest  # entity.target is required and must be binary
    holdout: float = Field(default=0.2, ge=0.1, le=0.5)

    @field_validator("entity")
    @classmethod
    def _needs_target(cls, v: EntityRequest) -> EntityRequest:
        if not v.target:
            raise ValueError("classification needs entity.target")
        return v


class CohortRequest(BaseModel):
    """Which entities, grouped by when they first appeared, seen active when.

    ``cohort_date`` buckets each entity into a signup cohort; ``activity_date``
    is when they were seen active. Both are date columns on the same table. The
    query returns only aggregated (cohort, period, distinct-entities) counts, so
    no row-level data leaves the database (tier A)."""

    model_config = ConfigDict(extra="forbid")

    table: str
    schema_name: Optional[str] = None
    catalog: Optional[str] = None
    entity_key: str
    cohort_date: str
    activity_date: str
    grain: Literal["day", "week", "month"] = "month"
    max_periods: int = Field(default=12, ge=2, le=36)
    filters: List[FilterSpec] = Field(default_factory=list)


class CohortRetentionParams(BaseModel):
    """COHORT_RETENTION — tier A. Retention curves by signup cohort: the share of
    each cohort still active 1, 2, … periods after signup. Pure aggregation, no
    model; only cohort×period counts leave the database."""

    model_config = ConfigDict(extra="forbid")

    cohort: CohortRequest


class ExperimentRequest(BaseModel):
    """One A/B test: rows split into arms by ``group_column``, each with an
    ``outcome``.     ``binary`` outcomes are 0/1 conversions (two-proportion z-test);
    ``continuous`` outcomes are a numeric metric per row (Welch's t-test). Only
    per-arm summary statistics leave the database — never a row (tier A).

    ``control`` names the baseline arm. When absent the engine looks for a
    conventionally-named control arm (``control``/``baseline``/``a``/``0``) and
    otherwise takes the first arm by label, so "lift" always has a stated
    reference rather than being chosen to look favourable."""

    model_config = ConfigDict(extra="forbid")

    table: str
    schema_name: Optional[str] = None
    catalog: Optional[str] = None
    group_column: str
    outcome_column: str
    outcome_type: Literal["binary", "continuous"] = "binary"
    control: Optional[str] = None
    filters: List[FilterSpec] = Field(default_factory=list)


class ExperimentTestParams(BaseModel):
    """EXPERIMENT_TEST — tier A. Compares two arms of an experiment on one
    outcome and reports the difference, relative lift, a confidence interval and
    a p-value. A statistical test on aggregates, not a fitted model."""

    model_config = ConfigDict(extra="forbid")

    experiment: ExperimentRequest
    confidence: float = Field(default=0.95, ge=0.80, le=0.99)


PARAMS_BY_SKILL: Dict[str, Type[BaseModel]] = {
    "anomaly_detection": AnomalyParams,
    "forecast": ForecastParams,
    "changepoint": ChangepointParams,
    "seasonality": SeasonalityParams,
    "correlation": CorrelationParams,
    "contribution": ContributionParams,
    "clustering": ClusteringParams,
    "driver_analysis": DriverAnalysisParams,
    "regression": RegressionParams,
    "classification": ClassificationParams,
    "cohort_retention": CohortRetentionParams,
    "experiment_test": ExperimentTestParams,
}


# ── Guards ────────────────────────────────────────────────────────────────────


class GuardExit(BaseModel):
    """One executable way out of a guard refusal.

    ``patch`` re-runs with ``params_patch`` merged in; ``override`` runs anyway
    and marks the result low-confidence; the other two leave the skill.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["patch", "override", "switch_skill", "answer_with_sql"] = "patch"
    label: str
    description: str = ""
    params_patch: Dict[str, Any] = Field(default_factory=dict)
    recommended: bool = False


class GuardResult(BaseModel):
    """Outcome of one guard. ``detail`` carries numbers, never adjectives."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str
    observed: Optional[float] = None
    required: Optional[float] = None
    overridable: bool = True
    exits: List[GuardExit] = Field(default_factory=list)


# ── Result envelope ───────────────────────────────────────────────────────────


class Validation(BaseModel):
    """The one number the status strip shows, plus how it was obtained.

    ``metric`` is WAPE for non-negative series with a positive total, else
    MASE (with MAE in ``extras``). MAPE is never used: profit can be zero or
    negative. ``coverage`` is the empirical share of holdout points inside
    the interval, with ``coverage_n`` its sample size.
    """

    model_config = ConfigDict(extra="forbid")

    metric: str
    value: Optional[float] = None
    band: Literal["good", "fair", "poor", "n/a"] = "n/a"
    basis: str = ""
    coverage: Optional[float] = None
    coverage_n: Optional[int] = None
    extras: Dict[str, float] = Field(default_factory=dict)


class ChartSeriesSpec(BaseModel):
    """Role-based series; the browser maps roles to design tokens at render time
    (actual → --text, expected/forecast → --rose, interval → --insight-bg,
    flagged → --err). Never a colour here."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["actual", "expected", "interval", "flagged", "forecast"]
    label: str
    column: Optional[str] = None
    lower_column: Optional[str] = None
    upper_column: Optional[str] = None
    flag_column: Optional[str] = None


class ChartSpec(BaseModel):
    """``band`` is the ML time-series chart (role-based). The other types reuse
    the ordinary chart builder with explicit x/y bindings, so a contribution
    bar or a cluster scatter needs no new renderer."""

    model_config = ConfigDict(extra="forbid")

    chart_type: Literal["band", "line", "bar", "horizontal_bar", "scatter"] = "band"
    x_column: str = "ts"
    series: List[ChartSeriesSpec] = Field(default_factory=list)
    # Non-band bindings.
    y_columns: List[str] = Field(default_factory=list)
    series_column: Optional[str] = None
    # Restrict the charted rows (e.g. one dimension of a contribution table).
    row_filter: Optional[Dict[str, Any]] = None
    # ISO date of the first forecast point, when the chart has one.
    forecast_start: Optional[str] = None
    x_label: str = ""
    y_label: str = ""


class Egress(BaseModel):
    """What left the database and reached the model. Aggregates have already
    left the database, so this is 'rows sent to the model', not 'rows out'."""

    model_config = ConfigDict(extra="forbid")

    tier: Tier
    rows_sent_to_model: int
    columns: List[str]


class EngineInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    module_hash: str = ""
    runner: str = ""


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_ts: str
    sql: Optional[str] = None
    filters_summary: str = ""
    missing_policy: str = ""
    grain: str = ""
    span_start: Optional[str] = None
    span_end: Optional[str] = None
    periods_observed: int = 0
    periods_filled: int = 0


class CandidateScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    metric: str
    value: Optional[float] = None
    selected: bool = False
    is_baseline: bool = False
    note: str = ""


class ModelDetails(BaseModel):
    """Everything the Model details tab shows. Kept separate from ``validation``
    so the strip stays one number and the tab can grow."""

    model_config = ConfigDict(extra="forbid")

    method_used: str
    seasonal_periods: List[int] = Field(default_factory=list)
    candidates: List[CandidateScore] = Field(default_factory=list)
    params_used: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class ResultEnvelope(BaseModel):
    """One envelope, every skill.

    ``columns``/``rows`` are an ordinary result table, which is why Save, Send,
    snapshot, export and the rows renderer work with no changes. ``facts`` is
    the immutable set of numbers the narration node may restate; it never
    computes its own.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTRACT_VERSION
    skill: str
    params: Dict[str, Any]
    method_used: str
    columns: List[str]
    rows: List[Dict[str, Any]]
    chart_spec: ChartSpec
    validation: Validation
    guard_results: List[GuardResult] = Field(default_factory=list)
    low_confidence: bool = False
    egress: Egress
    engine: EngineInfo
    provenance: Provenance
    details: ModelDetails
    facts: Dict[str, Any] = Field(default_factory=dict)
    headline: str = ""
    caveats: List[str] = Field(default_factory=list)

    def artifact_view(self) -> Dict[str, Any]:
        """The persisted/answer-pane slice: everything except the rows table,
        which already lives in the result snapshot."""
        data = self.model_dump(mode="json")
        data.pop("rows", None)
        data.pop("columns", None)
        return data


# ── Skill registry ────────────────────────────────────────────────────────────


class SkillSpec(BaseModel):
    """A registered skill: fixed signature, declared guards, pinned engine.

    Nothing about a skill is inferred at runtime that is not declared here.
    ``routes_on`` are the intent cues the planner's regex pre-filter uses; the
    LLM call only ever chooses among registered names.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    tier: Tier
    family: Family = "series"
    title: str
    description: str
    params_model: Type[BaseModel]
    guards: List[str]
    engine: str
    routes_on: List[str]
    estimated_seconds: int = 4

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in PARAMS_BY_SKILL:
            raise ValueError(f"unknown skill {v!r}")
        return v


SKILLS: Dict[str, SkillSpec] = {
    "anomaly_detection": SkillSpec(
        name="anomaly_detection",
        tier="A",
        title="Anomaly detection",
        description=(
            "Rolls the measure up to one series per period and flags periods that "
            "fall outside the range a seasonal model expected."
        ),
        params_model=AnomalyParams,
        guards=["series_length", "gap_ratio", "min_history", "intermittent"],
        engine="src.analysis.engines.anomaly_detection",
        routes_on=[
            "anomal", "weird", "unusual", "outlier", "spike", "unexpected",
            "abnormal", "is this normal", "out of the ordinary", "irregular",
            "strange", "drop", "surge",
        ],
        estimated_seconds=4,
    ),
    "forecast": SkillSpec(
        name="forecast",
        tier="A",
        title="Forecast",
        description=(
            "Rolls the measure up to one series per period, cross-validates a "
            "shortlist of statistical models and projects the winner forward."
        ),
        params_model=ForecastParams,
        guards=["series_length", "gap_ratio", "min_history", "max_horizon", "intermittent"],
        engine="src.analysis.engines.forecast",
        routes_on=[
            "forecast", "predict", "projection", "project", "what will", "next quarter",
            "next month", "next year", "next week", "run rate", "at this rate",
            "expect", "going to be", "trend into", "by the end of",
        ],
        estimated_seconds=6,
    ),
    "changepoint": SkillSpec(
        name="changepoint",
        tier="A",
        title="Changepoint detection",
        description=(
            "Rolls the measure up per period and finds where its underlying level "
            "shifted (PELT on the de-seasonalised trend)."
        ),
        params_model=ChangepointParams,
        guards=["series_length", "gap_ratio", "min_history", "intermittent"],
        engine="src.analysis.engines.changepoint",
        routes_on=["when did it change", "trend break", "when did it start", "regime", "shift", "structural break",
                   "changepoint", "change point", "inflection"],
        estimated_seconds=4,
    ),
    "seasonality": SkillSpec(
        name="seasonality",
        tier="A",
        title="Seasonality",
        description=(
            "Decomposes the per-period measure into trend, seasonal cycle and remainder; "
            "reports the cycle's strength, peaks and troughs."
        ),
        params_model=SeasonalityParams,
        guards=["series_length", "gap_ratio", "min_history", "intermittent"],
        engine="src.analysis.engines.seasonality",
        routes_on=["seasonal", "seasonality", "cyclical", "peak month", "peak week", "time of year", "cycle"],
        estimated_seconds=4,
    ),
    "correlation": SkillSpec(
        name="correlation",
        tier="A",
        title="Correlation",
        description=(
            "Rolls two measures up per period and measures how they move together, "
            "including at a lag. Reports association only — never causation."
        ),
        params_model=CorrelationParams,
        guards=["series_length", "gap_ratio", "min_history"],
        engine="src.analysis.engines.correlation",
        routes_on=["correlat", "move with", "related to", "relationship between", "does .* drive", "lead indicator",
                   "leading indicator", "lagged", "moves together"],
        estimated_seconds=4,
    ),
    "contribution": SkillSpec(
        name="contribution",
        tier="A",
        family="contribution",
        title="Contribution analysis",
        description=(
            "Explains a change in a measure between two periods by decomposing the "
            "delta across the slices of each dimension. Arithmetic, not a model."
        ),
        params_model=ContributionParams,
        guards=["slices"],
        engine="src.analysis.engines.contribution",
        routes_on=["why did .* (drop|fall|rise|grow|change|increase|decrease)", "what explains the (drop|change|rise|fall)",
                   "what drove the", "breakdown of the change", "contribut", "which .* account for"],
        estimated_seconds=3,
    ),
    "clustering": SkillSpec(
        name="clustering",
        tier="B",
        family="entity",
        title="Clustering",
        description=(
            "Groups entities (customers, products, stores) into segments from numeric "
            "features. Row-level: capped and audited. The segments are named by the narration."
        ),
        params_model=ClusteringParams,
        guards=["cardinality", "feature_count"],
        engine="src.analysis.engines.clustering",
        routes_on=["segment", "cluster", "group (the |our )?(customers|products|stores|users)", "types of customers",
                   "natural cohorts", "personas"],
        estimated_seconds=8,
    ),
    "driver_analysis": SkillSpec(
        name="driver_analysis",
        tier="B",
        family="entity",
        title="Driver analysis",
        description=(
            "Fits a gradient-boosted model of a target on candidate features and ranks "
            "them by held-out importance. Predictive, not causal. Row-level: capped and audited."
        ),
        params_model=DriverAnalysisParams,
        guards=["cardinality", "feature_count"],
        engine="src.analysis.engines.driver_analysis",
        routes_on=["what predicts", "what drives", "drivers of", "which factors", "feature importance",
                   "what influences", "determinants of"],
        estimated_seconds=8,
    ),
    "regression": SkillSpec(
        name="regression",
        tier="B",
        family="entity",
        title="Regression",
        description=(
            "Fits an interpretable linear model (OLS) of a target on candidate features and "
            "reports signed coefficients, standardized effect sizes, confidence intervals, "
            "p-values and R². Associative, not causal. Row-level: capped and audited."
        ),
        params_model=RegressionParams,
        guards=["cardinality", "feature_count"],
        engine="src.analysis.engines.regression",
        routes_on=["linear regression", "regression model", "regress", "coefficients",
                   "effect of .* on", "how much does .* affect", "elasticity", "sensitivity of"],
        estimated_seconds=6,
    ),
    "classification": SkillSpec(
        name="classification",
        tier="B",
        family="entity",
        title="Classification",
        description=(
            "Fits an interpretable logistic-regression model predicting a binary target "
            "(e.g. churn yes/no) from candidate features and reports odds ratios, confidence "
            "intervals, p-values, held-out AUC and calibration. Associative, not causal. "
            "Row-level: capped and audited."
        ),
        params_model=ClassificationParams,
        guards=["cardinality", "feature_count"],
        engine="src.analysis.engines.classification",
        routes_on=["predict (who|which|whether)", "likelihood of", "probability of", "propensity",
                   "churn", "classify", "who is likely to", "risk of"],
        estimated_seconds=6,
    ),
    "cohort_retention": SkillSpec(
        name="cohort_retention",
        tier="A",
        family="cohort",
        title="Cohort retention",
        description=(
            "Groups entities into signup cohorts and measures the share of each cohort still "
            "active 1, 2, … periods later. Pure aggregation: only cohort-by-period counts leave "
            "the database, no row-level data."
        ),
        params_model=CohortRetentionParams,
        guards=["cohort_size", "retention_history"],
        engine="src.analysis.engines.cohort_retention",
        routes_on=["retention", "cohort", "retained", "churn curve", "come back", "repeat (rate|purchase)",
                   "stickiness", "how many .* return"],
        estimated_seconds=5,
    ),
    "experiment_test": SkillSpec(
        name="experiment_test",
        tier="A",
        family="experiment",
        title="A/B test",
        description=(
            "Compares two arms of an experiment on one outcome (a conversion rate or a numeric "
            "metric) and reports the difference, relative lift, confidence interval and p-value. "
            "A statistical test on per-arm aggregates — only the summary leaves the database."
        ),
        params_model=ExperimentTestParams,
        guards=["arm_count", "group_size"],
        engine="src.analysis.engines.experiment_test",
        routes_on=["a/?b test", "experiment", "variant", "treatment (group|arm|vs)", "control group",
                   "statistically significant", "significant (difference|lift|uplift)", "uplift",
                   "did .* (beat|outperform)", "difference between .* groups"],
        estimated_seconds=3,
    ),
}


def get_skill(name: str) -> SkillSpec:
    try:
        return SKILLS[name]
    except KeyError as exc:
        raise KeyError(f"unknown skill {name!r}; registered: {sorted(SKILLS)}") from exc


def parse_params(skill: str, raw: Dict[str, Any]) -> BaseModel:
    """Validate a raw params dict against the skill's model. Raises ValidationError."""
    return get_skill(skill).params_model.model_validate(raw)


def method_options(skill: str) -> List[str]:
    """The selectable ``method`` values of a skill, read from its params model.

    The one source for the ``model`` chip on the confirm card and on a finished
    result's setup card, so the two can never drift apart. Empty for skills
    without a ``method``.
    """
    try:
        field = get_skill(skill).params_model.model_fields.get("method")
    except KeyError:
        return []
    if field is None:
        return []
    return [arg for arg in get_args(field.annotation) if isinstance(arg, str)]


# Human names for the raw enum values the cards expose. The value stays the
# contract; the label is what the user reads. Long explanations belong in a
# chip's ``help``, never in the option text (it has to fit a 190px control).
METHOD_LABELS: Dict[str, str] = {
    "auto": "Auto", "auto_arima": "Auto ARIMA", "auto_ets": "Auto ETS", "theta": "Theta", "drift": "Drift",
    "seasonal_naive": "Seasonal naive", "seasonal": "Seasonal (MSTL)", "trend": "Trend (LOWESS)",
    "sigma3": "3-sigma", "kmeans": "K-means", "hdbscan": "HDBSCAN",
    "hgb": "Gradient boosting", "xgboost": "XGBoost", "lightgbm": "LightGBM",
}
AGG_LABELS: Dict[str, str] = {"sum": "Sum", "count": "Count", "avg": "Average", "min": "Minimum", "max": "Maximum"}
# Optional fields show their "no value" as a word the user can pick again
# (``_UNSET_SENTINELS`` turns it back into None); this is how that word reads.
SENTINEL_LABELS: Dict[str, str] = {"none": "— none —", "auto": "Auto"}


def _nested_request(spec: "SkillSpec") -> "tuple[str, Type[BaseModel]]":
    """The family's nested request object: ``series`` for most tier A skills,
    ``entity`` for tier B, ``cohort`` for cohort retention, ``experiment`` for A/B tests."""
    key = {"entity": "entity", "cohort": "cohort", "experiment": "experiment"}.get(spec.family, "series")
    model = {"entity": EntityRequest, "cohort": CohortRequest, "experiment": ExperimentRequest}.get(spec.family, SeriesRequest)
    return key, model


def field_bounds(skill: str, key: str) -> "tuple[Optional[float], Optional[float]]":
    """``(min, max)`` of a chip's parameter, read from the pydantic field.

    Resolves ``key`` on the skill's params model first, then on the family's
    nested request model (``row_cap`` lives on ``EntityRequest``,
    ``max_periods`` on ``CohortRequest``). Numeric fields yield ``ge``/``le``;
    list fields yield ``min_length``/``max_length`` (a count). The setup card
    validates against these before a run, so the bounds a user sees are the
    ones ``parse_params`` enforces.
    """
    spec = get_skill(skill)
    field = spec.params_model.model_fields.get(key)
    if field is None:
        field = _nested_request(spec)[1].model_fields.get(key)
    if field is None:
        return None, None
    lo: Optional[float] = None
    hi: Optional[float] = None
    for meta in field.metadata:
        for attr in ("ge", "min_length"):
            if getattr(meta, attr, None) is not None:
                lo = getattr(meta, attr)
        for attr in ("le", "max_length"):
            if getattr(meta, attr, None) is not None:
                hi = getattr(meta, attr)
    return lo, hi


# Where the data lives is decided by the planner from the catalog and by the
# connection settings — never by a client patch. A case-variant table name
# would otherwise pass a case-folded catalog check yet name a different
# (quoted, case-sensitive) object.
PATCH_DENIED_FIELDS = frozenset({"table", "schema_name", "catalog"})


def merge_params_patch(skill: str, base: Dict[str, Any], patch: Dict[str, Any]) -> BaseModel:
    """Apply an allowlisted patch to stored params and re-validate.

    Only keys the skill's model (or its nested ``series`` / ``entity``)
    declares survive, minus ``PATCH_DENIED_FIELDS``; anything else is dropped
    rather than raising, so a client cannot smuggle fields the contract does
    not know.
    """
    spec = get_skill(skill)
    merged: Dict[str, Any] = dict(base)
    top_fields = set(spec.params_model.model_fields)
    nested_key, nested_model = _nested_request(spec)
    nested_fields = set(nested_model.model_fields) - PATCH_DENIED_FIELDS
    nested_patch: Dict[str, Any] = dict(patch.get(nested_key) or {})
    for key, value in (patch or {}).items():
        if key in ("series", "entity", "cohort", "experiment"):
            continue
        value = _unset_sentinel(key, value)
        if key in top_fields:
            merged[key] = value
        elif key in nested_fields:
            nested_patch[key] = value
    if nested_patch:
        merged_nested = dict(merged.get(nested_key) or {})
        for key, value in nested_patch.items():
            if key in nested_fields:
                merged_nested[key] = _unset_sentinel(key, value)
        merged[nested_key] = merged_nested
    return spec.params_model.model_validate(merged)


# The card shows an optional field's "no value" as a word the user can pick
# again; the patch must turn it back into None rather than a column called "none".
_UNSET_SENTINELS = {"group_by": ("none", ""), "k": ("auto", ""), "holidays": ("none", "")}
# List-valued chips; a setup card persisted before the multiselect existed
# still sends these as one comma-separated string.
_LIST_FIELDS = frozenset({"features", "dimensions"})


def _unset_sentinel(key: str, value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _UNSET_SENTINELS.get(key, ()):
        return None
    if key in _LIST_FIELDS and isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


# ── Proposal / clarification (API-facing) ─────────────────────────────────────


class ParamChip(BaseModel):
    """One editable parameter on the setup card (confirm card and "Edit setup").

    ``key``/``value``/``options`` are the contract the patch speaks; the rest
    is presentation the card needs to be readable without a manual: which
    section the field sits in, its unit, one line of help, the bounds
    ``parse_params`` will enforce, and human names for raw enum values.
    Everything presentational is defaulted so proposals persisted before it
    existed still render (in one "Setup" section, without bounds).
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: Any
    options: List[Any] = Field(default_factory=list)
    editable: bool = True
    # Section label as displayed: Data / Model / Output / Comparison / Cohort / Test.
    group: str = "Setup"
    # Control when there are no options (``multiselect`` uses ``options`` as the pool).
    kind: Optional[Literal["number", "text", "date", "multiselect"]] = None
    unit: Optional[str] = None
    # The unit follows another chip's value: ``"grain"`` → days / weeks / months.
    unit_from: Optional[str] = None
    help: Optional[str] = None
    min: Optional[Union[int, float]] = None
    max: Optional[Union[int, float]] = None
    step: Optional[Union[int, float]] = None  # 1 marks an integer field
    required: bool = False
    option_labels: Dict[str, str] = Field(default_factory=dict)  # keyed by str(option)
    # Window chip only: the default look-back per grain, so a grain change on
    # an untouched window moves it to the new grain's default.
    defaults_by_grain: Optional[Dict[str, int]] = None


class AnalysisProposal(BaseModel):
    """What the graph returns instead of a result when it must ask first.

    ``kind='confirm'``: first run of a skill on a connection — chips + egress.
    ``kind='clarify'``: the planner found several candidates — options.
    ``kind='guard'``:   a pre-SQL guard refused — exits.
    The proposal is persisted server-side; the browser only ever posts its id
    plus an allowlisted parameter patch back to ``/api/analysis/run``.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    kind: Literal["confirm", "clarify", "guard"]
    skill: str
    title: str
    message: str
    params: Dict[str, Any]
    chips: List[ParamChip] = Field(default_factory=list)
    options: List[GuardExit] = Field(default_factory=list)
    guard_results: List[GuardResult] = Field(default_factory=list)
    egress_summary: str = ""
    tier: Tier = "A"
    estimated_seconds: int = 4
    expires_at: Optional[str] = None
    contract_version: str = CONTRACT_VERSION
