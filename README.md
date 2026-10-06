# Jeen Insights

A multi-connection, natural-language analytics application. Jeen Insights reads
curated metadata (tables, columns, relationships, business terms, knowledge
pairs) directly from the shared Jeen metadata database and lets a user ask
questions in plain language against any registered data connection.

> **Proprietary.** Jeen Insights is a private product of Jeen Ltd. See
> [`LICENSE`](LICENSE). Do not publish or redistribute this repository.

Documentation: [feature overview](docs/features.md) ·
[architecture](docs/architecture-and-tech-stack.md) ·
[agent flow](docs/agent-state-flow.md) · [ML skills](docs/ml-skills.md) ·
[deployment](deployment/README.md) · [tests](tests/README.md).

## Features

A one-page list of every feature is in [`docs/features.md`](docs/features.md).
Highlights:

- 🤖 **LangGraph text-to-SQL pipeline** — a 28-node graph (context → router →
  catalog → filter grounding → SQL gen → validation → execution → eval → output)
  with separate repair and semantic retry budgets, conversation memory, ML-skill
  and catalog-help branches. A sibling graph answers Power BI questions with DAX.
  Node reference: [`docs/agent-state-flow.md`](docs/agent-state-flow.md).
- 🧪 **ML analysis skills** — 12 validated statistical/ML skills run in an isolated
  analytics sandbox ([`docs/ml-skills.md`](docs/ml-skills.md)).
- 🌐 **Multi-provider LLM** — Azure OpenAI (default), OpenAI, Anthropic, Google
  Gemini, and any OpenAI-compatible endpoint via `LangChainLlmService`.
- ✨ **Insights & follow-up questions** — post-execution eval node generates a
  summary, key findings, and 3–5 clickable follow-up questions via the
  `fused_eval_analytics` LangGraph node.
- 🔌 **Multi-connection** — pick any active connection from
  `public.metadata_sources`. Each connection has its own curated metadata.
- 📚 **No RAG / no embeddings** — curated metadata from Schema Modeler is
  injected directly into the system prompt at every turn.
- 🐘 **Shared Jeen metadata DB** — writes only to tables with the `insights_`
  prefix; reads from `metadata_*` / `knowledge_pairs`.
- 📊 **Admin analytics** — Settings › Analytics (admin role only): DAU/WAU/MAU,
  questions per day, top users and connections, success and failure breakdown,
  ML-skill usage and the thumbs / ratings / comments feed. Backed by the durable
  usage ledger `insights_usage_events` (migration 036), so numbers survive
  conversation retention. Metric definitions are shown as tooltips; see
  `deployment/migrations.md` for grants, retention and privacy notes.
- 🐳 **Docker-first** — `docker compose up -d --build` brings up the UI, the API
  and the analytics sandbox. Kubernetes (AKS, EKS, OpenShift, Argo CD) is
  documented in [`deployment/`](deployment/README.md).

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                               Docker Compose                              │
│  ┌──────────────────┐  signed   ┌─────────────────────┐                   │
│  │ jeen-insights-ui │  token    │  jeen-insights-api  │                   │
│  │ Flask / Gunicorn │──────────▶│  FastAPI + LangGraph │                   │
│  │ public  :8501    │           │  internal   :8001    │                   │
│  └──────────────────┘           └───────┬───────┬──────┘                   │
│                                         │       │                          │
│                      ┌──────────────────▼─┐   ┌─▼───────────────────────┐ │
│                      │ LangGraph pipeline │   │ jeen-insights-analytics │ │
│                      │ 28 nodes (SQL);    │   │ ML sandbox  :8100       │ │
│                      │ sibling graph (DAX)│   │ internal, no egress     │ │
│                      └──────────┬─────────┘   └─────────────────────────┘ │
│                                 │                                          │
│  ┌──────────────────────────────▼──────────────────────────────────────┐ │
│  │ LangChainLlmService (Azure OpenAI / OpenAI / Anthropic / Google)     │ │
│  │ Shared metadata DB — curated metadata + insights_* tables            │ │
│  │ Per-connection data sources: Postgres, Trino, Databricks, Power BI   │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

More detail: [`docs/architecture-and-tech-stack.md`](docs/architecture-and-tech-stack.md).

## Quick Start

### Prerequisites

- Docker Desktop
- Azure OpenAI API access
- A Jeen metadata DB with at least one row in `public.metadata_sources` and
  the curated `metadata_tables` / `metadata_columns` / `metadata_relationships`
  / `knowledge_pairs` / `metadata_business_terms` rows for that connection
  (use Schema Modeler to set them up).

### 1. Configure environment

Copy `.env` and set:

```env
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<your-aoai>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-5.1

METADATA_DB_HOST=jeen-pg-dev-weu.postgres.database.azure.com
METADATA_DB_PORT=5432
METADATA_DB_NAME=jeen_data_metadata_dev
METADATA_DB_USER=jeen_pg_dev_admin
METADATA_DB_PASSWORD=<rotate>
METADATA_DB_SSL=true
```

### 2. Start the stack

```bash
docker-compose up -d --build
```

### 3. Apply the operational migrations (once)

```bash
docker exec jeen-insights-api python scripts/run_insights_migrations.py
```

This creates `insights_conversation_sessions`, `insights_query_insights`,
`insights_pinned_questions`, plus helpers and views. Later revisions also
alter existing Insights-owned tables (e.g. `022_conversations_and_turn_artifacts`
adds a parent foreign key to `insights_conversation_sessions` and re-keys legacy
cross-connection session ids). The runner records each revision once and applies
it in a transaction; it never touches the shared `metadata_*` tables. In
Kubernetes the same migrations run as an explicit Job, see
[`deployment/migrations.md`](deployment/migrations.md).

Run the migrations **before** rolling out a build that depends on them. If
migration 022 is missing, the API starts anyway and logs
`conversation persistence is DISABLED for this process`; questions still work,
but conversations are not restored on reload until the migration is applied.

### 4. Open the UI

http://localhost:8501

Pick a connection from the dropdown in the top bar and ask a question.

## API surface

| Method | Endpoint                                          | Notes                                                  |
|--------|---------------------------------------------------|--------------------------------------------------------|
| GET    | `/api/connections`                                | List active connections (no secrets).                  |
| GET    | `/api/connections/{source_key}`                   | Connection details + metadata row counts.              |
| POST   | `/api/connections/{source_key}/refresh-metadata`  | Invalidate the metadata loader cache for a source.     |
| POST   | `/api/connections/{source_key}/warm-cache`        | Pre-load the catalog cache for a source.               |
| POST   | `/api/query`, `/api/query/stream`                 | Body: `{question, connection, session_id?}`; the stream variant sends the table first, then insights. |
| GET    | `/api/tables`, `/api/tables-rich`                 | List tables on the chosen data source (`connection=<source_key>`). |
| GET    | `/api/schema/{table}?connection=<source_key>`     | Column-level schema.                                   |
| POST   | `/api/generate-insights` (+ `/stream`)            | Body must include `connection` + `dataset` + `question`; `sql` runs the eval subgraph. |
| POST   | `/api/empty-result-hint`, `/api/generate-profile` | Empty-result diagnosis; data profile of a result.      |
| POST   | `/api/generate-chart`, `/api/enhance-chart`, `/api/edit-chart` (+ `/rebuild`) | Chart build, AI enhance and plain-language edit; `connection` is required. |
| GET    | `/api/chart-capabilities`, `/api/map-tiles/…`, `/api/map-search` | Chart options; same-origin map tile proxy and geocoder. |
| POST   | `/api/feedback`                                   | Records `thumbs_up` / `thumbs_down` / `edited` / `catalog_gap` (+ optional `notes`). |
| POST   | `/api/answer-feedback`                            | Rating and comment on one answer.                      |
| POST   | `/api/analysis/run`, `/rerun`, `/chart`, `/forecast/accuracy` | Run or re-run an ML skill, chart it, check a forecast against actuals. |
| GET/POST | `/api/analysis/suggestions`, `/routing`, `/skills`, `/skills/prefs` | Skill catalog, routing hints and per-user consent.     |
| GET/POST/PATCH/DELETE | `/api/saved-analyses[/{id}]`       | Saved analyses with chart state.                       |
| GET/POST | `/api/knowledge-questions`, `/api/knowledge-columns`, `/api/suggest-questions` | Knowledge-pair question and column suggestions. |
| GET/PATCH | `/api/user/onboarding`                         | First-time experience progress.                        |
| GET/PUT/POST | `/api/settings/prompts…`, `/models…`, `/runtime`, `/app-info` | Admin: prompts (versions, restore), model catalog and health, runtime limits. |
| GET/POST/PUT/DELETE | `/api/mcp/…`                         | Admin: MCP servers, catalog source, cache TTL.         |
| GET/POST/PUT/PATCH/DELETE | `/api/connectors/…`        | Admin: integrations, secrets, group roles, audit.      |
| GET/POST | `/api/me/connections/…`, `/api/actions/…`       | Per-user OAuth linking; propose, preview, execute and continue a gated action. |
| GET    | `/api/admin/analytics/{overview,timeseries,top-users,top-connections,feedback,analysis,errors,runs,runs/{query_id}}?days=7\|30\|90` | Admin only. Usage / quality reports from the durable ledger; `feedback` reads are audited. 503 until migration 036 is applied. |
| GET    | `/api/conversation/{session_id}`                  | Legacy raw dump of one conversation (superseded below).|
| GET    | `/api/conversations/last?connection=`             | Hydration payload for the user's newest conversation on a connection; spawns the retention prune. |
| GET    | `/api/conversations?connection=` or `?all=true`   | Cursor-paged list of the user's conversations (`all` includes removed connections). |
| GET    | `/api/conversations/{id}`                         | Header + paged turn metadata (`before=<sequence_number>`), no rows. |
| GET    | `/api/conversations/{id}/turns/{turn_id}/artifact`| Result snapshot + chart baseline for one turn.         |
| POST   | `/api/conversations/{id}/turns/{turn_id}/rerun`   | Re-execute the stored SQL/DAX (no LLM); refreshes the snapshot. |
| PATCH / DELETE | `/api/conversations/{id}`                 | Rename / hard-delete (cascades to turns, insights, artifacts). |
| GET    | `/api/conversations/favorites`                    | The user's favourite answers.                          |
| PUT / DELETE | `/api/conversations/{id}/turns/{turn_id}/favorite` | Star / unstar an answer.                          |
| GET/POST | `/api/user/recent-questions`, `pinned-questions`, `pin-question`, `unpin-question`, `history-log` | Per-(user, connection) history. |
| GET    | `/health`                                         | Liveness; the API is otherwise reachable only through the UI. |

The UI adds `/api/ask`, `/api/ask/stream` (proxied to `/api/query`) and the
account endpoints `/api/users`, `/api/users/{id}/role` for Settings › Users.
The table lists the API's route groups; the route modules under
`src/api/routes/` are the full reference.

Every endpoint that operates on a dataset requires the `connection` parameter
(the `source_key` from `metadata_sources`). Requests without it return 400.

Identity for every user-scoped endpoint comes from the internal token minted by
the Flask UI (`request.state.principal`); `user_id` fields in bodies or query
strings are accepted for backward compatibility but never trusted.

### Conversation persistence

When the UI opens (or the connection is switched) it restores the user's last
conversation on that connection: every turn's question, answer, SQL, inline
analytics and, for table answers, the result rows and the server-built chart.
The **History** tab lists previous conversations (open / rename / delete); a
conversation whose connection was removed opens read-only.

Storage is bounded per user + connection by count, never by age:

| Env var | Default | Meaning |
|---------|---------|---------|
| `CONVERSATION_PERSISTENCE_ENABLED` | `true` | Kill switch for capture + prune (forced off if migration 022 is missing). |
| `CONVERSATION_SNAPSHOT_MAX_ROWS` | `2000` | Rows kept per turn; above it the turn restores through a re-run ("Load data"). |
| `CONVERSATION_SNAPSHOT_MAX_BYTES` | `1048576` | Byte cap for one turn's rows envelope. |
| `CONVERSATION_CHART_MAX_BYTES` | `524288` | Byte cap for the persisted chart spec + config. |
| `CONVERSATION_KEEP_LAST` | `30` | Conversations kept per user + connection; older ones are deleted. |
| `CONVERSATION_SNAPSHOT_KEEP_LAST_TURNS` | `100` | Most recent turns that keep rows + chart; older turns keep text only. |
| `CONVERSATION_RETENTION_ON_OPEN` | `true` | Run the prune as a background task when the app is opened. |
| `CONVERSATION_RETENTION_MIN_INTERVAL_SECONDS` | `600` | Throttle per user + connection (in-memory debounce + DB claim row, so it holds across replicas). |
| `CONVERSATION_MAX_TURNS_HYDRATED` | `50` | Turns per hydration page. |

Worst case at the defaults is about 150 MiB of JSON and 200k stored rows per
user + connection. Only an explicit allowlist of the answer payload is stored
(never prompts or schema context). Not in this version, tracked as follow-ups:
encryption at rest for snapshots, LLM-generated titles, persisting client-side
chart quick-toggles and table formatting.

## What the agent does on every question

1. **Context** — build the conversation-memory ledger from the last turns.
2. **Router** — classify intent: `needs_query`, `needs_analysis` (ML skill),
   `from_memory`, `history_lookup`, `capability`, `catalog_help`,
   `out_of_scope`, `unsafe`, `greeting`.
3. **Catalog lookup** — load per-connection metadata bundle from `metadata_*`.
4. **Filter grounding** — bind literals in the question to real column values.
5. **SQL generator** — call the LLM with the curated schema; a focused
   `sql_repair` step fixes errors, with separate budgets for execution errors
   and semantic mismatches.
6. **Validation** — SQLGlot parse + table-name check + DLP governance scan.
7. **Execution** — run SQL through the connection's `SqlRunner` (Postgres, Trino,
   Databricks; SELECT-only enforcement).
8. **Eval** (`fused_eval_analytics`) — check intent match, summarise results,
   generate 3–5 follow-up questions shown as clickable chips in the UI.
9. **Output** — format response, save to memory and the usage ledger, write the
   observability trace.

ML-skill questions take the analysis branch (planner → guard → SQL → sandboxed
run); Power BI connections use the sibling text-to-DAX graph
([`docs/agent-state-flow-dax.md`](docs/agent-state-flow-dax.md)). The full node
and arc reference is [`docs/agent-state-flow.md`](docs/agent-state-flow.md).

When the `POST /api/generate-insights` endpoint receives a `sql` field, it
invokes the eval node directly as a standalone subgraph (bypassing the full
pipeline). Results without SQL fall back to the legacy `insight_service` path.

## Project layout

```
jeen-insight/
├── docker-compose.yml              ui (:8501) + api (:8001) + analytics sandbox (:8100)
├── Dockerfile / .ui / .analytics   one image per service
├── .env / .env.example             METADATA_DB_*, LLM and identity settings
├── LICENSE                         Jeen Ltd proprietary license
├── requirements*.txt, pyproject.toml
├── db/migrations/insights/         ordered SQL migrations (run as an explicit step)
├── deployment/                     Helm chart, AKS/EKS/OpenShift/Argo CD guides, validators
├── docs/                           feature overview, architecture, agent flows, ML skills
├── evals/                          offline golden sets and the eval harness
├── scripts/                        migration runner, e2e DB helper, diagram generator
├── src/
│   ├── api/                        FastAPI factory, lifespan, middleware, chart builder,
│   │                               maps, routes/ (query, insights, charts, conversations,
│   │                               analysis, settings, mcp, connectors, …)
│   ├── agent/
│   │   ├── jeen_insights_agent.py  SQL agent (wraps the LangGraph pipeline)
│   │   ├── dax_insights_agent.py   Power BI text-to-DAX agent
│   │   ├── langgraph_agent/        graph.py (28 nodes), state.py, nodes/, prompt_loader.py
│   │   ├── langgraph_agent_dax/    DAX graph and nodes
│   │   ├── llm_service.py          LangChainLlmService (multi-provider, fallback)
│   │   ├── prompt_cache.py         DB-backed lazy cache for insights_prompts
│   │   ├── prompts/, prompts_dax/  *.md prompt templates (seeded into DB)
│   │   └── …                       conversation history, memory, onboarding, profiling
│   ├── analysis/                   ML skill contracts, guards, engines, runner
│   ├── analytics/                  durable usage ledger
│   ├── analytics_service/          ML sandbox service
│   ├── connections/, metadata/     connection resolution, curated catalog, MCP
│   ├── connectors/                 SqlRunner engines + integration providers
│   ├── security/                   internal auth, encryption, feature flags
│   ├── i18n/                       English and Hebrew message catalogs
│   ├── tools/                      sql_tool, dax_tool
│   ├── ui_app.py                   Flask UI, login and proxy to the API
│   └── static/, templates/         vanilla-JS frontend and Jinja templates
├── templates/insight_prompt.txt    legacy insights prompt
└── tests/
    ├── unit/                       fast, offline (default pytest run)
    ├── integration/                requires live services (opt-in)
    ├── e2e/                        mocked-UI specs and the live suite (Playwright)
    ├── js/                         frontend unit tests
    └── stress/                     multi-user accuracy and speed runner
```

## Development

### Running tests

```bash
# Unit tests (fast, no live services)
python3 -m pytest tests/unit/ -q

# Integration tests (requires running stack + browser driver)
python3 -m pytest tests/integration/ -m integration
```

Unit tests run entirely offline — all LLM and DB calls are mocked. They cover
individual LangGraph nodes, graph flow paths, route handlers, and LLM JSON
parsing. The other test types (offline evals, mocked-UI e2e, live suite, stress)
are described in [`tests/README.md`](tests/README.md).

### Prompt management

All LLM prompts live in `src/agent/prompts/*.md` and `templates/insight_prompt.txt`.
On startup, `lifespan._seed_prompts` syncs every registered prompt to the
`insights_prompts` DB table:

- **New prompts** are inserted automatically.
- **Default (non-custom) prompts** are refreshed if the source file has
  changed — so editing a `.md` file and restarting the container is enough.
- **Custom (user-edited) prompts** are never overwritten.

To edit a prompt live: Settings → AI Agent → pick the prompt → edit → Save.

### Merge conflict resolution

This codebase spans two long-running concerns that touch overlapping files:

| Concern | Key files |
|---|---|
| LLM service abstraction | `llm_service.py`, all `langgraph_agent/nodes/*.py`, `jeen_insights_agent.py` |
| LangGraph pipeline | `langgraph_agent/graph.py`, `state.py`, `nodes/*` |

When merging branches that touch either concern, follow this checklist:

**Before committing the merge:**

```bash
# 1. Confirm no old class name survives anywhere
grep -r "AzureOpenAILlmService" src/ --include="*.py"

# 2. Confirm no conflict markers were accidentally committed
grep -rn "<<<<<<\|=======\|>>>>>>>" src/ templates/

# 3. Run the full unit suite — catches import errors immediately
python3 -m pytest tests/unit/ -q

# 4. Smoke-test the live API
curl -s http://localhost:8001/health
curl -s -X POST http://localhost:8001/api/generate-insights \
  -H 'Content-Type: application/json' \
  -d '{"connection":"<your_conn>","question":"test","sql":"SELECT 1",
       "dataset":{"columns":["n"],"rows":[[1]]}}' | python3 -m json.tool
```

**Decision guide for common conflicts:**

| File | Strategy |
|---|---|
| `llm_service.py` | Keep **ours** — `LangChainLlmService` is the target interface |
| `lifespan.py` | Keep **ours** — has `_seed_prompts`, `LangGraph` init, `LangChain` startup |
| `langgraph_agent/nodes/*.py` | Keep **theirs** (pipeline) then grep+fix `AzureOpenAILlmService` → `LangChainLlmService` |
| `langgraph_agent/graph.py` | Keep **theirs** (full pipeline) then append `build_insights_eval_graph` / `run_eval` |
| `langgraph_agent/state.py` | Keep **theirs** (`AgentState`) then append `InsightsState` |
| `langgraph_agent/__init__.py` | **Merge** — export both full pipeline and subgraph helpers |
| Prompt `.md` files | Keep **ours** — we own the updated output schemas |

**Why the grep matters:** files accepted wholesale from the other branch
(e.g. `--theirs`) can silently import a class that no longer exists in our
`llm_service.py`. The unit test suite will catch this on the very first run,
but the grep lets you fix it before committing.

### Rebuilding the Docker image

After adding a new Python dependency to `requirements.txt`:

```bash
docker compose build jeen-insights-api
docker compose up -d jeen-insights-api
```

For a quick dependency install without a full rebuild (testing only):

```bash
docker exec jeen-insights-api pip install <package>
docker compose restart jeen-insights-api
```

## Roadmap

- Connection types beyond Postgres (currently returns 501 for Snowflake /
  PowerBI / etc.).
- Encrypted secrets at rest in `metadata_sources`.
- Streaming support for the LangGraph eval node (currently a single non-streaming
  LLM call; the legacy `insight_service` path already streams).

## License

Proprietary. Copyright (c) 2026 Jeen Ltd. All rights reserved. This is a
private product; see [`LICENSE`](LICENSE).
