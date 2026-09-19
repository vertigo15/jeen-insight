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

The live suite is on-demand (needs an LLM and the data source); CI runs `pytest` only.

## Tags (live suite)

Every live test has an **area** tag and one or more **type** tags. `LIVE_ONLY=kp,conv` or `npm run test:live:<type>` selects by either.

- Area: `@kp` knowledge pairs · `@conv` conversations · `@ml` ML skills · `@sql` SQL smoke · `@resilience` edge cases · `@features` result & chart · `@history` conversation & history · `@extras` LLM-backed features · `@admin` settings
- Type: `@smoke` · `@e2e` · `@feature` · `@regression` · `@security`

## Where a new test goes

- A new **question** is data, not code: `tests/e2e/live/kp.cases.js` (a registered knowledge-pair question), `questions.js` (SQL or ML case) or `conversations.js` (multi-turn). The spec generates the test.
- A new **feature check** is a `test()` in the matching `tests/e2e/live/features.<results|history|llm|admin>.live.spec.js`. Add `'@smoke'` to its tags only if it runs under ~30 s with no extra LLM call.
- A **deterministic UI flow** belongs in `tests/e2e/specs/` with a fixture in `tests/e2e/harness/fixtures.js`.
- **Backend logic** belongs in `tests/unit/`; a new **guardrail case** in `evals/datasets/`.

## Ground truth

- SQL correctness: the metadata **knowledge pairs** (question → SQL) the LLM is shown. Fetched from the running stack at setup, graded in tiers (`exact` → `equivalent` → `structural` → `execution_match`); pass strictness `LIVE_KP_STRICT` (default `structural`).
- Conversations: memory answers are checked against the previous turn's rows (`derive()`).
- ML: the result envelope (`params`, `validation`, `guard_results`, `egress`, `facts`) against per-case bounds.
- Everything else is structural (route, status, surfaces) — never a number typed into a test.

## Reading the result

- Playwright list output while running; `npm run report:live` for the HTML report with traces and screenshots of failures.
- `tests/e2e/test-results-live/scorecard.md` (and `.json`): pass rates per area and type, SQL-vs-gold tiers, ML envelopes, latency p50/p95, recorded findings, and any gate failures. `junit.xml` for CI dashboards.
- A test marked **infra** failed because the stack did (`Backend unavailable`, 502/503), not because the product regressed.
