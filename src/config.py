"""Configuration module for Jeen Insights."""

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from environment variables."""

    # Azure OpenAI Configuration. Optional: LLM credentials normally live in the
    # metadata DB (Settings → AI Models); these are only the env fallback used
    # when the DB has no active model. Blank in air-gapped deployments that use
    # an on-prem OpenAI-compatible endpoint configured through the UI.
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_VERSION: str = "2025-01-01-preview"
    AZURE_OPENAI_DEPLOYMENT_NAME: str = "gpt-5.1"

    # Shared Metadata DB (operational + curated metadata)
    METADATA_DB_HOST: str
    METADATA_DB_PORT: int = 5432
    METADATA_DB_NAME: str
    METADATA_DB_USER: str
    METADATA_DB_PASSWORD: str
    METADATA_DB_SSL: bool = True
    # True (default): the API creates/alters its own baseline tables at start-up
    # so a fresh local database boots without a separate step. False: the API
    # only verifies the baseline exists and refuses to start with a clear
    # message when it does not — use this wherever the metadata DB is shared
    # with a live Schema Modeler, so every schema change goes through the
    # migration Job (scripts/run_insights_migrations.py).
    SCHEMA_BOOTSTRAP_ON_START: bool = True

    # Application Settings
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    # Interface language for users without a saved choice (BCP 47 tag from the
    # registry in src/i18n). The Flask UI reads this from the environment
    # directly (see src.i18n.default_locale); mirrored here for discoverability.
    DEFAULT_LOCALE: str = "en"
    # Log output format: "json" (structured, for log aggregators), "console"
    # (human-readable), or "auto" (console in dev mode, json in production).
    LOG_FORMAT: str = "auto"

    # OpenStreetMap point visualizations are disabled until a tile provider is
    # explicitly configured.  Tile and geocoder credentials stay server-side:
    # the browser receives only a same-origin tile proxy URL.
    OSM_MAPS_ENABLED: bool = False
    OSM_TILE_URL: str = ""
    OSM_TILE_API_KEY: str = ""
    OSM_TILE_API_KEY_HEADER: str = "Authorization"
    OSM_TILE_TIMEOUT_SECONDS: float = 10.0
    # Server-side raster cache plus the browser max-age. Tiles change rarely;
    # a week avoids repeat MapTiler fetches on pan/zoom. LRU bounds memory.
    OSM_TILE_CACHE_TTL_SECONDS: int = 604800
    OSM_TILE_CACHE_MAX_ENTRIES: int = 2048
    # Optional server-proxied marine layers. Seamarks are a transparent overlay
    # (navigation marks, buoys, channels), while maritime is a MapTiler custom
    # ocean style that must render the provider's maritime boundaries.
    OSM_SEAMARKS_ENABLED: bool = False
    OSM_SEAMARKS_TILE_URL: str = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"
    OSM_SEAMARKS_TILE_API_KEY: str = ""
    OSM_SEAMARKS_TILE_API_KEY_HEADER: str = "Authorization"
    OSM_MARITIME_TILE_URL: str = ""
    OSM_MARITIME_TILE_API_KEY: str = ""
    OSM_MARITIME_TILE_API_KEY_HEADER: str = ""
    # Terrain is a server-proxied raster basemap. The approved default uses the
    # same managed MapTiler key as Standard; it is disabled until enabled by a
    # deployment overlay after provider access has been validated.
    OSM_TERRAIN_ENABLED: bool = False
    OSM_TERRAIN_TILE_URL: str = (
        "https://api.maptiler.com/maps/outdoor/256/{z}/{x}/{y}.png?key={api_key}"
    )
    OSM_TERRAIN_TILE_API_KEY: str = ""
    OSM_TERRAIN_TILE_API_KEY_HEADER: str = ""
    # Approved adapters are `nominatim` (managed/self-hosted compatible
    # endpoint) and `maptiler` (uses its default endpoint when BASE_URL is
    # blank). Keep this blank unless the deployment is approved for geocoding.
    OSM_GEOCODER_PROVIDER: str = ""
    OSM_GEOCODER_BASE_URL: str = ""
    OSM_GEOCODER_API_KEY: str = ""
    OSM_GEOCODER_API_KEY_HEADER: str = "Authorization"
    OSM_GEOCODER_USER_AGENT: str = "Jeen Insights map geocoder"
    OSM_GEOCODER_TIMEOUT_SECONDS: float = 5.0
    OSM_GEOCODER_MIN_INTERVAL_SECONDS: float = 1.0
    OSM_GEOCODER_CACHE_TTL_SECONDS: int = 2592000
    OSM_GEOCODER_ERROR_CACHE_TTL_SECONDS: int = 300
    OSM_GEOCODER_CACHE_MAX_ENTRIES: int = 10000
    # Bound a chart request so one result cannot turn into a long-running
    # third-party geocoding job. The remaining unique places are marked limited.
    OSM_GEOCODER_MAX_UNIQUE_PLACES: int = 50

    # LangGraph agent settings
    LANGGRAPH_MAX_RETRIES: int = 3
    # When True, a genuinely empty (0-row) result is diagnosed once: a heuristic
    # gates a single LLM call that may regenerate the SQL when the emptiness
    # looks like a JOIN/filter mistake, and produces a likely-cause hint.
    LANGGRAPH_EMPTY_RECHECK: bool = True
    # Optional cheaper deployment for the router, filter planner and memory nodes.
    # Defaults to AZURE_OPENAI_DEPLOYMENT_NAME when empty.
    AZURE_OPENAI_ROUTER_DEPLOYMENT: str = ""

    # ── Conversation memory (prior results referenced by follow-up questions) ──
    # Rows of a prior turn shown to the memory model as a sample; the full data
    # is only ever read by the database (a jsonb_to_recordset CTE in the metadata
    # Postgres — no tables are created).
    MEMORY_SAMPLE_ROWS: int = 3
    # Row ceiling for computing over a prior result (snapshot cap by default).
    MEMORY_COMPUTE_MAX_ROWS: int = 2000
    # Max literal values a prior result may contribute to a new live query
    # ("the top 4 products from T3" → WHERE product IN (...)).
    MEMORY_MAX_BOUND_VALUES: int = 100
    # "Did I ask about X?" without an explicit period searches this far back.
    HISTORY_LOOKUP_DEFAULT_DAYS: int = 30
    HISTORY_LOOKUP_MAX_RESULTS: int = 10
    DLP_ENABLED: bool = True
    SQLGLOT_VALIDATION_ENABLED: bool = True
    # Reject table references qualified with a schema/catalog that doesn't match
    # the connection's configured schema/catalog (blocks cross-schema escapes
    # like `private.users`). Only enforced when the connection schema/catalog is
    # known. Set False for connections that intentionally span schemas.
    SCHEMA_QUALIFIER_VALIDATION_ENABLED: bool = True
    # Extra governed column names (comma-separated) blocked by DLP in addition to
    # the built-in regex patterns — lets ops tag sensitive columns without a code
    # change (e.g. "salary,dob,home_address").
    DLP_GOVERNED_COLUMNS: str = ""
    # Deny-by-default: block query execution when no catalog metadata is
    # available for the connection (failed load or empty catalog). Prevents the
    # model from querying arbitrary, unvalidated tables. Set False only for
    # trusted schema-less deployments that intentionally query without a
    # registered catalog.
    REQUIRE_CATALOG_FOR_QUERY: bool = True
    # Run fused_eval_analytics after non-trivial queries (summary + insights).
    # Set False to skip the third LLM call and prioritise speed over analysis.
    EVAL_ANALYTICS_ENABLED: bool = True
    # Per-call Azure OpenAI timeout in seconds. Prevents a single hung LLM
    # request from blocking the entire query. 0 = no timeout.
    LLM_TIMEOUT_SECONDS: int = 30

    # ── Runtime guardrails (defaults; overridable live via app_settings) ────
    # Per-statement Postgres timeout for user-data queries. Prevents a runaway
    # query from exhausting the connection pool. 0 = no timeout.
    DB_STATEMENT_TIMEOUT_MS: int = 30000
    # Hard ceiling on rows returned by run_sql, regardless of the requested
    # LIMIT. The model cannot exceed this.
    MAX_RESULT_ROWS: int = 10000
    # Number of previous Q&A turns loaded as short-term conversation memory.
    CONVERSATION_CONTEXT_TURNS: int = 5

    # ── Conversation persistence (restore last conversation on reopen) ──────
    # Kill switch for capturing per-turn answer/snapshot/chart artifacts and for
    # the on-open retention prune. Turn logging and LLM context are unaffected.
    # The lifespan forces this off when migration 022 has not been applied.
    CONVERSATION_PERSISTENCE_ENABLED: bool = True
    # All-or-nothing caps for one turn's result snapshot. Above either cap the
    # turn is stored as `too_large` and restores through a re-run instead.
    CONVERSATION_SNAPSHOT_MAX_ROWS: int = 2000
    CONVERSATION_SNAPSHOT_MAX_BYTES: int = 1_048_576
    # Cap for chart_spec + chart_config persisted from /generate-chart and
    # /edit-chart. Enforced separately because the chart is written later.
    CONVERSATION_CHART_MAX_BYTES: int = 524_288
    # Count-based retention, per user + connection. No time-based expiry.
    # Conversations beyond KEEP_LAST are deleted (turns, insights, artifacts).
    CONVERSATION_KEEP_LAST: int = 30
    # Most recent turns that keep their full snapshot + chart; older turns keep
    # question/SQL/answer only and restore through a re-run.
    CONVERSATION_SNAPSHOT_KEEP_LAST_TURNS: int = 100
    # The prune runs as a detached background task when the user opens the app
    # (GET /api/conversations/last), throttled per user + connection.
    CONVERSATION_RETENTION_ON_OPEN: bool = True
    CONVERSATION_RETENTION_MIN_INTERVAL_SECONDS: int = 600
    # Turns returned per hydration page.
    CONVERSATION_MAX_TURNS_HYDRATED: int = 50

    # ── Cost governors ──────────────────────────────────────────────────────
    # Max concurrent text-to-SQL queries a single user may run (per replica).
    # Prevents one user from pinning the LLM / exhausting the DB pool. 0 = off.
    MAX_CONCURRENT_QUERIES_PER_USER: int = 5
    # Seconds to wait for a free slot before rejecting with HTTP 429. 0 = reject
    # immediately when the per-user limit is hit.
    QUERY_QUEUE_WAIT_SECONDS: float = 0.0

    # ── Connector / integration platform ───────────────────────────────────
    # Shared secret used by Flask (sole issuer) to mint short-lived, audience-
    # bound internal tokens that FastAPI verifies into a Principal. When empty,
    # falls back to FLASK_SECRET_KEY / AUTH_SECRET so a correctly-configured
    # deployment enforces the boundary automatically. Rotating: set
    # INTERNAL_API_SECRET to "<kid>:<secret>[,<kid>:<secret>...]"; the first
    # entry signs, all entries verify.
    INTERNAL_API_SECRET: str = ""
    # When true, FastAPI rejects any non-exempt request without a valid internal
    # token (default-deny). Auto-enabled whenever an internal secret is
    # resolvable; set to false only for isolated unit tests.
    INTERNAL_AUTH_ENABLED: bool = True
    # Master key (KEK) for envelope encryption of connector secrets and per-user
    # OAuth token material. Base64 or raw >=32 chars. REQUIRED before any
    # connector credential can be stored; the crypto layer refuses to encrypt
    # without it. Rotating: prepend a new "<kid>:<key>" (comma-separated); the
    # first entry wraps new DEKs, all entries unwrap existing ones.
    APP_ENCRYPTION_KEY: str = ""
    # Single-tenant deployment: connectors are global and every identity/group is
    # expected under this Entra tenant. Falls back to AZURE_AD_TENANT_ID.
    CONNECTORS_TENANT_ID: str = ""
    # Comma-separated recipient-domain allowlist for outbound connector actions
    # (e.g. "example.com,partner.org"). Empty = only the sender's own domain.
    CONNECTOR_RECIPIENT_DOMAIN_ALLOWLIST: str = ""
    # TTL (seconds) for durable result snapshots used as the export
    # authorization source, and for pending action proposals.
    CONNECTOR_SNAPSHOT_TTL_SECONDS: int = 3600
    CONNECTOR_PROPOSAL_TTL_SECONDS: int = 900
    # Max age (seconds) that a cached Entra group-membership snapshot is trusted
    # for authorization. Past this, entitlement checks treat the cache as stale
    # and fail closed so removing a user from a group revokes access promptly.
    CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS: int = 900

    # ── Power BI (text-to-DAX) ──────────────────────────────────────────────
    # Base URL for the Power BI REST API. Override only for sovereign clouds
    # (e.g. https://api.powerbigov.us). The delegated OAuth resource stays
    # https://analysis.windows.net/powerbi/api regardless.
    POWERBI_API_BASE: str = "https://api.powerbi.com"
    # Delegated scope requested for read-only dataset queries. The reserved AAD
    # scopes (openid/profile/email/offline_access) are added by the catalog entry;
    # this is just the Power BI resource scope used for token refresh fallbacks.
    POWERBI_DEFAULT_SCOPE: str = (
        "https://analysis.windows.net/powerbi/api/Dataset.Read.All"
    )
    # Per-call timeout (seconds) for the executeQueries endpoint. Legacy JSON
    # executeQueries has no server-side queryTimeout, so this is the client cap.
    POWERBI_EXECUTE_TIMEOUT_SECONDS: float = 60.0
    # Max DAX repair/regeneration attempts before giving up (separate from the
    # transport retry budget, which is fixed in the feedback router).
    DAX_MAX_RETRIES: int = 4
    # When True, run the DAX static validator (lexer/linter + symbol resolution +
    # DLP) before executing against Power BI. Mirrors SQLGLOT_VALIDATION_ENABLED.
    DAX_VALIDATION_ENABLED: bool = True
    # Value (entity) linking: before generating DAX, verify that the literals in
    # the plan's filters exist as real column values, correcting typos and asking
    # the user when a value is ambiguous or absent. Costs one bounded, read-only
    # probe per filtered text column (cached per user), and fails open.
    DAX_ENTITY_RESOLUTION_ENABLED: bool = True
    # Distinct values pulled per column before the column is treated as too large
    # to match locally (the probe then falls back to a server-side search).
    DAX_ENTITY_MAX_DOMAIN_VALUES: int = 1000
    # Similarity (0-100) a column value needs to be considered a candidate.
    # Lower = more typo tolerance and more clarification questions.
    DAX_ENTITY_MATCH_THRESHOLD: float = 78.0
    # When a literal matches nothing in its target column, search sibling text
    # columns of the same table ("Mountain 300" is a model, not a product name).
    DAX_ENTITY_CROSS_COLUMN_ENABLED: bool = True
    # SQL counterpart of DAX entity resolution. The planner binds filter intent
    # to catalog columns; the resolver validates literal values before SQL is
    # generated. It fails open when a source cannot supply a bounded read-only
    # value lookup.
    SQL_FILTER_RESOLUTION_ENABLED: bool = True
    SQL_FILTER_MAX_DOMAIN_VALUES: int = 1000
    SQL_FILTER_MATCH_THRESHOLD: float = 78.0
    # Bound one MCP/SQL value probe and keep user-scoped verified domains short
    # lived, so filter grounding cannot dominate a query's latency or leak
    # stale RLS-visible values across policy changes.
    SQL_FILTER_LOOKUP_TIMEOUT_MS: int = 5000
    SQL_FILTER_CACHE_TTL_SECONDS: int = 900
    # Metadata-first grounding. Schema Modeler's column profiles and captured
    # values (same metadata DB, or the MCP profile/value tools) are consulted
    # before any source probe; a probe then only confirms what the metadata
    # could not certify.
    SQL_FILTER_METADATA_EVIDENCE_ENABLED: bool = True
    # Under an MCP catalog, let the metadata-DB store answer profile/value
    # reads the MCP server has not mapped (same ``source`` identity on both).
    SQL_FILTER_METADATA_DB_FALLBACK: bool = True
    # Who may see captured values in clarifications/cache:
    #   none         never enumerate values from metadata
    #   source_wide  every user of the source sees the same values (pooled
    #                credentials, no row-level security) — captured values of
    #                non-sensitive columns are shared evidence
    #   user_scoped  values differ per user (RLS); metadata hits are candidates
    #                that must be confirmed under the user's identity
    SQL_FILTER_VALUE_VISIBILITY: str = "source_wide"
    # A literal the evidence could not verify: "ask" the user (default) or
    # "allow" the SQL to run with the literal as written, clearly disclosed.
    SQL_FILTER_UNVERIFIED_EXECUTION: str = "ask"
    # Source probes (point confirmation, bounded search) are only issued to
    # connectors that cancel a statement server-side; this switch turns them
    # off entirely.
    SQL_FILTER_SOURCE_PROBE_ENABLED: bool = True
    # ``SELECT DISTINCT`` on the source to enumerate a small, uncaptured domain.
    # LIMIT bounds returned rows, not scanned rows, so it also requires an exact
    # (not estimated) profile row/distinct count under the domain cap.
    SQL_FILTER_SOURCE_DISTINCT_ENABLED: bool = True
    # Comma-separated table.column globs that are never probed or enumerated.
    SQL_FILTER_PROBE_DENYLIST: str = ""
    # How old metadata may be to prove a value exists (hit) vs. to tell the user
    # it does not (miss). Absence is stricter: it is shown as a fact.
    SQL_FILTER_EXISTENCE_MAX_AGE_HOURS: int = 168
    SQL_FILTER_ABSENCE_MAX_AGE_HOURS: int = 24
    # NOTE: there is deliberately no app-only (service-principal) or pre-minted
    # test-token escape hatch here. Power BI is read strictly through the
    # signed-in user's delegated grant so that the model's row-level security
    # applies to them; a shared app identity would return rows the asker is not
    # allowed to see. Any environment still setting POWERBI_APP_* or
    # POWERBI_TEST_ACCESS_TOKEN is ignored (``extra = "ignore"`` below).

    # ── ML skills (anomaly detection, forecasting) ──────────────────────────
    # Master switch for the needs_analysis route, the analysis graph branch and
    # the /api/analysis endpoints. Keep False in production until the sandbox
    # runner is deployed.
    ML_SKILLS_ENABLED: bool = True
    # Where skills execute: "sandbox" (the jeen-insights-analytics container —
    # the only production option), "local" (a resource-limited child process;
    # development only) or "in_process" (tests only). The two non-sandbox
    # runners are refused when JEEN_DEV_MODE=false.
    ANALYSIS_RUNNER: str = "local"
    ANALYSIS_TIMEOUT_SECONDS: int = 60
    ANALYSIS_MEMORY_MB: int = 1024
    # Internal-only URL of the sandbox service and the audience its tokens carry.
    ANALYSIS_SANDBOX_URL: str = "http://jeen-insights-analytics:8100"
    ANALYSIS_SANDBOX_AUDIENCE: str = "jeen-insights-analytics"
    # Max rows sent to the model per run — tier A (aggregates) and tier B
    # (row-level; also clamps EntityRequest.row_cap) — and the per-user budget.
    # The sandbox enforces the same two limits independently.
    ANALYSIS_MAX_SERIES_ROWS: int = 1500
    ANALYSIS_MAX_ENTITY_ROWS: int = 50_000
    ANALYSIS_RUNS_PER_HOUR_PER_USER: int = 60
    # How long a confirm / clarify / guard proposal stays resumable.
    ANALYSIS_PROPOSAL_TTL_SECONDS: int = 900

    # ── Deployment safety ───────────────────────────────────────────────────
    # Development / POC mode. Defaults to TRUE so a fresh copy of the app + shared
    # DB "just works" anywhere with zero secret provisioning (boots without strong
    # signing keys and stores the MCP token as portable plaintext). Set
    # JEEN_DEV_MODE=false to HARDEN a real deployment: then strong FLASK_SECRET_KEY
    # / INTERNAL_API_SECRET and a valid APP_ENCRYPTION_KEY become mandatory and the
    # MCP bearer token is encrypted at rest.
    JEEN_DEV_MODE: bool = True
    # Out-of-band token required to complete first-run admin setup. When empty a
    # random token is generated once and printed to the server log so only an
    # operator (with log access) can bootstrap the first admin.
    SETUP_BOOTSTRAP_TOKEN: str = ""

    # ── Schema linking (prompt-side catalog pruning / RAG) ──────────────────
    # For large catalogs, injecting every table+column into the system prompt is
    # slow, costly, and hurts accuracy. When enabled, prompt_builder selects the
    # most relevant tables/columns for the current question (lexical scoring +
    # relationship expansion) instead of dumping the whole catalog. Validation
    # still uses the FULL allowlist, so pruning never blocks a valid query.
    SCHEMA_LINK_ENABLED: bool = True
    # Only prune when the catalog exceeds this many columns; small schemas are
    # injected in full (no behavior change, no relevance risk).
    SCHEMA_LINK_MIN_COLUMNS: int = 60
    # Caps applied to the pruned prompt view.
    SCHEMA_LINK_MAX_TABLES: int = 20
    SCHEMA_LINK_MAX_COLUMNS: int = 300
    SCHEMA_LINK_MAX_COLUMNS_PER_TABLE: int = 40

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # tolerate legacy DATA_SOURCE_* / PGVECTOR_* envs

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        """Normalise to upper case and fail fast on an unknown level name.

        Previously ``getattr(logging, LOG_LEVEL)`` crashed at startup on a typo
        (e.g. ``info``); this turns that into a clear configuration error.
        """
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        up = str(v).strip().upper()
        if up not in allowed:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(allowed)}, got {v!r}"
            )
        return up

    @field_validator("LOG_FORMAT")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        allowed = {"auto", "json", "console"}
        low = str(v).strip().lower()
        if low not in allowed:
            raise ValueError(
                f"LOG_FORMAT must be one of {sorted(allowed)}, got {v!r}"
            )
        return low

    @property
    def metadata_connection_string(self) -> str:
        """Build PostgreSQL connection string for the shared metadata DB."""
        suffix = "?sslmode=require" if self.METADATA_DB_SSL else ""
        return (
            f"postgresql://{self.METADATA_DB_USER}:{self.METADATA_DB_PASSWORD}"
            f"@{self.METADATA_DB_HOST}:{self.METADATA_DB_PORT}/{self.METADATA_DB_NAME}"
            f"{suffix}"
        )


# Global settings instance
settings = Settings()
