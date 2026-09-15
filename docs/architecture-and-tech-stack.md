# Architecture & Technology Stack

A complete breakdown of the architecture and technology stack of **Jeen Insights** —
a multi-connection, natural-language analytics application. (The repo folder is
`jeen-insight`; the legacy name is `venna_test3`.)

## High-level architecture

It's a **two-container system** orchestrated by Docker Compose, with a strict
internal auth boundary between them:

```
┌─────────────────────┐        ┌──────────────────────────┐
│  jeen-insights-ui   │ ─────▶ │   jeen-insights-api      │
│  Flask (gunicorn)   │  HTTP  │   FastAPI + LangGraph     │
│  :8501 (public)     │ +token │   :8000 (internal-only)   │
└─────────────────────┘        └────────────┬─────────────┘
                                             │
                                   LangGraph agent pipeline
                                             │
                          ┌──────────────────┴──────────────────┐
                          │ LLM (LangChain, multi-provider)      │
                          │ Shared metadata DB (curated schema)  │
                          │ Per-connection data sources (runtime)│
                          └──────────────────────────────────────┘
```

Key design point: the **UI is the only public surface** (`:8501`). The API is not
published to the host — the UI reaches it over the compose network, and the API
default-denies any request that lacks a valid signed token minted by the UI. See
the deliberate `expose` (not `ports`) in `docker-compose.yml` and the
`InternalAuthMiddleware` in `src/api/app_factory.py`.

## The two services

**1. UI layer** — `src/ui_app.py` (a single ~54 KB Flask app)

- Flask + Gunicorn (gthread worker) as the production WSGI server (`Dockerfile.ui`).
- Acts as a **proxy/BFF**: handles login/session, CSRF (flask-wtf), login
  rate-limiting (flask-limiter), then forwards requests to the FastAPI backend.
- It is the **sole issuer** of internal service tokens (see auth below).

**2. API layer** — `src/main.py` → `src/api/`

- FastAPI + Uvicorn.
- Assembled by a factory (`src/api/app_factory.py`) that wires ~14 routers under
  `src/api/routes/` (query, insights, charts, connections, connectors, history,
  health, settings, mcp, saved_analyses, etc.).
- `src/api/lifespan.py` handles startup: DB schema/prompt seeding and compiling the
  LangGraph graph. Shared services live in `src/api/state.py`.

## The agent (the core value)

The heart is a **LangGraph text-to-SQL state machine** built in
`src/agent/langgraph_agent/graph.py`. `build_graph()` wires ~18 nodes into a
`StateGraph`. The flow on every question:

1. **memory_shrink_check / memory_summarizer** — summarize conversation history if
   over token budget.
2. **fused_router** — classify intent: `needs_query`, `from_memory`,
   `out_of_scope`, `unsafe`, `greeting`.
3. **catalog_lookup** — load per-connection curated metadata (deny-by-default if no
   catalog).
4. **filter_planner / filter_grounder** — entity/value linking: verify literal
   filter values actually exist in columns (fuzzy match via `rapidfuzz`), ask for
   clarification if ambiguous.
5. **prompt_builder** — inject curated schema (with optional schema-linking/pruning
   for large catalogs).
6. **sql_generator** — LLM generates SQL (retries up to `max_retries`).
7. **sqlglot_validate** — parse + table-name allowlist + schema-qualifier check.
8. **dlp_check** — governance scan for sensitive columns.
9. **execute_query** — run via a `SqlRunner` (SELECT-only enforced).
10. **fused_eval_analytics** — summary, key findings, and 3–5 clickable follow-up
    questions.
11. **response_formatter → save_to_memory → observability_log**.

There's a `feedback_classifier` node that creates repair loops (bounded by a
recursion limit of 48). Each node is wrapped by `_timed()` to emit a live execution
trace for the UI's trace panel.

Notable extras:

- A **standalone insights eval subgraph** (`build_insights_eval_graph`) called
  directly by `/api/generate-insights`.
- A parallel **text-to-DAX agent** for Power BI (`src/agent/langgraph_agent_dax/`,
  `src/agent/dax_insights_agent.py`).
- The README says "no RAG / no embeddings" — curated metadata is injected directly
  into the prompt.

## LLM abstraction

`src/agent/llm_service.py` (`LangChainLlmService`) is a **multi-provider wrapper**
over LangChain:

- Azure OpenAI (default), OpenAI, Anthropic, Google Gemini, and any
  OpenAI-compatible endpoint.
- Supports a separate cheaper "router" deployment for cheap nodes
  (router/summarizer) vs. the big model (SQL gen, eval).
- Prompts are file-based (`src/agent/prompts/*.md`), seeded into an
  `insights_prompts` DB table at startup, editable live from the UI, with a
  DB-backed cache (`prompt_cache.py`).

## Data connectors

A pluggable connector layer under `src/connectors/`:

- `base.py` defines the abstract `SqlRunner` with all the **safety machinery
  centralized** as a template method: SELECT/WITH-only gate, structural
  single-statement enforcement (via sqlglot, so `SELECT 1; DELETE...` and
  DML-in-CTE are blocked), hard row cap, statement timeouts, and error/secret
  sanitization.
- `factory.py` registers concrete engines: **PostgreSQL** (`asyncpg`),
  **Trino/Presto**, **Databricks/Spark**.
- **Power BI** is deliberately *not* a `SqlRunner` — it's a delegated-OAuth REST
  source served by the DAX agent.
- There's also a broader **connector/integration platform**
  (`src/connectors/providers/`): Slack, Jira, Power BI, Microsoft Graph mail,
  Tavily — with OAuth, action gating/policy, audit, egress controls, and MCP
  support (`src/metadata/mcp_*`).

Two databases are involved: the **shared Jeen metadata DB** (curated metadata +
operational `insights_*` tables) and the **per-connection data sources** resolved at
runtime from `metadata_sources` / `settings_services`.

## Security model

Security is a strong theme (`src/security/` + config):

- **Internal auth boundary** (`internal_auth.py`): Flask mints short-lived (120s),
  audience-bound, HMAC-signed tokens (`itsdangerous`); FastAPI verifies them into a
  server-side `Principal`. Supports key rotation via `<kid>:<secret>` lists and
  fails closed in production.
- **Envelope encryption** (AES-GCM, `crypto.py` + `cryptography`) for connector
  secrets and per-user OAuth tokens.
- **DLP governance**, **read-only enforcement**, **deny-by-default catalog**, and
  **row/statement caps**, plus per-user concurrency governors.
- **Entra ID (Azure AD) SSO** via `msal`, group-based entitlements, and Power BI
  queried under the user's delegated grant (so row-level security applies).
- A `JEEN_DEV_MODE` flag toggles between a zero-config POC boot and hardened
  production.

## Conversation persistence

Conversations are first-class rows (`insights_conversations`, id == the
`session_id` every turn already carries), scoped to one user and one connection.
Each turn can own an `insights_turn_artifacts` row holding the restore payload:
an allowlisted slice of the formatted answer (answer, error, metrics, inline
findings/suggestions/followups — never prompts or schema context), an
all-or-nothing result snapshot under row/byte caps, and the server-built chart
baseline written by `/generate-chart` / `/edit-chart`.

- **Write path**: `log_query` upserts the conversation and allocates the
  sequence number under a row lock in one transaction; `save_to_memory` writes the
  artifact; chart routes attach the chart. Artifact writes are guarded upserts
  (`pruned` is terminal; only the rerun endpoint promotes a turn back to `stored`).
- **Read path**: `GET /api/conversations/last` returns turn metadata without rows;
  the UI fetches one turn's artifact on selection and renders it through the same
  `displayResults` + ChartManager path as a live answer (no LLM call when a
  chart baseline exists). Turns without rows offer "Load data", which re-executes
  the stored SQL/DAX through the protected runner / delegated Power BI token.
- **Retention** is count-based only and runs per user + connection as a detached
  background task spawned from `/last`: conversations beyond `KEEP_LAST` are
  hard-deleted (FK cascade), blobs beyond `KEEP_LAST_TURNS` are dropped. A DB claim
  row throttles it across replicas; tracked tasks are drained before the pool
  closes.
- **Identity**: every history / saved-analysis / chart / conversation route derives
  the user from the verified `Principal`; a `session_id` is only accepted when the
  conversation exists, is owned by the caller, and lives on the requested connection.

## Frontend

The UI is **vanilla JS, no framework** — a large `src/static/script.js` (~268 KB) +
`style.css` (~215 KB), plus modular controllers under `src/static/` (chart-feature,
chat, insights, profiling, settings, workspace). Charting uses vendored **ECharts**
and **D3**. Templates are server-rendered Jinja (`index.html`, `login.html`,
`setup.html`). There's also an optional OSM map layer with a server-side tile proxy
and geocoder.

## Technology stack summary

| Layer | Technology |
|---|---|
| Language / runtime | Python ≥ 3.11 |
| API | FastAPI, Uvicorn, Pydantic v2 / pydantic-settings |
| UI | Flask, Gunicorn, flask-wtf (CSRF), flask-limiter; vanilla JS + ECharts/D3 |
| Agent | LangGraph, LangChain (OpenAI/Anthropic/Google), SQLGlot, RapidFuzz |
| Databases / drivers | PostgreSQL (asyncpg, psycopg 3), Trino, Databricks SQL connector |
| Auth / security | MSAL (Entra ID), bcrypt, cryptography (AES-GCM), itsdangerous |
| Integrations | Slack, Jira, MS Graph, Tavily, Power BI (DAX), MCP |
| Data profiling | pandas, numpy, ydata-profiling, sweetviz |
| Packaging / infra | Docker + docker-compose, uv (`uv.lock`), pytest (unit + opt-in integration) |

## Project layout at a glance

```
src/
├── api/         FastAPI app, routes, lifespan, middleware, chart builder, maps
├── agent/       LangGraph pipeline, LLM service, prompts, DAX agent, profiling
├── connectors/  SqlRunner engines + integration providers (Slack/Jira/PowerBI…)
├── metadata/    curated metadata loader, schema linker, value index, MCP
├── connections/ connection resolution service
├── security/    internal auth, crypto, feature flags
├── tools/       sql_tool / dax_tool
├── static/      vanilla-JS frontend + ECharts/D3
└── ui_app.py    Flask UI/proxy (public entrypoint)
```
