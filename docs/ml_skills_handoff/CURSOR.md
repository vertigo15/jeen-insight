# Driving the ML skills work

## The one rule to state every time
> The HTML files are a design reference, not code to copy. Recreate the design inside the existing
> app using its own patterns and the CSS custom properties in `src/static/design-tokens.css`. No new
> dependencies in the frontend, no build step, no inline styles.

## Decisions already made — do not re-open
- **No ML pushdown.** Every skill runs on aggregates pulled through the existing `SqlRunner`.
- **No new result kind.** An ML answer is an ordinary result object with extra columns and a
  different default chart.
- **No LLM does the math.** The model picks the skill and fills parameters.
- **One SQL builder** (sqlglot AST) for every registered dialect; gap-fill in pandas.
- **The sandbox is the only production runner.**

## Phases

Sequencing: P0 → (P1 ∥ P2) → P3a → P3b → P4 → P5 → enable in prod → P6.
`ML_SKILLS_ENABLED` stays off in production until P5 is deployed.

### P0 — Spec reconciliation and contract freeze
`docs/ml_skills_handoff/` (this folder), `src/analysis/contracts.py`, `src/api/models.py`
(`QueryResponse.status/proposal/analysis/low_confidence`, `AnalysisRunRequest`,
`AnalysisRerunRequest`), `db/migrations/insights/023_ml_skills.sql`, the `analysis` +
`low_confidence` keys in `extract_artifact_fields`. No runtime behaviour change.

### P1 — Analysis engine (pure Python)
`src/analysis/series.py` (calendar reindex, `observed` mask, fill policy, seasonal-period
detection), `src/analysis/guards.py`, `src/analysis/engines/{forecast,anomaly_detection}.py`,
`src/analysis/runner.py` (`AnalysisRunner`, `LocalSubprocessRunner`). Tests on synthetic series
under `tests/unit/analysis/`.

### P2 — Portable aggregation SQL
`src/analysis/sql_builder.py`: `build_series_sql`, `build_span_probe_sql`. Emission tests per
registered dialect must pass `is_read_only_sql` and the `sqlglot_validate` node unchanged.

### P3a — Planning, guard, confirm, run API
`needs_analysis` route; `src/agent/analysis_planner.py`; `nodes/analysis.py` (`analysis_planner`,
`analysis_guard`); `src/agent/analysis_store.py` (proposals, skill prefs); `src/api/routes/analysis.py`
(`/run`, `/rerun`, `/skills/prefs`).

### P3b — Execution integration
`analysis_sql`, `analysis_run` nodes; eval narration mode + `prompts/analysis_narration.md`;
`response_formatter` attaches `analysis` + `low_confidence`; `save_to_memory` persists them;
restore path returns them; audit + rate limit; `evals/datasets/ml_routing_set.yaml`.

### P4 — Answer UI
`band` chart type (role-based series → tokens); status strip segments; Model details tab;
ChartChat re-run → `/api/analysis/rerun`; confirm card; guard-refusal state; low-confidence pill on
restore/pin/export; quick-start chips. Tokens only; every state checked in dark mode.

### P5 — Sandbox
`Dockerfile.analytics`, `src/analytics_service/`, `jeen-insights-analytics` in `docker-compose.yml`
on an internal network, AKS manifests, `HttpSandboxRunner`, prod startup check.

### P6 — Breadth, then tier B
contribution, changepoint, seasonality, correlation, multi-series; then clustering and
driver_analysis with the row-level builder, audit and guards.

### P7 — Interpretable models, boosting options, retention
- **regression** (statsmodels OLS): signed coefficients, standardized betas, 95% CIs, p-values, VIF,
  held-out + adjusted R². Same tier-B entity plumbing as driver_analysis; the equation, not just
  importance.
- **classification** (statsmodels Logit, sklearn LogisticRegression fallback on separation): odds
  ratios per SD with CIs and p-values, held-out ROC AUC, PR-AUC and Brier. Binary target only
  (`target_binary` guard).
- **driver_analysis `method`**: `hgb` (default) | `xgboost` | `lightgbm` | `auto`. Optional engines
  fall back to HistGradientBoosting with a note. Boosting variants stay *candidates*, not new skills.
- **cohort_retention** (new `cohort` family, tier A): `COUNT(DISTINCT entity)` per signup cohort ×
  activity period, one UNION-ALL branch for the NULL-period cohort size; the engine computes offsets
  and retention curves in pandas (no `DATE_DIFF`, which does not transpile). Guards `cohort_size`
  (≥ 2 cohorts) and `retention_history` (≥ 2 offsets). Pure aggregation, no model.

### P8 — A/B testing
- **experiment_test** (new `experiment` family, tier A): one summary row per arm —
  `arm, COUNT(*), SUM(x), SUM(x*x)`. Variance is reconstructed from the moments in the engine
  (`Var = (Σx² − n·mean²)/(n−1)`) to avoid the dialect-specific `STDDEV` spelling. `binary` outcomes
  → two-proportion z-test (pooled-variance p-value, unpooled Wald CI on the difference); `continuous`
  → Welch's t-test (`ttest_ind_from_stats`, unequal variances). Control arm is stated or inferred
  (a conventional name, else the first arm by label) so lift is never chosen to look favourable.
  Pre-SQL guards check the arm column is categorical and the outcome numeric; post-SQL guards
  `arm_count` (exactly 2) and `group_size` (≥ 30 per arm). A statistical test, not a fitted model.
- **Deferred**: `forecast_hierarchy` (coherent grouped forecasts via bottom-up/MinT reconciliation)
  and foundation-model forecasters (Chronos-Bolt, TimesFM). The latter are hundreds of MB with a GPU
  cost curve and only beat the statistical models on very short series, so they wait for real demand.

## Reviewing what gets produced
- Inline styles or hex literals in a frontend diff: it copied the prototype and broke dark mode.
- A new result kind, an `MLResult` class, or a second rows-table renderer: missed section 8 item 1.
- A new LLM call for narration: missed the eval-node reuse. Delete it.
- `eval()`, `exec()`, or model-authored code reaching the sandbox: hard stop, security review.
- Per-dialect SQL string templates or calendar SQL: missed section 5.
- Client-supplied `{skill, params}` accepted by `/api/analysis/run`: missed the proposal model.
- MAPE anywhere: missed the metric rule.
- Charts where the queried series is lavender: inverted the token rule in section 2.
- `analysis` / `low_confidence` missing from a restored turn: missed the allowlist extension.
