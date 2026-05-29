# Jeen Insights

A multi-connection, natural-language analytics application. Jeen Insights reads
curated metadata (tables, columns, relationships, business terms, knowledge
pairs) directly from the shared Jeen metadata database and lets a user ask
questions in plain language against any registered data connection.

> **Note:** the repository directory is still named `venna_test3` for legacy
> reasons; the application itself, container names, and all user-facing
> branding are **Jeen Insights**.

## Features

- 🤖 **LangGraph text-to-SQL pipeline** — 16-node graph (router → catalog → SQL gen
  → validation → execution → eval → output) with retry logic and memory.
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
- 🐳 **Docker-first** — `docker compose up -d --build` brings up API + UI.

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                             Docker Compose                                 │
│  ┌──────────────────┐          ┌─────────────────────┐                    │
│  │ jeen-insights-ui │─────────▶│  jeen-insights-api  │                    │
│  │   (Flask UI)     │          │  (FastAPI + LangGraph│                    │
│  │   :8501          │          │   agent)  :8001      │                    │
│  └──────────────────┘          └──────────┬──────────┘                    │
│                                            │                              │
│                               ┌────────────▼────────────┐                │
│                               │   LangGraph pipeline     │                │
│                               │ memory → router →        │                │
│                               │ catalog → sql_gen →      │                │
│                               │ validate → execute →     │                │
│                               │ eval (follow-ups) →      │                │
│                               │ format → save → log      │                │
│                               └────────────┬────────────┘                │
│                                            │                              │
│  ┌─────────────────────────────────────────▼──────────────────────────┐  │
│  │  LangChainLlmService (Azure OpenAI / OpenAI / Anthropic / Google)  │  │
│  │  Shared metadata DB  — curated metadata + insights_* tables        │  │
│  │  Per-connection PostgreSQL data sources (resolved at runtime)       │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────┘
```

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
`insights_pinned_questions`, plus helpers and views. All migrations are
idempotent and only add **new** tables — they never touch existing ones.

### 4. Open the UI

http://localhost:8501

Pick a connection from the dropdown in the top bar and ask a question.

## API surface

| Method | Endpoint                                          | Notes                                                  |
|--------|---------------------------------------------------|--------------------------------------------------------|
| GET    | `/api/connections`                                | List active connections (no secrets).                  |
| GET    | `/api/connections/{source_key}`                   | Connection details + metadata row counts.              |
| POST   | `/api/connections/{source_key}/refresh-metadata`  | Invalidate the metadata loader cache for a source.     |
| POST   | `/api/query`                                      | Body: `{question, connection, session_id?}`.           |
| GET    | `/api/tables?connection=<source_key>`             | List tables on the chosen data source.                 |
| GET    | `/api/schema/{table}?connection=<source_key>`     | Column-level schema.                                   |
| POST   | `/api/generate-insights`                          | Body must include `connection` + `dataset` + `question`.|
| POST   | `/api/generate-chart` / `/api/enhance-chart`      | Same: `connection` is required.                        |
| POST   | `/api/feedback`                                   | Records `thumbs_up` / `thumbs_down` / `edited`.        |
| GET    | `/api/conversation/{session_id}`                  | Conversation history with insights.                    |
| GET/POST | `/api/user/recent-questions` / `…/pin-question` | Per-(user, connection) history.                        |

Every endpoint that operates on a dataset requires the `connection` parameter
(the `source_key` from `metadata_sources`). Requests without it return 400.

## What the agent does on every question

1. **Memory check** — summarise conversation history if over token budget.
2. **Router** — classify intent: `needs_query`, `from_memory`, `out_of_scope`, `unsafe`.
3. **Catalog lookup** — load per-connection metadata bundle from `metadata_*`.
4. **SQL generator** — call the LLM with curated schema; retry on error (up to 3×).
5. **Validation** — SQLGlot parse + table-name check + DLP governance scan.
6. **Execution** — run SQL via `PostgresSqlRunner` (SELECT-only enforcement).
7. **Eval** (`fused_eval_analytics`) — check intent match, summarise results,
   generate 3–5 follow-up questions shown as clickable chips in the UI.
8. **Output** — format response, save to memory, write observability trace.

When the `POST /api/generate-insights` endpoint receives a `sql` field, it
invokes the eval node directly as a standalone subgraph (bypassing the full
pipeline). Results without SQL fall back to the legacy `insight_service` path.

## Project layout

```
jeen-insight/
├── docker-compose.yml              jeen-insights-api (:8001) + jeen-insights-ui (:8501)
├── Dockerfile / Dockerfile.ui
├── .env                            METADATA_DB_* + AZURE_OPENAI_*
├── requirements.txt                includes langgraph + langchain-*
├── pyproject.toml                  pytest config (unit tests default; integration opt-in)
├── src/
│   ├── api/
│   │   ├── app_factory.py
│   │   ├── lifespan.py             startup: schema, prompt seeding, graph compile
│   │   ├── state.py                module-level shared services
│   │   ├── models.py               Pydantic request/response schemas
│   │   └── routes/                 query, insights, charts, settings, health …
│   ├── agent/
│   │   ├── jeen_insights_agent.py  JeenInsightsAgent (wraps LangGraph pipeline)
│   │   ├── langgraph_agent/
│   │   │   ├── graph.py            build_graph (16-node) + build_insights_eval_graph
│   │   │   ├── state.py            AgentState + InsightsState TypedDicts
│   │   │   ├── prompt_loader.py    file-based prompt templates
│   │   │   └── nodes/              catalog, eval, execution, feedback, memory,
│   │   │                           output, router, sql_gen, validation
│   │   ├── llm_service.py          LangChainLlmService (multi-provider)
│   │   ├── prompt_cache.py         DB-backed lazy cache for insights_prompts
│   │   ├── insight_service.py      legacy insights path (no SQL context)
│   │   └── prompts/                *.md prompt templates (seeded into DB)
│   ├── connections/
│   ├── metadata/
│   ├── tools/sql_tool.py
│   ├── ui_app.py                   Flask UI proxy
│   └── static/                     script.js, style.css, chart-feature, insights/
├── templates/insight_prompt.txt    legacy insights prompt
└── tests/
    ├── unit/                       fast, offline (default pytest run)
    └── integration/                requires live services (opt-in)
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
parsing.

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

MIT
