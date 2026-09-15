# ML Skills — handoff package

**Scope: the ML skills feature only.** Nothing in this package changes onboarding, the landing page,
or any existing screen beyond the numbered additions in the answer pane.

## Read in this order

| File | What it is |
|---|---|
| `README.md` | **Start here. The spec.** Decisions, contracts, guards, SQL strategy, agent changes, sandbox, UI, copy rules. Source of truth for values and rules. |
| `CURSOR.md` | The phased work order (P0–P6) with one prompt per phase and a review checklist. |
| `1-skills-handoff.html` | The complete visual mock: pipeline, contract, catalog, result shapes, guards, build order. |
| `2-answer-anatomy.html` | The three answer-pane states in the real workspace layout. The UI to implement. |
| `3-skill-states.html` | Routing, result card, re-run drawer, clustering, guard refusal. |

The HTML files are self-contained (fonts and images inlined) and open offline. They are the source
of truth for **look and copy**; where they disagree with `README.md` on a value or a rule, the
README wins (see "Reconciliation" in the README for the known differences).

## The three decisions already made

1. **No ML pushdown.** Every skill runs in Python on aggregates pulled from the connection through
   the existing `SqlRunner`.
2. **No new result kind.** An ML answer is an ordinary result object with extra columns and a
   different default chart, so Save, Send, snapshot, export and pinning work unchanged.
3. **No LLM does the math.** The model picks the skill and fills parameters. It never generates
   analysis code and never computes a number.

If a diff re-opens one of these, send it back.

## Where the work lands

| Area | Files |
|---|---|
| Contracts, guards, engines, runner | `src/analysis/` |
| Portable aggregation SQL | `src/analysis/sql_builder.py` (sqlglot AST, one builder for every registered dialect) |
| Routing | `src/agent/langgraph_agent/nodes/router.py` — `needs_analysis` |
| Planner | `src/agent/analysis_planner.py` |
| Graph branch | `src/agent/langgraph_agent/graph.py` + `nodes/analysis.py` — planner, guard, sql, run |
| Narration | `src/agent/langgraph_agent/nodes/eval.py` narration mode + `src/agent/prompts/analysis_narration.md` |
| API | `src/api/routes/analysis.py` — `/api/analysis/run`, `/api/analysis/rerun`, skill prefs |
| Persistence | `db/migrations/insights/023_ml_skills.sql`, `src/agent/conversation_artifacts.py`, `src/agent/analysis_store.py` |
| Chart | `src/api/chart_builder.py` — one new `band` type |
| Answer UI | `src/static/workspace/workspaceController.js`, `src/static/chart-feature/components/ChartChat.js`, `src/static/analysis/` |
| Sandbox | `docker-compose.yml` + `jeen-insights-analytics` service (`Dockerfile.analytics`, `src/analytics_service/`) |
