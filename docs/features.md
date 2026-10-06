# Jeen Insights — feature overview

Jeen Insights is a private product of Jeen Ltd. It lets people ask business
questions in plain language against governed data connections and get back an
answer, the rows, a chart, insights and the evidence behind them.

This page lists every shipped feature in one place. Each row points to the
document that goes deeper. Defaults and variables are in
[`deployment/configuration.md`](../deployment/configuration.md).

## Ask and answer

| Feature | What it does | More |
|---|---|---|
| Natural-language questions | Text-to-SQL for Postgres, Trino/Presto and Databricks; text-to-DAX for Power BI datasets. Read-only, one statement, row and time capped. | [question-to-answer-flow](question-to-answer-flow.md), [agent-state-flow-dax](agent-state-flow-dax.md) |
| Smart routing | One router sends a question to a query, an ML skill, a memory answer, a History search, a capability or catalog-help answer, or a polite refusal (out of scope, unsafe, greeting). | [agent-state-flow](agent-state-flow.md) |
| Curated catalog, no RAG | Tables, columns, joins, business terms and verified question/SQL pairs from the Jeen Data semantic layer go straight into the prompt. Deny by default when a connection has no catalog. | [architecture](architecture-and-tech-stack.md) |
| Filter grounding | Resolves typed values to real column values (typo fixes, "any of these fields", clarification buttons), remembers choices per user and connection, never probes sensitive columns. Shown as "Filtered by" chips. | [architecture](architecture-and-tech-stack.md) |
| Streaming answers | The table appears first, then insights and the chart. Live progress and an execution trace panel. | [agent-state-flow](agent-state-flow.md) |
| Self-repair | Separate retry budgets for SQL/DAX errors and semantic mismatches; a failed retry restores the first answer; low-confidence answers are flagged. | [agent-state-flow](agent-state-flow.md) |
| Insights and follow-ups | Summary, key findings, and 3-5 clickable follow-up questions on every result; empty-result diagnosis; autocomplete from knowledge pairs and columns. | [README](../README.md) |

## Conversation

| Feature | What it does | More |
|---|---|---|
| Conversation memory | Ask about earlier answers and their data ("the top 4 from the previous answer"); computed over stored rows, never sent to the model. | [agent-state-flow](agent-state-flow.md) |
| Persistent conversations | Restored on reopen with answer, SQL, rows and chart; bounded per user and connection by count. | [README](../README.md) |
| History | List, search, open, rename and delete conversations; "did I ask about X last week?" is answered from the log. | [README](../README.md) |
| Favorites, pins, saved analyses | Star an answer, pin a question, save an analysis with its chart state. | [README](../README.md) |
| Feedback | Thumbs, ratings, comments, "catalog gap" reports on answers. | [README](../README.md) |
| Re-run | Re-execute a stored query without the LLM to refresh a snapshot. | [README](../README.md) |

## Charts and tables

| Feature | What it does | More |
|---|---|---|
| Auto charts | Instant chart-type detection and ECharts rendering; optional AI enhancement. | [chart-feature](../src/static/chart-feature/README.md) |
| Chart chat | Edit a chart in plain language (type, colours, formats, what-if scenarios); validated before it is applied. | [chart-feature](../src/static/chart-feature/README.md) |
| Maps | Optional OpenStreetMap layers with a server-side tile proxy and geocoder, so keys never reach the browser. | [configuration](../deployment/configuration.md) |
| Table tools | Sort, column formatting, export to Excel or CSV (loaded rows only). | [README](../README.md) |
| Data profiling | Generate a profile report for a result. | [README](../README.md) |

## Advanced analytics (ML skills)

12 validated statistical and ML skills (forecast, anomaly detection,
changepoint, seasonality, correlation, contribution, clustering, driver
analysis, regression, classification, cohort retention, A/B test) run in an
isolated sandbox with a confirm card, guards, egress tiers, model details,
adjustments to try and forecast-vs-actual tracking. The model picks the skill;
it never computes a number. See [ml-skills](ml-skills.md).

## Connections and integrations

| Feature | What it does | More |
|---|---|---|
| Multiple connections | Pick any active connection from the shared metadata DB; each has its own catalog. | [README](../README.md) |
| Trino mTLS | Client-certificate login when Schema Modeler saves an mTLS connection. | [trino-mtls](trino-mtls.md) |
| Power BI | Per-user delegated OAuth, so row-level security applies; measures-first DAX. | [agent-state-flow-dax](agent-state-flow-dax.md) |
| Connectors | Slack, Jira, Microsoft Graph mail, Tavily and Power BI providers with OAuth, group roles, action gating (propose, preview, execute), audit, egress and rate limits. Admin-enabled. | [architecture](architecture-and-tech-stack.md) |
| MCP servers | Register MCP servers, choose the catalog source, cached with TTLs, health checks and a tool tester. | [architecture](architecture-and-tech-stack.md) |

## Administration

| Feature | What it does | More |
|---|---|---|
| Settings | General, Metadata catalog, AI models, Query safety, My connections, Integrations, Users, Analytics, Prompts, About. Admin sections are gated by role. | [README](../README.md) |
| AI models | Shared model catalog with health checks, an active model, a cheaper router model and automatic fallback on transient errors. Providers: Azure OpenAI, OpenAI, Anthropic, Google, any OpenAI-compatible endpoint. | [architecture](architecture-and-tech-stack.md) |
| Prompt management | Every prompt is editable live, versioned and restorable; edited prompts are never overwritten by a release. | [PROMPTS](../PROMPTS.md) |
| Runtime settings | Admins change the statement timeout, max result rows, conversation context window and filter-grounding controls without a redeploy. | [configuration](../deployment/configuration.md) |
| Usage analytics | DAU/WAU/MAU, top users and connections, success and failure, skill usage, feedback feed, and per-run drill-down on a durable ledger. | [migrations](../deployment/migrations.md) |
| Users and roles | Local accounts, first-admin setup, separate Insights and Metadata roles (admin, editor, viewer). | [local-users](local-users.md) |
| Single sign-on | Microsoft Entra ID and OIDC (Keycloak, Zitadel). | [oidc](../deployment/oidc.md) |

## Security and governance

- Internal signed tokens between UI and API; the API is never public.
- SELECT-only, single statement, statement timeout and row caps in every runner.
- DLP scan of governed columns, sensitive-column hiding, deny by default catalog.
- Envelope encryption (AES-GCM) for connector secrets and user OAuth tokens.
- CSRF, login rate limits, per-user concurrency limits, audit of admin reads.
- The ML sandbox has no egress and a read-only filesystem.

## Experience

| Feature | What it does | More |
|---|---|---|
| English and Hebrew | Full UI catalogs, right-to-left layout, per-user locale and date format. | [README](../README.md) |
| First-time experience | Welcome dialog, guided tour, checklist, permanent opt-out. | [onboarding-ftue](onboarding-ftue.md) |
| Landing page | Public `/landing` introduction with the real interface. | [landing-page](landing-page.md) |

## Platform and delivery

Three images (UI, API, analytics sandbox) run on Docker Compose, AKS, EKS,
OpenShift (air-gapped) or Argo CD with one Helm chart. Migrations run as an
explicit Job, never from pods. See [`deployment/`](../deployment/README.md).
Test types and how to run them are in [`tests/README.md`](../tests/README.md).

## Planned

- Shared identity with per-application roles:
  [future-plans/shared-identity-rbac](future-plans/shared-identity-rbac.md).
- More connection types, encrypted secrets at rest in `metadata_sources`,
  streaming for the eval node, snapshot encryption, generated conversation
  titles. See the README roadmap.
