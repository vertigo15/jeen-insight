"""Configuration module for Jeen Insights."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from environment variables."""

    # Azure OpenAI Configuration
    AZURE_OPENAI_API_KEY: str
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_VERSION: str = "2025-01-01-preview"
    AZURE_OPENAI_DEPLOYMENT_NAME: str = "gpt-5.1"

    # Shared Metadata DB (operational + curated metadata)
    METADATA_DB_HOST: str
    METADATA_DB_PORT: int = 5432
    METADATA_DB_NAME: str
    METADATA_DB_USER: str
    METADATA_DB_PASSWORD: str
    METADATA_DB_SSL: bool = True

    # Application Settings
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # LangGraph agent settings
    LANGGRAPH_MAX_RETRIES: int = 3
    LANGGRAPH_MAX_HISTORY_TOKENS: int = 3000
    # Optional cheaper deployment for router/summarizer nodes.
    # Defaults to AZURE_OPENAI_DEPLOYMENT_NAME when empty.
    AZURE_OPENAI_ROUTER_DEPLOYMENT: str = ""
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
    # NOTE: there is deliberately no app-only (service-principal) or pre-minted
    # test-token escape hatch here. Power BI is read strictly through the
    # signed-in user's delegated grant so that the model's row-level security
    # applies to them; a shared app identity would return rows the asker is not
    # allowed to see. Any environment still setting POWERBI_APP_* or
    # POWERBI_TEST_ACCESS_TOKEN is ignored (``extra = "ignore"`` below).

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
