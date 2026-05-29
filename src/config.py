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
    # Run fused_eval_analytics after non-trivial queries (summary + insights).
    # Set False to skip the third LLM call and prioritise speed over analysis.
    EVAL_ANALYTICS_ENABLED: bool = True
    # Per-call Azure OpenAI timeout in seconds. Prevents a single hung LLM
    # request from blocking the entire query. 0 = no timeout.
    LLM_TIMEOUT_SECONDS: int = 30

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
