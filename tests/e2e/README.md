# ML skills — Playwright e2e

End-to-end coverage for the two things that matter about ML skills in the UI:

1. **When does the app run ML vs text-to-SQL?** — every canonical question is
   driven through the real frontend controller and the visible path badge is
   asserted against the **real backend routing rule**
   (`src/agent/langgraph_agent/nodes/router.py::explain_routing`).
2. **The ML flows** — confirm card (egress notice + editable chips), guard
   refusal (zero rows sent), clarification → resume, and re-run with a param
   diff.

## How it works (no live backend)

The specs load a static **harness** (`harness/index.html`) that boots the *real*
modules under test — `src/static/workspace/workspaceController.js` and
`src/static/analysis/analysisPanel.js` — with real CSS. A small fake backend
(`harness/fakeBackend.js`) stubs `window.fetch`:

- `POST /api/ask/stream` → an SSE stream ending in a `result` event.
- `POST /api/analysis/run|rerun` → a completed analysis turn.
- `GET  /api/analysis/routing` → the real routing prediction.

The **routing labels are not hand-written**: `generate_fixtures.py` calls the
real `explain_routing` over the canonical questions and writes
`harness/routing.generated.js`, which the fake backend uses to stamp each
streamed answer's path. If backend routing changes, these tests change with it.
`globalSetup.js` regenerates that file before every run.

The response *shapes* (proposal / analysis envelopes) live in
`harness/fixtures.js` and match the `QueryResponse` / proposal contract.

## Run

```bash
cd tests/e2e
npm install                 # installs @playwright/test@1.61.1 (browser is cached)
npm test                    # runs all specs headless
npm run test:headed         # watch it in a browser
npm run report              # open the last HTML report
```

The web server is a plain `python3 -m http.server` rooted at the repo, started
automatically by Playwright.

## The routing contract (also unit-tested)

`tests/unit/test_routing_preview.py` covers `resolve_ml_route` / `explain_routing`
and the `GET /api/analysis/routing` endpoint directly. Quick manual check:

```bash
curl -s "$UI/api/analysis/routing?q=Forecast+profit+next+8+weeks" | jq .would_route  # needs_analysis
curl -s "$UI/api/analysis/routing?q=Total+sales+by+region"        | jq .would_route  # router_decides
```

`would_route` is one of `needs_analysis` (ML), `needs_query` (SQL), `greeting`,
or `router_decides` (no deterministic cue — the router LLM chooses at run time).

## Keeping questions in sync

The canonical questions appear in three places and must match exactly:
`generate_fixtures.py` (`QUESTIONS`), `harness/fixtures.js` (`Q`), and
`specs/_helpers.js` (`Q`).

## Live suite (real stack, real LLM, real data)

`live/` is a second Playwright project that drives **real SQL and ML questions
through the running UI** — Docker stack → BFF → API → LLM → AdventureWorksDW —
and asserts what the user sees: the route badge, the status strip, the SQL
tab, the grid, the chart, the ML confirm/guard cards, Model details, Edit
setup and re-run. Nothing is stubbed. It is on-demand and **not part of CI**
(CI has no LLM or database credentials).

```bash
docker compose up -d                          # the stack on :8501
cd tests/e2e
npm run test:live:smoke                       # alive and answering (~8 min)
npm run test:live                             # e2e: sql + kp + conv + ml + resilience (~60 min)
npm run test:live:features                    # results/chart, history, LLM extras, admin (~30 min)
npm run test:live:regression                  # kp + conv + ml with scorecard gates on
npm run test:live:security                    # auth boundary, CSRF, admin gating, write refusal
npm run test:live:kp | test:live:conv | test:live:ml | test:live:sql | test:live:admin
npm run test:live:kp:all                      # every registered knowledge pair (sweep)
npm run test:live:headed                      # watch it
npm run report:live                           # HTML report
npm run scorecard                             # test-results-live/scorecard.md
```

| Env var              | Default                 | Meaning                                                                 |
|----------------------|-------------------------|-------------------------------------------------------------------------|
| `LIVE_APP_URL`       | `http://localhost:8501` | the UI/BFF                                                              |
| `LIVE_EMAIL`         | `admin`                 | login (admin needed for knowledge pairs and `@admin`)                   |
| `LIVE_PASSWORD`      | `admin`                 | login (older stacks use `ChangeMe123!`)                                 |
| `LIVE_CONNECTION`    | `AdventureWorksDW`      | connection display name (or source_key)                                 |
| `LIVE_RETRIES`       | `1`                     | Playwright retries per test                                             |
| `LIVE_ONLY`          | —                       | tag filter, comma list: area (`kp,conv,ml,sql,resilience,features,history,extras,admin`) or type (`smoke,e2e,feature,regression,security`) |
| `LIVE_KP_ALL`        | —                       | `1` = one test per registered knowledge pair (sweep)                   |
| `LIVE_KP_LIMIT`      | —                       | cap the sweep at n pairs                                                |
| `LIVE_KP_STRICT`     | `structural`            | SQL-vs-gold pass tier: `exact`, `equivalent` or `structural`            |
| `LIVE_GOLD_DSN`      | —                       | Postgres DSN: run the gold SQL read-only and compare rows (`execution_match`) |
| `LIVE_STRICT_ANSWER` | —                       | `1` = fail when the answer sentence states numbers not in the rows      |
| `LIVE_SKIP_KP_FETCH` | —                       | `1` = reuse `live/.generated/knowledge_pairs.json` (skips one login)    |
| `LIVE_MIN_PASS_RATE` | —                       | scorecard gate: minimum pass rate (fraction) over non-infra tests       |
| `LIVE_MIN_SQL_EQUIV` | —                       | scorecard gate: minimum share of graded `@kp` answers passing           |
| `LIVE_MAX_P95_MS`    | —                       | scorecard gate: maximum p95 wall time of answered turns                 |

Prerequisites: an admin login, `ML_SKILLS_ENABLED=true`, at least one healthy
LLM model, and the connection's metadata registered (tables + knowledge pairs).
Both catalog sources (Settings → Catalog source: `db` or the schema-modeler
`mcp`) must give the same answers; every test is annotated with the source it
ran under, so a regression in one adapter is attributable, and the `@admin`
suite switches between them and checks both serve the knowledge pairs.

### Test types and tags

Every test carries an **area** tag and one or more **type** tags (set on
`test.describe`); `npm run test:live:<type>` and `LIVE_ONLY` select by either.

| Type          | Files                                                              | What it proves                                                    |
|---------------|--------------------------------------------------------------------|-------------------------------------------------------------------|
| `@smoke`      | `smoke.live.spec.js` + a few tagged tests                          | health, login, connection, one KP answer, ML card, reload         |
| `@e2e`        | `sql`, `kp`, `conversations`, `ml`, `resilience`                   | full user journeys through the real UI                            |
| `@feature`    | `features.results`, `features.history`, `features.llm`, `features.admin` | one product feature at a time on a real answer              |
| `@regression` | `kp`, `conversations`, `ml`                                        | the graded set; run with gates (`test:live:regression`)          |
| `@security`   | `security.live.spec.js`                                            | 401 / CSRF 400 / admin 403 / write-intent refusal / validation nodes |

### Ground truth

- **Knowledge pairs (`@kp`)** — the metadata's question → SQL examples the LLM is
  shown (`# Knowledge Pairs` in `src/agent/prompts/jeen_insights_system.md`).
  `live/globalSetup.js` logs in and reads them from the admin route
  `GET /api/settings/prompts/jeen_insights_system/resolved?connection=…`
  (`live/knowledgePairs.js` parses both the DB and the MCP rendering) into
  `live/.generated/knowledge_pairs.json`. `live/kp.cases.js` names the curated
  questions and what the UI must show; the gold SQL is never copied into the
  repo. Each answer is graded by `live/sql_equivalence.py` (the project's
  sqlglot): `exact` (normalized text) → `equivalent` (canonical AST with
  aliases anonymized) → `structural` (same tables, aggregates, GROUP BY,
  filter literals, LIMIT) → `execution_match` (opt-in, `LIVE_GOLD_DSN`) →
  `mismatch` (with a fingerprint delta). The tier is recorded on every test and
  summarized in the scorecard.
- **Conversations (`@conv`)** — `live/conversations.js`: turns in one session;
  a memory turn's expected text is computed from the previous turn's rows
  (`derive`), so "which year was higher?" is checked against the actual maximum.
- **ML (`@ml`)** — besides the rendered card and strip, `live/mlEnvelope.js`
  checks the result envelope (`params`, `validation`, `guard_results`,
  `egress`, `facts`, `chart_spec`) against the bounds each case declares.
- Everything else is **structural** — path, `Completed`, row ranges, SQL
  clauses, which surfaces render — never a number typed into a test.

How it works:

- `live/globalSetup.js` probes the stack; when it is down every test skips
  itself instead of failing. It also fetches the knowledge pairs. `live/auth.setup.js`
  logs in once and saves the session (`.auth/live.json`, git-ignored). The
  login route allows five attempts a minute: a run uses two (three with
  `@security`), so use `LIVE_SKIP_KP_FETCH=1` when re-running quickly.
- `live/_live.js` drives the app the way a user does (`window.ChatController.send`,
  the connection switcher, New conversation) and reads back the DOM; it exposes
  the raw `QueryResponse` the UI holds (`rawResult`) so tests assert on data,
  and helpers for downloads, clipboard, settings, request capture
  (`captureRequest` aborts the request the UI would send, so a preference can
  be checked without an LLM call) and snapshot/restore.
- Feature suites ask one question in `beforeAll` and exercise every feature on
  that answer. Admin tests snapshot and restore what they touch.
- Tests run serially on one worker (one LLM conversation at a time, the
  analytics sandbox allows two concurrent runs) but are independent: a failing
  question never skips the rest. Infrastructure failures ("Backend
  unavailable", a dead API container) are reported as `infra`, not as product
  regressions.
- `live/scorecard.reporter.js` writes `test-results-live/scorecard.md` and
  `.json`: pass rates per area and type, SQL-vs-gold tiers, ML envelopes,
  latency p50/p95, findings recorded by tests (e.g. money columns not treated
  as numeric by the table tools), failures, and gate results. `junit.xml` is
  written alongside for CI dashboards.
- Per-question screenshots land in `live-screenshots/`; failure traces and
  videos in `test-results-live/`.

Budgets: ~2–3 min per SQL answer (the LLM and the data source vary a lot under
load), ~5–7 min per ML run; smoke ~8 min, features ~30 min, e2e ~60 min.
