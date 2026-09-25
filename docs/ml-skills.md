# ML analysis skills

Jeen Insights answers a data question two ways:

- **Text-to-SQL** — a read-only `SELECT` is written and run, then the results, a chart and short insights are shown.
- **Analytics / ML skills** — when a single query can't answer the question (anomalies, forecasts, trend shifts, drivers, segments, experiments), a **validated statistical/ML model** runs over the data and the finding is explained in plain language.

This page lists the skills that ship today, the algorithm behind each, and how to activate them. The list is defined in code in [`src/analysis/contracts.py`](../src/analysis/contracts.py) (`SKILLS`); each engine lives under [`src/analysis/engines/`](../src/analysis/engines/).

> For how the ML branch is wired into the agent graph (`analysis_planner → analysis_guard → analysis_sql → analysis_run`) and where it sits among the other routes, see [agent-state-flow.md](./agent-state-flow.md) (§10.3).

## How to activate a skill

1. **Enable the feature.** ML skills are gated by the `ML_SKILLS_ENABLED` setting; it must be on for the deployment. When off, every data question is text-to-SQL.
2. **Just ask in plain language.** The router classifies the question; strong keyword cues (e.g. "forecast", "anomaly", "why did X drop", "segment") route it to a skill even when the wording is ambiguous. If no registered skill fits the connection's catalog, it falls back to SQL.
3. **Confirm on the card.** Before an analysis runs, a **confirm card** opens with a one-line summary of the setup (`SUM(salesamount) · per month · last 24 months · 6 months ahead · 80% interval · Auto model`) that updates as you edit, and a **setup form** grouped into sections — **Data** (measure, aggregate, date column, split by), **Model** (grain, look-back window, model, sensitivity, …) and **Output** (horizon, interval, …); skills without a model use their own sections (Comparison, Cohort, Test). Every field has a label, a unit that follows the grain (`26 weeks`), one line of help, and the allowed range; out-of-range values are flagged on the field before anything is sent, and a field the server refuses is marked in place. Changed fields carry a **changed** badge and one **Reset all** restores the plan. Then:
   - **Run** — execute the analysis.
   - **Rephrase the question** — return to the composer to ask in other words.
   - **Answer with SQL instead** — skip ML for this one question.
   - **Don't ask again** — remember your consent for that skill on that connection (skips the card next time). The card states what leaves the database ("Aggregates only" or "Row-level, capped") next to the run-time estimate.
4. **Ask about the skills themselves.** Questions like *"what can this app do?"*, *"which ML models can I use?"* or *"can I use 3-sigma on profit?"* are answered directly (the `capability` route).

## Egress tiers

Every skill declares how much data leaves the database into the sandboxed analytics service:

- **Tier A — aggregate.** Only per-period / per-arm aggregates are sent (e.g. one value per month). All time-series, cohort and experiment skills.
- **Tier B — row-level.** Entity-level rows (customers, products, …) are sent, **capped and audited**. The clustering, driver, regression and classification skills.

## Supported skills

| Skill | What it does | Say something like | Engine / algorithm | Tier |
|-------|--------------|--------------------|--------------------|------|
| **Anomaly detection** | Flags periods that fall outside the range a seasonal or trend model expected. | "Flag unusual spikes or drops in daily sales" · "Is anything weird in profit?" | statsmodels **MSTL** decomposition or **LOWESS** trend + robust (MAD) residual band; `sigma3` alternative | A |
| **Forecast** | Cross-validates a shortlist of models and projects the measure forward with a calibrated interval. | "Forecast revenue for the next 6 months" | **statsforecast** — AutoARIMA / AutoETS / Theta / Drift / SeasonalNaive (+ Croston / ADIDA / IMAPA on intermittent demand), rolling-origin CV, conformal band, optional holiday regressor | A |
| **Changepoint detection** | Finds where the series' underlying level shifted. | "When did the trend in orders shift?" | **ruptures PELT** on the de-seasonalised trend | A |
| **Seasonality** | Decomposes into trend / seasonal cycle / remainder; reports strength, peaks, troughs. | "Is there a seasonal pattern in sales?" | statsmodels **MSTL/STL** decomposition | A |
| **Correlation** | Measures how two measures move together, including at a lag (association, not causation). | "Is ad spend correlated with revenue?" | **scipy** Pearson across lags + Granger, multiple-test adjusted | A |
| **Contribution analysis** | Explains a change between two periods by decomposing the delta across each dimension's slices. | "What drove the change in profit last quarter?" | Arithmetic delta decomposition (Adtributor-style) — not a model | A |
| **Clustering** | Groups entities (customers, products, stores) into segments from numeric features. | "Segment customers into groups" | **scikit-learn** KMeans / HDBSCAN, silhouette-scored | B |
| **Driver analysis** | Ranks candidate features by how well they predict a target (predictive, not causal). | "What drives customer churn?" | **scikit-learn** HistGradientBoosting (or XGBoost / LightGBM) + permutation importance | B |
| **Regression** | Interpretable linear model: signed coefficients, effect sizes, CIs, p-values, R². | "What explains store revenue?" | **statsmodels OLS** | B |
| **Classification** | Predicts a binary target (e.g. churn yes/no): odds ratios, CIs, p-values, held-out AUC, calibration. | "Which customers are likely to churn?" | **statsmodels Logit** / logistic regression | B |
| **Cohort retention** | Groups entities into signup cohorts and measures the share still active 1, 2, … periods later. | "Show retention by signup cohort" | Pure SQL aggregation of cohort-by-period counts | A |
| **A/B test** | Compares two arms on one outcome: difference, relative lift, confidence interval, p-value. | "Did variant B beat variant A?" | **scipy** — two-proportion z-test (binary) / Welch's t-test (continuous) | A |

## Choosing or changing the model / method

Some skills expose a model you can pick — the **Model** field in the card's Model section (the `method` parameter, shown with its human name), or by saying it in the question (e.g. *"flag anomalies in profit using 3-sigma"*, *"forecast revenue with ETS"*):

| Skill | Parameter | Options (default first) — shown as |
|-------|-----------|-------------------------------------|
| Anomaly detection | `method` | `auto` (MSTL or LOWESS + robust MAD band) — Auto · `seasonal` (force MSTL) — Seasonal (MSTL) · `trend` (force LOWESS) — Trend (LOWESS) · `sigma3` (mean ± 3σ) — 3-sigma |
| Forecast | `method` | `auto` — Auto · `auto_arima` — Auto ARIMA · `auto_ets` — Auto ETS · `theta` — Theta · `drift` — Drift · `seasonal_naive` — Seasonal naive |
| Clustering | `method` | `kmeans` — K-means · `hdbscan` — HDBSCAN |
| Driver analysis | `method` | `hgb` — Gradient boosting · `xgboost` — XGBoost · `lightgbm` — LightGBM · `auto` — Auto |
| A/B test | `outcome_type` | `binary` — Binary (conversion) · `continuous` — Continuous (numeric) |

You can also adjust the other parameters on the card before running — e.g. the **Look-back window** (in periods of the chosen grain; defaults day 90 / week 26 / month 24 for most skills, and day 120 / week 130 / month 36 for a **forecast**, which needs two full seasonal cycles plus room to hold the horizon out — changing the grain moves an untouched window to the new grain's default, while a number you typed keeps its value and only its unit changes), **Sensitivity**, **Grain**, or the candidate **Features** (picked from the table's numeric columns, 2–8). The allowed range of every numeric field is the one the contract enforces ([`src/analysis/contracts.py`](../src/analysis/contracts.py)); the card reads it from there, so what you see is what the server accepts.

A finished result has **Edit setup** in its status strip: it opens the same form with the values that ran — measure, date column, grain, window, horizon, model, split by, … — and **Re-run** (enabled once a value differs) produces a new answer (a child turn; the original is untouched) with the Model details tab listing what changed. For a forecast, `auto` runs the shortlist (Naive/SeasonalNaive, Drift, AutoETS, AutoARIMA) and keeps the baseline unless a model beats it in rolling-origin cross-validation; a model you pick explicitly is used regardless, and the note states its score against the baseline.

### How a forecast is validated and banded

- **Cross-validation depth.** Up to five rolling-origin folds at the requested horizon; when the history cannot afford five non-overlapping folds the origins overlap (step = h/2), so a short history still yields several votes and enough residuals to calibrate. When a confirmed season leaves no room to hold the horizon out after two full cycles, ETS/ARIMA are validated *without* a seasonal term (the seasonal-naive baseline keeps the season) rather than returning an unvalidated baseline.
- **Calibrated interval.** The band shown is not the model's native (Gaussian) band: each held-out residual is divided by the model's own half-width at that step, and one finite-sample conformal factor rescales the refit model's band. The Model details **Coverage** figure is then out-of-sample — fitted on the earlier folds, measured on the latest one or two. With too few residuals for the level (fewer than 4 at 80%, 19 at 95%) the native band is kept and the note says so. Point-only models get a constant conformal half-width instead.
- **Intermittent demand.** When more than half the periods are zero, Croston (SBA / classic), ADIDA and IMAPA join the `auto` shortlist and compete on the same folds; the `intermittent` guard records the share instead of refusing a forecast.
- **Holiday calendar** (`holidays`, a country code such as `IL`). The one regressor whose future is known: a holiday indicator (daily) or holidays-per-period count (weekly / monthly) is fitted by the ARIMA candidates; the other candidates ignore it. Business drivers (price, spend) are deliberately not accepted as regressors — their future values are unknown at forecast time.
- **Adjustments to try.** The Model details tab lists one-click re-runs derived from the result's own numbers (a poor band → coarser grain, no season confirmed with a limiting window → wider window, the baseline won → Theta). Every chip is validated against the contract before it is offered and re-runs as a child turn.
- **Realized accuracy.** Every completed forecast is captured (migration 034). Once forecast periods have elapsed, **Check against actuals** in Model details re-runs the same aggregation over those periods and shows the realized WAPE / MASE and band coverage beside what the model claimed, with the actuals drawn over the chart.

### Contract version 3 — rollout notes

`ForecastParams` gained `holidays`, so `CONTRACT_VERSION` moved from `"2"` to `"3"`. Consequences of any bump: pending confirm / clarify / guard proposals answer `409 The analysis contract changed; ask the question again` (they expire in 15 minutes regardless); "don't ask again" consent is keyed to the version, so every skill re-prompts once; the API and the analytics sandbox are separate Deployments and the sandbox refuses a mismatched version with a 409, so ship both images together (API first is safe: it fails closed until the sandbox follows). Completed turns keep their stored version and still re-run.

## Where the results appear

- **Answer** — a one- to two-sentence summary grounded strictly in the model's numbers.
- **Key insights** — algorithm-aware findings built deterministically from the engine's facts (the flagged periods, projected value, top drivers, …).
- **Follow-up chips** — clickable questions that reference the concrete entities the analysis surfaced.
- **Chart** — a band/line chart (actual, expected, interval, flagged points, forecast).
- **Model details tab** — method used, the validation metric (WAPE / MASE / R² / AUC / silhouette / p-value), candidate models, guard results, and data provenance.

## Guards

Each skill declares pre-run guards (in `SKILLS[...].guards`) that check the data is suitable — e.g. `series_length`, `min_history`, `gap_ratio` for time series; `cardinality` / `feature_count` for entity models; `cohort_size` / `retention_history` for cohorts; `arm_count` / `group_size` for A/B tests. A failed guard is shown as a result (not an error) with options to adjust, override (flagged low-confidence), or answer with SQL instead.

### Incomplete trailing period

A time series whose newest data stops mid-period (a month with rows up to the 16th, a week that ends on a Wednesday) would hand the model a month-to-date figure as if it were a full month — a forecast would then anchor on the shortfall and an anomaly check would flag it. The series preparation therefore **sets the last period aside** when either:

- the newest source timestamp (from the span probe) stops more than two days before the period's last day — the two-day slack keeps a weekend-closed business's Friday-ending weeks and months intact; or
- for `SUM` / `COUNT` of a non-negative measure, the last period is under 25% of the typical recent period while the period before it was ordinary and the same period one cycle earlier was not itself that low (a yearly shutdown month is a season, not a stub; daily series compare against the same weekday). This rule also runs when the days *are* covered — the AdventureWorks sample has rows on every day of its final month at 3% of the usual total — and the caveat then says so ("…although its data runs to 29 Jun 2025") and calls the period a *likely* partial load rather than a fact.

The period is removed from the fitted history, reported as an informational `partial_tail` guard and in the result's caveats with its numbers ("Jul 2008 is incomplete (data ends 1 Jul 2008, 1 of 31 days) and was set aside"), and for a forecast it becomes the first forecast period, with the model's full-period estimate shown next to the to-date figure. It still counts toward the `max_horizon` window, so setting it aside never flips that guard. A requested range that runs past the newest source row never adds zero-filled "future" periods. Forecasts of a measure that never went below zero are also floored at zero (point and interval).
