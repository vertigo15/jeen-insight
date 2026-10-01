# Tests

One place to answer "which test do I ask for, where does it go, how do I run it".
Live suite details (env vars, tiers, scorecard) are in [`e2e/README.md`](e2e/README.md).

## Test types

| Type | What it proves | Where | Run | Needs |
|---|---|---|---|---|
| **Unit** | Backend logic with mocked DB/LLM: nodes, planners, validators, routes | `tests/unit/` | `pytest` (default `testpaths`) | nothing — CI |
| **JS unit** | Frontend helpers (formatting, chart transforms, hydration) | `tests/js/*.mjs` | `node tests/js/<file>.mjs` | nothing |
| **Offline eval** | Golden sets scored by the production guardrails: SQL safety, groundedness, routing cues, ML planner | `evals/datasets/*.yaml` | `python -m evals.run_eval` | nothing — CI-able |
| **Integration** | Real Postgres, compiled graphs, Selenium rendering | `tests/integration/` | `JEEN_E2E_DB=1 pytest tests/integration -m integration` | Postgres; Chrome for Selenium |
| **UI e2e (mocked backend)** | Deterministic UI flows through the real frontend modules with a fake backend | `tests/e2e/specs/` | `cd tests/e2e && npm test` | nothing — CI-able |
| **Live smoke** | The stack is alive and answering: health, login, connection, one knowledge-pair answer, ML card, reload | `tests/e2e/live/smoke.live.spec.js` (+ `@smoke`-tagged tests) | `npm run test:live:smoke` (~8 min) | running stack |
| **Live e2e** | Full user journeys: SQL questions, curated knowledge pairs, multi-turn conversations, ML skills, resilience | `live/{sql,kp,conversations,ml,resilience}.live.spec.js` | `npm run test:live` (~60 min) | stack + LLM + AdventureWorksDW |
| **Live feature** | One product feature at a time on a real answer: table tools, chart controls, history, LLM extras, admin settings | `live/features.*.live.spec.js` | `npm run test:live:features` (~30 min) | stack; admin login for `@admin` |
| **Live regression** | The graded set (`@kp @conv @ml`) with scorecard gates on | same files | `npm run test:live:regression` | stack; nightly / pre-release |
| **Live security** | 401 without session, CSRF 400, admin 403, write-intent refusal, validation + DLP nodes ran | `live/security.live.spec.js` | `npm run test:live:security` | stack |
| **Live performance** | Latency p50/p95 of answered turns, gated by `LIVE_MAX_P95_MS` | scorecard dimension | `npm run test:live:perf` | stack |
| **Knowledge-pair sweep** | Every registered pair for the connection, graded | `live/kp.live.spec.js` | `npm run test:live:kp:all` (`LIVE_KP_LIMIT=n`) | stack |
| **Stress** | Accuracy and speed with 1, then 5, then 10 users asking in parallel: SQL vs knowledge-pair gold, time to first table and full answer, queueing, failures by kind | `tests/stress/` | `cd tests/stress && npm run stress` (~20 min locally; `stress:quick` ~10 min) | stack + LLM; `tests/e2e` npm-installed |

The live suite and the stress runner are on-demand (they need an LLM and the data source); CI runs `pytest` only.

## Tags (live suite)

Every live test has an **area** tag and one or more **type** tags. `LIVE_ONLY=kp,conv` or `npm run test:live:<type>` selects by either.

- Area: `@kp` knowledge pairs · `@conv` conversations · `@ml` ML skills · `@sql` SQL smoke · `@resilience` edge cases · `@features` result & chart · `@history` conversation & history · `@extras` LLM-backed features · `@admin` settings
- Type: `@smoke` · `@e2e` · `@feature` · `@regression` · `@security`

## Where a new test goes

- A new **question** is data, not code: `tests/e2e/live/kp.cases.js` (a registered knowledge-pair question), `questions.js` (SQL or ML case) or `conversations.js` (multi-turn). The spec generates the test.
- A new **feature check** is a `test()` in the matching `tests/e2e/live/features.<results|history|llm|admin>.live.spec.js`. Add `'@smoke'` to its tags only if it runs under ~30 s with no extra LLM call.
- A **deterministic UI flow** belongs in `tests/e2e/specs/` with a fixture in `tests/e2e/harness/fixtures.js`.
- **Backend logic** belongs in `tests/unit/`; a new **guardrail case** in `evals/datasets/`.
- A **stress / load** check belongs in `tests/stress/`; its questions are the curated `kp.cases.js` set, so a new stress question is a new knowledge-pair case.

## Ground truth

- SQL correctness: the metadata **knowledge pairs** (question → SQL) the LLM is shown. Fetched from the running stack at setup, graded in tiers (`exact` → `equivalent` → `structural` → `execution_match`); pass strictness `LIVE_KP_STRICT` (default `structural`).
- Conversations: memory answers are checked against the previous turn's rows (`derive()`).
- ML: the result envelope (`params`, `validation`, `guard_results`, `egress`, `facts`) against per-case bounds.
- Everything else is structural (route, status, surfaces) — never a number typed into a test.

## Reading the result

- Playwright list output while running; `npm run report:live` for the HTML report with traces and screenshots of failures.
- `tests/e2e/test-results-live/scorecard.md` (and `.json`): pass rates per area and type, SQL-vs-gold tiers, ML envelopes, latency p50/p95, recorded findings, and any gate failures. `junit.xml` for CI dashboards.
- A test marked **infra** failed because the stack did (`Backend unavailable`, 502/503), not because the product regressed.

## Stress (`tests/stress/`)

A plain Node runner (no browser, no dependencies of its own). Each simulated user is its own `stress-NN@stress.test` viewer account, because the API allows at most 5 parallel questions per account. At each level (1, 5 and 10 users by default) all users start together and ask the curated knowledge-pair questions from `e2e/live/kp.cases.js` back-to-back, each in a new conversation, through `POST /api/ask/stream` the way the workspace does; the chart is requested when the first rows arrive, as in the browser. Answers are graded after each level with the live suite's grader, so accuracy means the same thing as in `npm run test:live:kp`.

```bash
cd tests/e2e && npm install     # the runner reuses tests/e2e/live
cd ../stress
npm test                        # offline self-test, no stack needed
npm run stress:users            # create stress-01..10 and log them in (paced: /login allows 5 a minute per IP)
npm run stress                  # levels 1, 5, 10: 12 + 60 + 120 answers (~20 min when an answer takes ~15 s)
npm run stress:quick            # 4 questions per user: 4 + 20 + 40 answers (~10 min)
npm run stress:report -- results/<run> [--regrade]   # grade and render a run, also an interrupted one
npm run stress:teardown         # delete the stress accounts and their conversations
```

| Env var | Default | Meaning |
|---|---|---|
| `LIVE_APP_URL`, `LIVE_EMAIL`, `LIVE_PASSWORD`, `LIVE_CONNECTION` | as the live suite | the UI, an admin login (creates the accounts, reads the knowledge pairs), the connection |
| `LIVE_KP_STRICT`, `LIVE_GOLD_DSN` | `structural`, — | SQL-vs-gold pass tier; optional row comparison against the gold |
| `STRESS_LEVELS` | `1,5,10` | users in parallel, one level after another |
| `STRESS_QUESTIONS_PER_USER` | the case count (12) | questions per user per level; more than the set cycles through it again |
| `STRESS_CASES` | all | comma list of `kp.cases.js` ids |
| `STRESS_THINK_MS` | `0` | pause between one user's questions |
| `STRESS_ASK_TIMEOUT_MS` | `300000` | a question with no answer by then is a `timeout` failure |
| `STRESS_CHART` | `1` | `0` = do not send the chart request |
| `STRESS_COOLDOWN_MS` | `30000` | pause between levels |
| `STRESS_USER_PREFIX`, `STRESS_USER_PASSWORD` | `stress`, a local default | account names and password; the password is required for a remote target |
| `STRESS_ALLOW_REMOTE` | — | `1` is required for any target that is not localhost |
| `STRESS_MIN_ACCURACY`, `STRESS_MAX_ACCURACY_DROP`, `STRESS_MAX_P95_MS`, `STRESS_MAX_ERROR_RATE` | — | gates (fractions, except the ms one); unset = report only; exit code 1 when one fails |

Each run writes `tests/stress/results/<yyyymmdd-hhmm>-<host>/`: `run.json` (target, app version, model, git SHA, settings), `answers.jsonl` (every answer with its timings, metrics, SQL, rows and grade, saved as it arrives), `summary.json` and `report.md`.

Reading the report:

- **Correct**: the SQL passes the gold at `LIVE_KP_STRICT` and the row count is in the case's range. **Wrong**: it does not. **Failed**: no usable answer (timeout, 429, 5xx, stream error, not answered with SQL), counted by kind. **Ungraded**: no gold, or the grader could not parse the SQL. Accuracy = correct / (correct + wrong).
- Times are seconds from sending the question, over answered questions: *stream opened* (waiting for a UI-server thread and the API; the first number to grow when the UI server's 8 threads are all busy), *first table*, *full answer*, *stream closed* (history saved) and *chart ready*. "Where the time goes" splits the answer time per graph step.
- "Same rows as first level" shows whether a question returned the same result values under load as with one user. A question that is **wrong** by SQL shape but has the same rows computed the right numbers in a different way; set `LIVE_GOLD_DSN` and run `npm run stress:report -- results/<run> --regrade` to grade by rows instead, without asking again.
- The response does not say which model answered. When the provider rate-limits (HTTP 429) the app switches to another healthy model without failing the question, so the only sign in the report is a sudden shift from `exact` to `structural`/`mismatch` tiers. Check the API log: `docker logs jeen-insights-api 2>&1 | grep 'Error code: 429'`.
- With the default 12 questions the 1-user level has 12 answers, so compare per question rather than reading much into a few points of overall accuracy.

On a shared stack (e.g. dev161) `STRESS_ALLOW_REMOTE=1` and `STRESS_USER_PASSWORD` are required, and the runner waits 10 s before it starts. It writes only what normal use writes (the viewer accounts, conversations, usage-ledger rows); teardown removes the accounts and their conversations, while the usage-ledger rows stay. Run it at a quiet time: the stack's database and LLM quota are shared.
