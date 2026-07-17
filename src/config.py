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
