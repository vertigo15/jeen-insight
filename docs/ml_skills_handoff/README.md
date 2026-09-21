# ML Skills — design spec (reconciled)

Scope: the **ML skills feature only**. Companion to three visual references in this folder
(`1-skills-handoff.html`, `2-answer-anatomy.html`, `3-skill-states.html`). The HTML is the source of
truth for look and copy; this file is the source of truth for **values, contracts and rules**.

The contracts described here are code: [`src/analysis/contracts.py`](../../src/analysis/contracts.py)
(`CONTRACT_VERSION = "1"`). When this document and that module disagree, the module wins and this
document has a bug.

---

## 0. Reconciliation — what changed from the original handoff

The first draft of this package was reviewed against the repository and independently reviewed.
These are the corrections; the HTML mocks still show some of the old values.

- **Connectors are registry-driven.** v1 targets whatever is in `CONNECTOR_REGISTRY`
  (`src/connectors/factory.py`): PostgreSQL, Trino, Databricks. SQL Server was listed as a v1
  target but has no runner; the demo AdventureWorksDW lives on Postgres. Adding SQL Server, MySQL
  or Snowflake later is "add a runner + sqlglot dialect mapping" and needs no ML-specific SQL work.
- **Phased rollout.** `anomaly_detection` and `forecast` landed first (P1–P4); `contribution`,
  `changepoint`, `seasonality`, `correlation`, `group_by` multi-series, and the tier B
  `clustering` / `driver_analysis` followed in P6 on the same contracts; P7 added `regression`
  (interpretable OLS), `classification` (logistic), gradient-boosting method options in
  `driver_analysis`, and `cohort_retention`. See §3 for what each engine actually is.
- **Gap-fill SQL is gone.** Calendar generation does not transpile across dialects (sqlglot cannot
  emit it for tsql/mysql; its Trino output is wrong) and the old template had a `SUM(SUM(x))` bug.
  The SQL is only `DATE_TRUNC + AGG + GROUP BY`, built once as a sqlglot AST and emitted per
  dialect. Gap-fill happens in pandas with a missingness mask.
- **Anomaly `auto` is MSTL residuals** (not "Seasonal ARIMA" as the mock says). `iforest`, `shesd`,
  PyOD and stumpy are out of v1. Chronos-Bolt / TimesFM are out of v1.
- **Guards run in two stages**: pre-SQL (catalog + a one-row span probe) and post-SQL on the
  fetched aggregate series. History guards cannot be evaluated from the catalog alone.
- **Metrics**: WAPE, or MASE + MAE when the series has zeros/negatives. MAPE is banned.
- **Confirm / re-run** resume a server-persisted, expiring, owner-bound proposal; the browser never
  supplies authoritative `{skill, params}`. Re-runs create a child turn (`parent_query_id`).
- **The artifact allowlist carries `analysis` and `low_confidence`** so a restored ML turn keeps
  its Model details and its flag.
- **The eval node gets a narration mode** fed immutable engine facts; the eval → SQL-repair loop is
  disabled for successful analysis runs.
- **The sandbox (`jeen-insights-analytics`) is the only production runner.** A local subprocess
  runner exists for development and tests and refuses to start when `JEEN_DEV_MODE=false`.
- **Every run is audited** (skill, params, rows sent, columns, engine version — never values).
- **Dropped from the v1 card**: the "contributing features" block (not derivable from 26 aggregate
  rows) and the "Alert me" follow-up chip (Monitors are out of scope).
- **Copy**: "rows sent to the model", not "rows out" — an aggregate has already left the database.

---

## 1. The decision that shapes everything

None of the target connections has usable ML pushdown, so **every skill runs in Python on
aggregates pulled from the connection through the existing `SqlRunner`.** There is no pushdown
path in v1; do not build a routing abstraction for one.

The design problem is therefore the **aggregation step**, not model selection.

### Egress tiers

Every skill declares its tier. The tier drives the guard, the confirm-card copy and the audit entry.

**Tier A — aggregate (default).** SQL rolls the measure up to a time series; only the series leaves
the database and is sent to the model. 12–1500 rows (`ANALYSIS_MAX_SERIES_ROWS`). No row-level
data is read. v1: `anomaly_detection`, `forecast`. P6: `changepoint`, `seasonality`, `correlation`,
`contribution`.

**Tier B — row-level.** One row per entity. Capped (`EntityRequest.row_cap`, clamped to
`ANALYSIS_MAX_ENTITY_ROWS`, default 50,000), audited with the column list, and the confirm card
says so explicitly. P6: `clustering`, `driver_analysis`. P7: `regression`, `classification`, `cohort_retention`.

`analysis_sql` sets the executor's fetch limit to *cap + 1* (the shared `execute_query` node
otherwise defaults to 100 rows); `analysis_run` refuses any result that came back truncated rather
than analysing a silently shortened input. The sandbox enforces the same two caps independently
and counts request bytes as they arrive (a chunked body cannot bypass the limit).

Every run writes an audit entry (skill, params with filter values redacted to their shape, rows
sent, columns, engine version). Data values never — the parameters sent to the sandbox are the same
redacted copy, since SQL has already applied the filters.

---

## 2. Design tokens

Use `src/static/design-tokens.css` only. No hex literals — dark mode will break.

| Purpose | Token |
|---|---|
| Chart ink (actual series) | `var(--text)` |
| Expected / forecast series | `var(--rose)` |
| Prediction interval fill | `var(--insight-bg)` |
| Flagged / anomalous point | `var(--err)` |
| Guard banner / low-confidence pill | `var(--errsoft)` |
| Skill chip | bg `var(--rose)`, text `var(--bg)` |
| Param chip row | border `var(--border2)`, bg `var(--bg)` |
| Data series (non-ML charts) | `var(--teal)` |
| Metric labels, axis text | `var(--muted)` / `var(--faint)` |
| Mono (params, metrics, IDs) | `var(--font-mono)` |

**Rule:** lavender `--rose` means *model-generated*. Teal `--teal` means *data as queried*. An ML
chart's actual series stays `--text`; only the model's own output is lavender. Never render queried
data in lavender. The chart builder emits **roles** (`actual | expected | interval | flagged |
forecast`), never colours; the browser resolves roles to tokens at render time. Legends and
tooltips carry labels, so colour is never the only signal.

---

## 3. Skill catalog

Every skill below is registered in `src/analysis/contracts.py` and ships with an engine under
`src/analysis/engines/`. A skill's **family** decides the SQL shape and the preparation:
`series` (one measure per period), `contribution` (before/after totals per slice), `entity`
(one row per entity — tier B), `cohort` (distinct-entity counts per signup cohort × activity
period — tier A), `experiment` (count + two moments per A/B arm — tier A).

### Tier A — aggregate

| Skill | Family | Engine |
|---|---|---|
| `anomaly_detection` | series | statsmodels MSTL (periodic seasonal, robust MAD scale, small-sample inflation) or LOWESS trend; `auto` picks per confirmed seasonality, `seasonal`/`trend` force the branch; band at the normal quantile implied by `sensitivity`; `sigma3` kept as a labelled non-robust option |
| `forecast` | series | statsforecast shortlist [Naive, SeasonalNaive, AutoETS, AutoARIMA; MSTL+ETS/ARIMA for m > 24], rolling-origin CV at the horizon, native intervals with empirical coverage |
| `changepoint` | series | ruptures PELT with a **piecewise-linear** cost on the de-seasonalised series (a ramp is one regime, a step is two); BIC penalty from the robust noise scale; seasonal-echo filter below four cycles |
| `seasonality` | series | MSTL decomposition; noise-adjusted strength, peak/trough phase, trend growth once the cycle is removed |
| `correlation` | series | Pearson at every lag in ±`max_lag`, best lag + p-value, Granger both directions when n ≥ 30; **always** carries the not-causation line |
| `contribution` | contribution | before/after delta per slice, share of change, Adtributor surprise (JS divergence) to rank dimensions — arithmetic, not a model |
| `cohort_retention` | cohort | `COUNT(DISTINCT entity)` per signup cohort and activity period; the engine turns the counts into retention curves (share still active 0, 1, 2 … periods on) plus a size-weighted average curve — pure aggregation, no model |
| `experiment_test` | experiment | per-arm `count, Σx, Σx²`; a two-proportion z-test (binary outcome) or Welch's t-test (continuous) with the difference, relative lift vs a stated control, a confidence interval and a p-value — a statistical test, not a model |

**Multi-series** (`SeriesRequest.group_by`): one engine run per distinct value (≤ 12, guard
`series_count`), merged into one table with a leading `series_id` column; the chart shows the
largest series and the rows filter reaches the others.

### Tier B — row-level

| Skill | Family | Engine |
|---|---|---|
| `clustering` | entity | scikit-learn k-means (k by silhouette over 2–8 unless given) or `sklearn.cluster.HDBSCAN`; profiles in original units; the narration names the segments |
| `driver_analysis` | entity | scikit-learn `HistGradientBoostingRegressor` on a train split, held-out R², **permutation importance on the held-out split**; direction from Spearman ρ. `method` selects the boosting engine (`hgb` default, `xgboost`/`lightgbm` when installed, `auto` keeps the best held-out R²) — variants are candidates, not new skills |
| `regression` | entity | statsmodels `OLS`; signed coefficients + standardized betas, 95% CIs, p-values, VIF; held-out R² and adjusted R². Interpretable counterpart to `driver_analysis` — the equation, not just importance |
| `classification` | entity | statsmodels `Logit` (sklearn `LogisticRegression` fallback on separation); odds ratios per SD with CIs and p-values; held-out ROC AUC, PR-AUC and Brier calibration. Binary target |

Deviation from the original handoff, on purpose: driver analysis uses scikit-learn gradient
boosting + permutation importance rather than LightGBM + SHAP. It answers the same question
("which features predict the target, and how strongly") with two fewer heavy dependencies in the
sandbox image and a metric that is defined on rows the model never saw. SHAP can be added later as a
second attribution column without changing the contract.

P7 added `regression` (interpretable OLS), `classification` (interpretable logistic regression),
gradient-boosting method options inside `driver_analysis`, and `cohort_retention`. P8 added
`experiment_test` (two-proportion z / Welch's t on per-arm aggregates).
Later: forecast_hierarchy (coherent grouped forecasts via bottom-up/MinT reconciliation); foundation
models (Chronos-Bolt, TimesFM) as extra CV candidates — deferred: hundreds of MB and a GPU cost
curve, so they only earn a place where there is too little history to fit a statistical model.

### Two distinctions to hold onto
- **`contribution` is not `driver_analysis`.** "Why did revenue drop" is a decomposition of the
  delta across slices. "What predicts revenue" is a fitted model.
- **Statistical first, foundation models second.** They earn a place where there is too little
  history to fit, but they are hundreds of MB and a GPU cost curve.

### How `auto` picks a method (forecast)
1. Detect candidate seasonal periods from the grain (day: 7, and 365 when > 2 years; week: 52;
   month: 12) and **confirm** each with a seasonal-strength test. Grain alone never establishes
   seasonality. A period is testable only with **more than two full cycles** (`n ≥ 2·m + 1`,
   `series.min_points_for_period`): STL rejects a period of exactly `n/2`, and every seasonal engine
   shares this one boundary so none degrades differently at the edge.
2. Cross-validate the shortlist with rolling-origin windows scored **at the requested horizon** —
   never a single holdout. WAPE pools the folds; MASE is scaled per fold by the naive error of that
   fold's training prefix, so held-out periods never enter their own denominator.
3. Take the lowest error **only if it beats SeasonalNaive**; otherwise return SeasonalNaive and say so.
4. Report winner, runner-up and the baseline in Model details.

A method that cannot beat seasonal-naive is a finding. Say it rather than shipping a confident
wrong number.

### Validation metric
- **WAPE** when the series is non-negative with a positive total.
- Otherwise **MASE** (with MAE in business units in `extras`). Profit can be zero or negative, so
  MAPE is never used.
- Intervals show empirical holdout **coverage** with its sample size.
- Anomaly detection shows historical **band coverage**, never an implied false-positive rate.

---

## 3b. Skill contracts

Each skill is a registered `SkillSpec` in `src/analysis/contracts.py`, not a prompt:
`name, tier, params_model (pydantic, all defaulted), guards, engine (module path), routes_on`.

All skills share `SeriesRequest`: `table, schema_name, catalog, date_column, measure_column,
agg (sum|count|avg|min|max), grain (day|week|month), start, end (half-open), filters, timezone,
week_start`. In v1 `timezone` is fixed to `UTC` (timestamps are aggregated as stored) and
`week_start` to `monday` (what `DATE_TRUNC('WEEK')` yields everywhere); other values are refused
rather than mislabelled. `table`, `schema_name` and `catalog` are never client-patchable
(`PATCH_DENIED_FIELDS`), and `analysis_guard` rewrites every identifier to the catalog's exact
spelling and takes schema/catalog from the connection before any SQL is built — a case-variant name
cannot pass a case-folded check and then name a different quoted object.

### ANOMALY_DETECTION — tier A
Routes on: *anything weird / unusual / spikes / outliers / unexpected drop / is this normal*.

| Param | Type | Default |
|---|---|---|
| `series` | SeriesRequest | required |
| `window` | int periods | 90 days / 26 weeks / 24 months |
| `sensitivity` | float 0.80–0.99 | `0.95` — expected share of history inside the band |
| `method` | auto \| seasonal \| trend \| sigma3 | `auto` |

Rows: `ts, actual, expected, lower, upper, score, is_anomaly, observed`. Facts: flagged points with
date, actual, expected, deviation %; band coverage; seasonal periods; `seasonal_detected` /
`seasonal_modelled` flags stating whether a season was found and used.

### FORECAST — tier A
Routes on: *what will X be / project / next quarter / run rate / at this rate*.

| Param | Type | Default |
|---|---|---|
| `series` | SeriesRequest | required |
| `horizon` | int periods | `8` |
| `interval` | float 0.50–0.99 | `0.80` |
| `method` | auto \| auto_arima \| auto_ets \| theta \| seasonal_naive | `auto` |

Rows: `ts, actual, forecast, lower, upper, observed, is_forecast`. Facts: last actual, forecast
endpoints with interval, percent change over the horizon, winner/runner-up/baseline errors.

### Result envelope — one shape, every skill
`schema_version, skill, params, method_used, columns, rows, chart_spec (role-based),
validation {metric, value, band, basis, coverage, coverage_n}, guard_results[], low_confidence,
egress {tier, rows_sent_to_model, columns}, engine {name, version, module_hash, runner},
provenance {query_ts, sql, filters_summary, missing_policy, grain, span, periods_observed,
periods_filled}, details {method_used, seasonal_periods, candidates[], params_used, notes},
facts, headline, caveats`.

`facts` is the immutable set of numbers the narration node may restate. It never computes its own.

---

## 4. Guards

Guards are **graph nodes, not UI**. A guard failure routes to `response_formatter` exactly like
`catalog_blocked` and `dlp_blocked` do, and renders as one sentence naming the shortfall in numbers,
then up to three exits with one recommended.

**Pre-SQL** (`analysis_guard`, catalog + one-row span probe `SELECT MIN(date), MAX(date), COUNT(*)`):

| Guard | Rule |
|---|---|
| `date_column` | the chosen date column exists and is typed date/timestamp |
| `measure_numeric` | the measure column exists and is numeric (COUNT accepts any) |
| `span` | the probe returns ≥ 1 row and a non-null span |

**Post-SQL** (`analysis_run`, on the fetched aggregate series after calendar reindex):

| Guard | Rule | Applies to |
|---|---|---|
| `series_length` | n ≥ 12 periods | all |
| `gap_ratio` | share of periods with no source rows ≤ 20% | all |
| `min_history` | n ≥ max(2m, 12) for a seasonal fit with period m; otherwise fit non-seasonal (not a refusal) | all |
| `max_horizon` | horizon ≤ min(n / 3, 2 × longest confirmed period); non-seasonal: n / 3 | forecast |
| `intermittent` | share of exact zeros ≤ 50% (v1 refuses; exit = coarser grain) | all |

**Family-specific guards** (P6): `series_count` (≤ 12 series per `group_by`), `slices` (≥ 2
slices and ≤ 2,000 slice rows for contribution), `cardinality` (50 ≤ entities ≤ `row_cap`),
`feature_count` (2–8 numeric features), `target_numeric` (driver analysis, regression,
classification), `target_binary` (classification), `cohort_size` (≥ 2 signup cohorts) and
`retention_history` (≥ 2 period offsets, for cohort retention), `arm_count` (exactly 2 arms) and
`group_size` (≥ 30 observations per arm, for experiment_test).

**Every guard offers an override**, and the override sets `low_confidence: true` on the result.
That flag must survive pinning, export and the snapshot.

Exits are **server-generated and executable**: each carries a `params_patch` the browser posts back
to `/api/analysis/run`, or `kind=override`. Typical set: coarser grain (recommended), wider window,
run anyway.

---

## 5. Aggregation SQL — one builder, every dialect

`src/analysis/sql_builder.py` builds one sqlglot AST:

```sql
SELECT DATE_TRUNC('<grain>', <date_column>) AS ts, <AGG>(<measure_column>) AS value
FROM <catalog.schema.table>
WHERE <date_column> >= <start> AND <date_column> < <end> [AND <grounded filters>]
GROUP BY DATE_TRUNC('<grain>', <date_column>)
ORDER BY ts
```

emitted with `.sql(dialect=sqlglot_dialect_for(database_type), identify=True)`. Rules:
- No positional `GROUP BY 1` (illegal on SQL Server).
- Table qualified with the connection schema/catalog so `enforce_schema_qualifier` passes.
- Postgres `money` measures are cast to numeric.
- Half-open time range with typed date literals.
- Grounded filters reuse `filter_grounder` output verbatim.

No calendar in SQL. `series.prepare_series` reindexes to a complete calendar at the grain, keeps an
`observed` mask, zero-fills additive aggregates (SUM/COUNT) and leaves NaN for the rest (which the
`gap_ratio` guard then judges). Timezone and week start are declared on `SeriesRequest`.

Two more builders share the same AST discipline (P6): `build_contribution_sql` — one
`UNION ALL` branch per dimension with `CASE WHEN … 'before' / 'after'` and slices cast to text —
and `build_entity_sql` — key, features[, target] ordered by the key so the row cap is
deterministic. `group_by` adds a `series_id` column and a second GROUP BY expression; correlation
adds a second aggregated measure as `value2`.

The emitted SQL goes through the unchanged `sqlglot_validate → dlp_check → execute_query` path
and the same `SqlRunner` the question path uses.

---

## 6. Agent changes

### Router
`nodes/router.py` — `needs_analysis` in `_VALID_ROUTES`. Prompt:

> `needs_analysis` — the question asks for a prediction, an anomaly judgement, or whether
> something is unusual, and cannot be answered by a single aggregate query. Prefer `needs_query`
> when a SELECT would answer it. "What was profit last quarter" is `needs_query`. "Will profit grow
> next quarter" and "is anything weird in profit" are `needs_analysis`.

### Graph
Branch off `filter_grounder` (so grounded literal filters are available), rejoin at
`trivial_result_check`:

```
filter_grounder ──(needs_analysis)──► analysis_planner   [llm]   ──(clarify)──► response_formatter
                                           │
                                      analysis_guard     [db]    ──(fail / confirm)──► response_formatter
                                           │ (pass)
                                      analysis_sql       [logic]
                                           │
                                      sqlglot_validate ──► dlp_check ──► execute_query
                                           │
                                      analysis_run       [ml]    ──(error / guard fail)──► response_formatter
                                           │
                                      trivial_result_check ──► fused_eval_analytics (narration mode) ──► response_formatter
```

`POST /api/analysis/run` re-enters the same compiled graph with `analysis_resume=True`, so the
planner is skipped and the branch starts at `analysis_guard`. `analysis_confirmed` is set only
when the card being resumed *is* the confirm card: a resolved clarification or a guard exit
re-enters unconfirmed and still stops at the confirm card on a first run (unless remembered).

Every stop — clarify, guard, confirm — is a persisted proposal. A clarification stores the
planner's raw plan; `/run` applies the chosen option to that plan and re-validates it against the
catalog server-side (`apply_clarification` + `build_params_from_plan`), so the client never
supplies parameters. `/run` claims the proposal atomically *before* executing
(`claim_proposal`: `UPDATE … WHERE consumed_at IS NULL AND expires_at > NOW()`), releases the
claim if the run raises, and records the child `query_id` after; re-runs are created already
consumed with their idempotency key so a replay collides before anything executes. The hourly
budget is charged once, in `analysis_guard`, right before an execution — a confirm stop or a
refused guard never costs a run.

### State
`analysis_skill, analysis_params, analysis_result, analysis_guard_failure, analysis_clarification,
analysis_proposal, analysis_confirm_required, analysis_resume, analysis_confirmed, low_confidence`.

### Planner
`src/agent/analysis_planner.py`, shaped like `tool_planner.py`:
- `detect_analysis_intent(question) -> str | None` — keyword pre-filter, no LLM.
- One temperature-0 JSON call filling `SeriesRequest` + skill params from the catalog (date and
  numeric columns per table), the question and the grounded filters.
- Ambiguity (several candidate date columns / measures) → a clarification with the candidates as
  chips. Parse failure → `needs_query`.
- Never derives params from result *values* — only from the catalog and the user's text.

### Eval node — reuse, with a narration mode
`fused_eval_analytics` runs unchanged for SQL answers. When `analysis_result` is present it renders
`analysis_narration.md` with the envelope's `facts`, `validation`, `caveats` and the copy rules,
and its `answers_intent=false` never routes to the SQL repair loop. **No new LLM node.**

---

## 7. Sandbox — `jeen-insights-analytics`

The one genuinely new piece of infrastructure, and the largest new attack surface in the repo.

- Runs **our pinned skill modules with validated params**. Never evaluates model-authored code.
  That single line is what separates this from a code interpreter.
- Input is JSON (`{skill, params, series, contract_version}`); output is the envelope.
- No route to the host or the internet: compose attaches it only to an `internal: true` network
  (which still lets it reach the API container — compose has no per-container egress policy); the
  enforcing control is the AKS default-deny egress NetworkPolicy, which needs an enforcing CNI.
  No data-source credentials in the environment. No service-account token.
- Non-root, read-only filesystem, per-run tmpfs, dropped capabilities.
- Hard wall clock (30 s default), memory cap, byte-counted payload cap, per-tier row caps.
- Internal HMAC token (same `internal_auth` pattern as UI → API, its own audience) signed with
  its **own** key, `INTERNAL_ANALYTICS_SECRET`: the sandbox holds only that key, so a compromised
  sandbox can mint tokens for itself and nothing else — never an API-audience token.
- Pinned exact versions: statsforecast, statsmodels, pandas, numpy. scikit-learn only when
  clustering ships. `hdbscan` is redundant (`sklearn.cluster.HDBSCAN`).
- `ANALYSIS_RUNNER=sandbox` is required when `JEEN_DEV_MODE=false`; otherwise skills are disabled
  with a clear message. `ANALYSIS_RUNNER=local` (subprocess, resource-limited) is dev/test only.

---

## 8. UI

The result does **not** live in the conversation panel. The main answer pane owns it, with the
anatomy the app already has. `2-answer-anatomy.html` state A numbers every addition:

```
title (the question, verbatim)
status strip     Completed · anomaly_detection · 26 pts · 3 flagged · WAPE 6.4% · 26 rows sent · low confidence?
[ chart ]        band: actual (--text), expected (--rose), interval (--insight-bg), flagged (--err)
edit-in-words bar + Apply      'Adjust this analysis — "weekly instead of daily", "flag fewer"'
rows table       ts | actual | expected | lower | upper | is_anomaly
footer tabs      SQL & run details · Profiling · Model details
```

1. **An ML result is an ordinary result object** — same rows table, same Save/Send, same snapshot
   semantics. Extend `chart_builder.py` with a `band` chart type rather than inventing a result kind.
2. **The method strip is the existing status strip** (`#v3-meta-row`, `.v3-status`).
3. **Re-run is the existing edit-in-words bar** (`ChartChat.js`). For an ML result Apply posts to
   `/api/analysis/rerun`; the result is appended as a **new child turn** with a parameter-diff chip
   row against the parent. Never mutate in place.
4. **`Model details`** is one more footer tab: method, params, seasonal period, CV table
   (winner / runner-up / baseline), interval coverage + n, engine version, guard results,
   missing-data treatment, filters summary, query timestamp.
5. **The narrative and Key insights stay in the conversation panel**, produced by the eval node.
6. **The confirm card** (state B) is shown on the first run of each skill per connection, with the
   chip row as the parameter form and the egress statement ("26 weekly aggregates are sent to the
   analysis service; no order rows are read"). "Don't ask again for anomaly checks on this
   connection" persists to `insights_user_skill_prefs`, scoped to the contract version — and it is
   accepted **only** as part of running a displayed confirm card (`/run` with `remember=true` on a
   `kind=confirm` proposal). `POST /api/analysis/skills/prefs` can only forget; nothing that never
   showed the egress notice can grant consent.
7. **Guard refusal** (state C): one sentence with numbers, up to three exits, one recommended,
   "Run anyway" visibly low-confidence.
8. **`low_confidence`** renders as a pill in the strip and travels into restored turns, pins and exports.
9. **Quick-start chips**: one ML suggestion per connection in the existing empty-state suggestions
   once a date column and a measure are detected.

### What does earn a new surface
**Monitors.** Out of scope for v1. Do not show an "Alert me" chip until they exist.

---

## 9. Copy rules

- Headline is the **finding**, never the operation. "Three weeks fall outside the expected range",
  not "Anomaly detection complete".
- Name the method in the strip, never in the headline.
- Always state the validation number where the user can see it without clicking.
- Never say "the model predicts" — say what it predicts.
- Every interval gets a plain-language caveat once: "treat anything past three periods as a
  direction, not a number".
- Correlation results always carry the not-causation line (P6).
- Guard refusals name the shortfall in numbers ("62 of 104 periods"), then offer exits.
  Never "insufficient data".
- Say "rows sent to the model", not "rows out".
