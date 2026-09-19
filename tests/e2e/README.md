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
npm run test:live                             # everything (~25 min)
npm run test:live:sql                         # ~10 SQL questions + greeting/retry (~8 min)
npm run test:live:ml                          # ~9 ML skills + consent/exits (~15 min)
npm run test:live:headed                      # watch it
npm run report:live                           # HTML report
```

| Env var           | Default                 | Meaning                                   |
|-------------------|-------------------------|-------------------------------------------|
| `LIVE_APP_URL`    | `http://localhost:8501` | the UI/BFF                                |
| `LIVE_EMAIL`      | `admin`                 | login                                     |
| `LIVE_PASSWORD`   | `admin`                 | login (older stacks use `ChangeMe123!`)   |
| `LIVE_CONNECTION` | `AdventureWorksDW`      | connection display name (or source_key)   |
| `LIVE_RETRIES`    | `1`                     | Playwright retries per test               |
| `LIVE_ONLY`       | —                       | `sql`, `ml` or `resilience` (tag filter)  |

Prerequisites: an admin login, `ML_SKILLS_ENABLED=true`, and at least one
healthy LLM model. Both catalog sources (Settings → Catalog source: `db` or the
schema-modeler `mcp`) must give the same answers; every test is annotated with
the source it ran under, so a regression in one adapter is attributable. (The
MCP adapter used to pass the grouped column markdown through untyped, which
made every ML skill fall back to SQL and date-filtered questions ask for
clarification — `normalize_columns_markdown` in `src/metadata/mcp_catalog_client.py`
now rewrites it into the flat typed shape the planner reads.)

How it works:

- `live/globalSetup.js` probes the stack; when it is down every test skips
  itself instead of failing. `live/auth.setup.js` logs in once and saves the
  session (`.auth/live.json`, git-ignored).
- `live/_live.js` drives the app the way a user does (`window.ChatController.send`,
  the connection switcher, New conversation) and reads back the DOM; it also
  calls `/api/analysis/skills/prefs` to forget "don't ask again" so the confirm
  card always appears, and `/api/analysis/routing` as a precondition so a
  misrouted ML question fails with the router's reason rather than a timeout.
- `live/questions.js` is the question set. Expectations are **structural** —
  path, `Completed` status, a row-count range, SQL clauses (`GROUP BY`, no
  writes), which surfaces render, card sections, Model-details headings —
  never numbers from the data, because the LLM writes the SQL and picks the
  model.
- Tests run serially on one worker (one LLM conversation at a time, the
  analytics sandbox allows two concurrent runs) but are independent: a failing
  question never skips the rest. Infrastructure failures ("Backend
  unavailable", a dead API container) are reported as such, not as product
  regressions.
- Per-question screenshots land in `live-screenshots/`; failure traces and
  videos in `test-results-live/`.
