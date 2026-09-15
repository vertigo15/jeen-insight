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
