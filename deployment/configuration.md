# Configuration reference

This is the canonical deployment inventory. Put non-secret values in component
Helm `env` maps/ConfigMaps and secret values in externally managed Kubernetes
Secrets. Never commit populated values.

Legend: **Need** is `R` (required in every production deployment), `C`
(required when the feature is enabled), or `O` (optional). **Secret** is
`yes` when the value must be held in a secret manager/Kubernetes Secret.
**Precedence** is `env`, `DB → env` (an `app_settings` row overrides the
environment default), `model DB → env` (active Schema Modeler model config
first), or `connector DB → env` (per-connector configuration first).

## Required database

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `METADATA_DB_HOST` | R | no | no default | API, UI, migration | `env`; Schema Modeler metadata PostgreSQL host |
| `METADATA_DB_PORT` | O | no | `5432` | API, UI, migration | `env` |
| `METADATA_DB_NAME` | R | no | no default | API, UI, migration | `env`; Schema Modeler metadata database |
| `METADATA_DB_USER` | R | yes | no default | API, UI, migration | `env`; use a dedicated role per boundary |
| `METADATA_DB_PASSWORD` | R | yes | no default | API, UI, migration | `env` |
| `METADATA_DB_SSL` | R | no | `true` | API, UI, migration | `env`; enables encrypted PostgreSQL transport |
| `SCHEMA_BOOTSTRAP_ON_START` | R | no | local `true`; shared DB `false` | API | `env`; production/shared DB must use `false` |

`METADATA_DB_SSL=true` currently builds PostgreSQL `sslmode=require`. It
encrypts transport but does **not** expose `verify-ca`/`verify-full`, a custom
RDS CA path, or hostname verification. For RDS or regulated deployments, treat
this as a known limitation: enforce private networking and server-side TLS,
track the missing certificate-verification control, and do not claim full
server identity verification.

## Deployment security

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `JEEN_DEV_MODE` | R | no | local `true`; production `false` | API, UI | `env`; production fail-closed mode |
| `FLASK_SECRET_KEY` | R | yes | random 48+ character token | UI; API fallback | `env`; session/CSRF signing |
| `INTERNAL_API_SECRET` | R | yes | independent random token or key ring | API, UI | `env`; UI-issued internal audience tokens |
| `INTERNAL_AUTH_ENABLED` | R | no | `true` | API | `env`; keep enabled outside isolated tests |
| `APP_ENCRYPTION_KEY` | C | yes | random 32-byte base64 KEK | API | `env`; required for encrypted connector/MCP secrets |
| `INTERNAL_ANALYTICS_SECRET` | C | yes | independent random token | API, analytics | `env`; sandbox token signing/verification |
| `SESSION_COOKIE_SECURE` | R | no | production `true` | UI | `env`; HTTPS-only cookie |
| `SETUP_BOOTSTRAP_TOKEN` | C | yes | generated once when blank | UI | `env`; first local-admin setup only |
| `RATELIMIT_STORAGE_URI` | O | yes when authenticated | `memory://`; shared Redis for replicas | UI | `env`; login rate-limit storage |
| `ENCRYPT_MCP_TOKENS_BACKFILL` | O | no | `false` | migration | `env`; one-time, shared-DB-sensitive backfill |

Generate signing tokens with a cryptographic random generator. When connectors
or MCP secrets are enabled, generate the KEK as 32 random bytes encoded as
base64. The KEK belongs to the complete set of deployments sharing one metadata
database, not to a pod, user or component. Every reader of encrypted connector
data must have the same key ring.

Rotation uses `kid:key` comma-separated key rings: prepend the new key so new
data is wrapped/signed with it, retain old keys for reads, rewrap/reconnect
stored grants, then remove retired keys only after verification. Back up the
KEK in the approved secret manager; losing all read keys makes encrypted
connector credentials unrecoverable. Never enable the MCP-token backfill until
all database readers share the KEK.

Use component-specific Secrets where possible: UI gets Flask, bootstrap and
OIDC client secrets; API gets database, LLM fallback, internal API and KEK
material; analytics gets only `INTERNAL_ANALYTICS_SECRET`; migration gets a
dedicated database role plus only keys required by an opted-in backfill.

## Migration controls

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `RUN_MIGRATIONS_ON_START` | R | no | shared DB `false` | workload policy | legacy safety switch; keep `false` |
| `MIGRATION_LOCK_WAIT_SECONDS` | O | no | `120` | migration | `env`; advisory-lock wait bound |
| `MIGRATION_LOCK_TIMEOUT` | O | no | `15s` | migration | `env`; PostgreSQL table-lock timeout |
| `MIGRATION_STATEMENT_TIMEOUT` | O | no | `10min` | migration | `env`; per-statement timeout |
| `MIGRATION_ALLOW_CHECKSUM_DRIFT` | O | no | `false` | migration | emergency explicit override only |

See the [migration runbook](migrations.md). Chart values normally inject these
into the Job; they do not belong in workload pods.

## Service and public URL

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `APP_HOST` | O | no | `0.0.0.0` | API | `env`; listen address |
| `APP_PORT` | O | no | `8000` | API | `env`; listen port |
| `API_BASE_URL` | R | no | in-cluster API Service URL | UI | `env`; never browser-facing |
| `UI_PORT` | O | no | `8501` | UI | `env`; listen port |
| `PUBLIC_APP_URL` | R | no | exact external HTTPS origin | UI | `env`; OAuth callback construction |
| `ANALYTICS_HOST` | O | no | `0.0.0.0` | analytics | `env`; listen address |
| `ANALYTICS_PORT` | O | no | `8100` | analytics | `env`; listen port |
| `LOG_LEVEL` | O | no | `INFO` | all | `env` |
| `LOG_FORMAT` | O | no | `auto`; production `json` | all | `env` |
| `DEFAULT_LOCALE` | O | no | `en` | API, UI | `env`; user preference overrides after login |

`PUBLIC_APP_URL` must be the URL users actually open, including scheme and any
non-default port, without a trailing slash. Register callbacks derived from
that same origin: `/auth/microsoft/callback`,
`/auth/keycloak/callback`, and `/auth/zitadel/callback`. Configure forwarded
scheme/host handling at the ingress; do not use the ClusterIP URL in callbacks.

## LLM

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `AZURE_OPENAI_API_KEY` | C | yes | blank | API | `model DB → env`; fallback credential |
| `AZURE_OPENAI_ENDPOINT` | C | no | blank | API | `model DB → env`; fallback endpoint |
| `AZURE_OPENAI_API_VERSION` | O | no | `2025-01-01-preview` | API | `model DB → env` |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | O | no | `gpt-5.1` | API | `model DB → env`; fallback deployment |
| `AZURE_OPENAI_ROUTER_DEPLOYMENT` | O | no | primary deployment | API | `env`; optional cheaper routing model |
| `LLM_TIMEOUT_SECONDS` | O | no | `30` | API | `env`; zero removes the per-call cap |
| `LANGGRAPH_MAX_RETRIES` | O | no | `3` | API | `env` |
| `LANGGRAPH_EMPTY_RECHECK` | O | no | `true` | API | `env`; one bounded empty-result recheck |

The active model/provider in the shared database is authoritative. Environment
Azure OpenAI settings are fallback only and should remain blank when the model
is managed in Schema Modeler or an air-gapped on-prem endpoint is used.

## Microsoft Entra and generic OIDC

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `AZURE_AD_CLIENT_ID` | C | no | blank | UI; connectors | `env`; Entra app/client ID |
| `AZURE_AD_CLIENT_SECRET` | C | yes | blank | UI; connectors | `env`; confidential client secret |
| `AZURE_AD_TENANT_ID` | C | no | blank | UI; connectors | `env`; tenant |
| `AZURE_AD_REDIRECT_URI` | O | no | derived from `PUBLIC_APP_URL` | UI | `env`; explicit Microsoft callback |
| `OIDC_PROVIDERS` | O | no | `keycloak,zitadel` | UI | `env`; enabled provider keys |
| `OIDC_KEYCLOAK_ISSUER` | C | no | blank | UI | `env`; enables provider with client ID |
| `OIDC_KEYCLOAK_CLIENT_ID` | C | no | blank | UI | `env` |
| `OIDC_KEYCLOAK_CLIENT_SECRET` | O | yes | blank for public client | UI | `env` |
| `OIDC_KEYCLOAK_LABEL` | O | no | provider title | UI | `env` |
| `OIDC_KEYCLOAK_IDP_HINT` | O | no | blank | UI | `env`; Keycloak upstream hint |
| `OIDC_KEYCLOAK_ADMIN_ROLES` | O | no | blank | UI | `env`; comma-separated authoritative roles |
| `OIDC_KEYCLOAK_EDITOR_ROLES` | O | no | blank | UI | `env` |
| `OIDC_KEYCLOAK_SCOPES` | O | no | `openid profile email` | UI | `env`; space-separated scopes |
| `OIDC_ZITADEL_ISSUER` | C | no | blank | UI | `env`; enables provider with client ID |
| `OIDC_ZITADEL_CLIENT_ID` | C | no | blank | UI | `env` |
| `OIDC_ZITADEL_CLIENT_SECRET` | O | yes | blank for public client | UI | `env` |
| `OIDC_ZITADEL_LABEL` | O | no | provider title | UI | `env` |
| `OIDC_ZITADEL_IDP_HINT` | O | no | blank | UI | `env`; supported generic hint field |
| `OIDC_ZITADEL_ADMIN_ROLES` | O | no | blank | UI | `env` |
| `OIDC_ZITADEL_EDITOR_ROLES` | O | no | blank | UI | `env` |
| `OIDC_ZITADEL_SCOPES` | O | no | `openid profile email` | UI | `env` |
| `OIDC_CA_BUNDLE` | C | no | blank | UI | `env`; mounted private-CA PEM path |
| `OIDC_TLS_VERIFY` | R | no | `true` | UI | `env`; never disable in production |

See the [OIDC guide](oidc.md).

## ML sandbox

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `ML_SKILLS_ENABLED` | O | no | `true`; disable until runner exists | API | `env` |
| `ANALYSIS_RUNNER` | R | no | local `local`; production `sandbox` | API | `env`; non-sandbox refused in production |
| `ANALYSIS_SANDBOX_URL` | C | no | analytics Service URL | API | `env` |
| `ANALYSIS_SANDBOX_AUDIENCE` | C | no | `jeen-insights-analytics` | API, analytics | `env`; must match |
| `ANALYSIS_AUTH_ENABLED` | R | no | `true` | analytics | `env` |
| `ANALYSIS_TIMEOUT_SECONDS` | O | no | `60` | API, analytics | `env`; wall-clock limit |
| `ANALYSIS_MEMORY_MB` | O | no | `1024` | API, analytics | `env`; run memory cap |
| `ANALYSIS_MAX_SERIES_ROWS` | O | no | `1500` | API, analytics | `env`; aggregate tier cap |
| `ANALYSIS_MAX_ENTITY_ROWS` | O | no | `50000` | API, analytics | `env`; row-level tier cap |
| `ANALYSIS_MAX_CONCURRENT` | O | no | `2` | analytics | `env`; sandbox concurrency |
| `ANALYSIS_RUNS_PER_HOUR_PER_USER` | O | no | `60` | API | `env`; per-user budget |
| `ANALYSIS_PROPOSAL_TTL_SECONDS` | O | no | `900` | API | `env`; resumable proposal lifetime |

## Maps and geocoding

All map provider credentials are server-side API secrets. The browser uses the
same-origin proxy.

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `OSM_MAPS_ENABLED` | O | no | `false` | API | `env`; master point-map switch |
| `OSM_TILE_URL` | C | no | blank | API | `env`; URL must contain z/x/y tokens |
| `OSM_TILE_API_KEY` | C | yes | blank | API | `env` |
| `OSM_TILE_API_KEY_HEADER` | O | no | `Authorization` | API | `env` |
| `OSM_TILE_TIMEOUT_SECONDS` | O | no | `10` | API | `env` |
| `OSM_TILE_CACHE_TTL_SECONDS` | O | no | `604800` | API | `env` |
| `OSM_TILE_CACHE_MAX_ENTRIES` | O | no | `2048` | API | `env` |
| `OSM_SEAMARKS_ENABLED` | O | no | `false` | API | `env` |
| `OSM_SEAMARKS_TILE_URL` | C | no | OpenSeaMap endpoint | API | `env` |
| `OSM_SEAMARKS_TILE_API_KEY` | O | yes | blank | API | `env` |
| `OSM_SEAMARKS_TILE_API_KEY_HEADER` | O | no | `Authorization` | API | `env` |
| `OSM_MARITIME_TILE_URL` | O | no | blank | API | `env`; custom maritime layer |
| `OSM_MARITIME_TILE_API_KEY` | C | yes | blank | API | `env` |
| `OSM_MARITIME_TILE_API_KEY_HEADER` | O | no | blank | API | `env` |
| `OSM_TERRAIN_ENABLED` | O | no | `false` | API | `env` |
| `OSM_TERRAIN_TILE_URL` | C | no | managed outdoor raster template | API | `env` |
| `OSM_TERRAIN_TILE_API_KEY` | C | yes | blank | API | `env` |
| `OSM_TERRAIN_TILE_API_KEY_HEADER` | O | no | blank | API | `env` |
| `OSM_GEOCODER_PROVIDER` | O | no | blank | API | `env`; `nominatim` or `maptiler` |
| `OSM_GEOCODER_BASE_URL` | O | no | provider default | API | `env` |
| `OSM_GEOCODER_API_KEY` | C | yes | blank | API | `env` |
| `OSM_GEOCODER_API_KEY_HEADER` | O | no | `Authorization` | API | `env` |
| `OSM_GEOCODER_USER_AGENT` | C | no | Jeen Insights identifier | API | `env`; required by many Nominatim services |
| `OSM_GEOCODER_TIMEOUT_SECONDS` | O | no | `5` | API | `env` |
| `OSM_GEOCODER_MIN_INTERVAL_SECONDS` | O | no | `1` | API | `env`; provider rate spacing |
| `OSM_GEOCODER_CACHE_TTL_SECONDS` | O | no | `2592000` | API | `env` |
| `OSM_GEOCODER_ERROR_CACHE_TTL_SECONDS` | O | no | `300` | API | `env` |
| `OSM_GEOCODER_CACHE_MAX_ENTRIES` | O | no | `10000` | API | `env` |
| `OSM_GEOCODER_MAX_UNIQUE_PLACES` | O | no | `50` | API | `env`; per-request bound |

## Query, DLP, concurrency, memory and persistence

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `DLP_ENABLED` | R | no | `true` | API | `env`; built-in governed-column checks |
| `DLP_GOVERNED_COLUMNS` | O | no | blank | API | `env`; additional comma-separated names |
| `SQLGLOT_VALIDATION_ENABLED` | R | no | `true` | API | `env`; SQL static validation |
| `SCHEMA_QUALIFIER_VALIDATION_ENABLED` | R | no | `true` | API | `env`; block cross-schema escape |
| `REQUIRE_CATALOG_FOR_QUERY` | R | no | `true` | API | `env`; deny query without catalog |
| `EVAL_ANALYTICS_ENABLED` | O | no | `true` | API | `env`; post-query analysis call |
| `DB_STATEMENT_TIMEOUT_MS` | O | no | `30000` | API | `DB → env`; user-data statement limit |
| `MAX_RESULT_ROWS` | O | no | `10000` | API | `DB → env`; hard result ceiling |
| `MAX_CONCURRENT_QUERIES_PER_USER` | O | no | `5` | API replica | `env`; zero disables |
| `QUERY_QUEUE_WAIT_SECONDS` | O | no | `0` | API replica | `env`; free-slot wait |
| `CONVERSATION_CONTEXT_TURNS` | O | no | `5` | API | `DB → env`; short-term context |
| `MEMORY_SAMPLE_ROWS` | O | no | `3` | API | `env` |
| `MEMORY_COMPUTE_MAX_ROWS` | O | no | `2000` | API | `env` |
| `MEMORY_MAX_BOUND_VALUES` | O | no | `100` | API | `env` |
| `HISTORY_LOOKUP_DEFAULT_DAYS` | O | no | `30` | API | `env` |
| `HISTORY_LOOKUP_MAX_RESULTS` | O | no | `10` | API | `env` |
| `CONVERSATION_PERSISTENCE_ENABLED` | O | no | `true` | API | `env`; requires current schema |
| `CONVERSATION_SNAPSHOT_MAX_ROWS` | O | no | `2000` | API | `env` |
| `CONVERSATION_SNAPSHOT_MAX_BYTES` | O | no | `1048576` | API | `env` |
| `CONVERSATION_CHART_MAX_BYTES` | O | no | `524288` | API | `env` |
| `CONVERSATION_KEEP_LAST` | O | no | `30` | API | `env`; per user/connection |
| `CONVERSATION_SNAPSHOT_KEEP_LAST_TURNS` | O | no | `100` | API | `env` |
| `CONVERSATION_RETENTION_ON_OPEN` | O | no | `true` | API | `env` |
| `CONVERSATION_RETENTION_MIN_INTERVAL_SECONDS` | O | no | `600` | API | `env` |
| `CONVERSATION_MAX_TURNS_HYDRATED` | O | no | `50` | API | `env` |
| `JEEN_RESULT_CACHE_MAX_ENTRIES` | O | no | `256` | API | `env`; process-local result cache |
| `JEEN_RESULT_CACHE_TTL_SECONDS` | O | no | `1800` | API | `env` |
| `JEEN_RESULT_CACHE_PER_USER` | O | no | `5` | API | `env` |
| `SCHEMA_LINK_ENABLED` | O | no | `true` | API | `env`; prompt-side catalog pruning |
| `SCHEMA_LINK_MIN_COLUMNS` | O | no | `60` | API | `env` |
| `SCHEMA_LINK_MAX_TABLES` | O | no | `20` | API | `env` |
| `SCHEMA_LINK_MAX_COLUMNS` | O | no | `300` | API | `env` |
| `SCHEMA_LINK_MAX_COLUMNS_PER_TABLE` | O | no | `40` | API | `env` |

Only `DB_STATEMENT_TIMEOUT_MS`, `MAX_RESULT_ROWS`, and
`CONVERSATION_CONTEXT_TURNS` use `app_settings` overrides. Other environment
variables remain deployment-controlled unless explicitly documented.

## DAX, Power BI and filter grounding

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `POWERBI_API_BASE` | O | no | public Power BI API | API | `env`; sovereign-cloud override |
| `POWERBI_DEFAULT_SCOPE` | O | no | delegated dataset read scope | API | `env` |
| `POWERBI_EXECUTE_TIMEOUT_SECONDS` | O | no | `60` | API | `env` |
| `DAX_MAX_RETRIES` | O | no | `4` | API | `env` |
| `DAX_VALIDATION_ENABLED` | R | no | `true` | API | `env` |
| `DAX_ENTITY_RESOLUTION_ENABLED` | O | no | `true` | API | `env` |
| `DAX_ENTITY_MAX_DOMAIN_VALUES` | O | no | `1000` | API | `env` |
| `DAX_ENTITY_MATCH_THRESHOLD` | O | no | `78` | API | `env`; 0–100 similarity |
| `DAX_ENTITY_CROSS_COLUMN_ENABLED` | O | no | `true` | API | `env` |
| `SQL_FILTER_RESOLUTION_ENABLED` | O | no | `true` | API | `env` |
| `SQL_FILTER_MAX_DOMAIN_VALUES` | O | no | `1000` | API | `env` |
| `SQL_FILTER_MATCH_THRESHOLD` | O | no | `78` | API | `env` |
| `SQL_FILTER_LOOKUP_TIMEOUT_MS` | O | no | `5000` | API | `env` |
| `SQL_FILTER_CACHE_TTL_SECONDS` | O | no | `900` | API | `env`; user-scoped cache |
| `SQL_FILTER_METADATA_EVIDENCE_ENABLED` | O | no | `true` | API | `env` |
| `SQL_FILTER_METADATA_DB_FALLBACK` | O | no | `true` | API | `env` |
| `SQL_FILTER_VALUE_VISIBILITY` | R | no | `source_wide` | API | `env`; choose `none`/`source_wide`/`user_scoped` |
| `SQL_FILTER_UNVERIFIED_EXECUTION` | R | no | `ask` | API | `env`; `ask` or explicit `allow` |
| `SQL_FILTER_SOURCE_PROBE_ENABLED` | O | no | `true` | API | `env` |
| `SQL_FILTER_SOURCE_DISTINCT_ENABLED` | O | no | `true` | API | `env` |
| `SQL_FILTER_PROBE_DENYLIST` | O | no | blank | API | `env`; comma-separated table.column globs |
| `SQL_FILTER_EXISTENCE_MAX_AGE_HOURS` | O | no | `168` | API | `env` |
| `SQL_FILTER_ABSENCE_MAX_AGE_HOURS` | O | no | `24` | API | `env` |

Power BI always uses the signed-in user's delegated grant so row-level security
applies. App-only credentials and pre-minted test-token variables are ignored.

## Connectors

Connector definitions, OAuth client secrets and user grants are normally
stored encrypted in the connector/app database UI. Environment client IDs are
fallbacks only.

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `CONNECTORS_TENANT_ID` | O | no | `AZURE_AD_TENANT_ID` | API | `connector DB → env`; single tenant |
| `CONNECTOR_RECIPIENT_DOMAIN_ALLOWLIST` | O | no | sender's own domain | API | `connector DB → env`; outbound email policy |
| `CONNECTOR_SNAPSHOT_TTL_SECONDS` | O | no | `3600` | API | `env`; export authorization snapshot |
| `CONNECTOR_PROPOSAL_TTL_SECONDS` | O | no | `900` | API | `env` |
| `CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS` | O | no | `900` | API | `env`; stale membership fails closed |
| `SLACK_CLIENT_ID` | O | no | blank | API | `connector DB → env`; OAuth client fallback |
| `JIRA_CLIENT_ID` | O | no | blank | API | `connector DB → env`; OAuth client fallback |

Provider client secrets, Slack/Jira secrets, Tavily keys and Power BI delegated
tokens are connector records protected by `APP_ENCRYPTION_KEY`, not global
environment variables. The `connectors_enabled` master switch in
`app_settings` is authoritative and ships disabled.

## Legacy and runtime-only variables

| Variable | Need | Secret | Default / typical | Owner | Precedence and purpose |
| --- | --- | --- | --- | --- | --- |
| `AUTH_SECRET` | O | yes | blank | UI, API | legacy fallback for `FLASK_SECRET_KEY` |
| `AUTH_URL` | O | no | unused | none | ignored legacy Auth.js variable |
| `AUTH_TRUST_HOST` | O | no | unused | none | ignored legacy Auth.js variable |
| `HOME` | O | no | `/tmp` in hardened containers | all | runtime filesystem location |
| `MPLCONFIGDIR` | O | no | `/tmp/mpl` | analytics | writable matplotlib cache |

Older `DATA_SOURCE_*`, `PGVECTOR_*`, `POWERBI_APP_*`, and
`POWERBI_TEST_ACCESS_TOKEN` variables are intentionally ignored. Remove them
from new deployments rather than relying on them.
