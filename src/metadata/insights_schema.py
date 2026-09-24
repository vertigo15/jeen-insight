"""Insights-owned baseline tables, created on demand.

Shared by the API lifespan (every start) and ``scripts/run_insights_migrations.py``
(before the SQL revisions) so the two can run in either order on a fresh
database: several revisions ``INSERT INTO app_settings``, while the API's own
start-up needs columns on tables the revisions create.

The metadata database itself is provisioned by Jeen Schema Modeler (the
``metadata_*`` catalogue and the ``admin_models`` / ``admin_providers`` /
``admin_models_providers`` LLM registry). Insights never creates those; it
only checks they are there and says so clearly when they are not.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PLATFORM_TABLES = ("admin_models", "admin_providers", "admin_models_providers")

PLATFORM_MISSING_MESSAGE = (
    "The metadata database has not been initialised by Jeen Schema Modeler "
    "(missing table(s): {missing}). Point METADATA_DB_* at the Schema Modeler "
    "metadata database, or run its sql/init-metadata-db.sql on this one first."
)


class PlatformSchemaMissing(RuntimeError):
    """The shared metadata DB lacks the Schema Modeler tables Insights relies on."""


class InsightsSchemaMissing(RuntimeError):
    """Verify-only start-up found Insights objects that the migration Job has not created."""


# Everything the API's SELECT/INSERT statements need before the first request.
# Tables the SQL revisions create and the API only probes for (022/023) are
# deliberately not listed: the lifespan degrades gracefully without them.
BASELINE_TABLES = (
    "app_settings",
    "insights_prompts",
    "insights_mcp_servers",
    "insights_mcp_cache",
    "insights_filter_preferences",
)
BASELINE_COLUMNS = (
    ("insights_conversation_sessions", "graph_time_ms"),
    ("insights_conversation_sessions", "result_artifact"),
    ("insights_conversation_sessions", "node_trace"),
    ("insights_mcp_servers", "token_ciphertext"),
    ("insights_mcp_servers", "token_wrapped_dek"),
    ("auth_users", "locale"),
    ("auth_users", "date_format"),
)

INSIGHTS_MISSING_MESSAGE = (
    "Insights schema objects are missing ({missing}) and SCHEMA_BOOTSTRAP_ON_START is "
    "false, so this process will not create them. Run the migration Job "
    "(scripts/run_insights_migrations.py) against this database first."
)

# The metadata DB is shared with Schema Modeler; a booting API pod must never
# queue indefinitely behind another session for a DDL lock.
BOOTSTRAP_LOCK_TIMEOUT = "15s"


async def missing_platform_tables(conn) -> list[str]:
    """Names of the Schema Modeler tables that are absent from this database."""
    missing: list[str] = []
    for table in PLATFORM_TABLES:
        present = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        if not present:
            missing.append(table)
    return missing


async def missing_insights_objects(conn) -> list[str]:
    """Baseline tables/columns (``table`` or ``table.column``) absent from this database."""
    missing: list[str] = []
    for table in BASELINE_TABLES:
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            missing.append(table)
    for table, column in BASELINE_COLUMNS:
        present = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            " WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2)",
            table, column,
        )
        if not present:
            missing.append(f"{table}.{column}")
    return missing


async def verify_insights_baseline(conn) -> None:
    """Read-only check that the baseline exists; raises :class:`InsightsSchemaMissing`."""
    missing = await missing_insights_objects(conn)
    if missing:
        raise InsightsSchemaMissing(INSIGHTS_MISSING_MESSAGE.format(missing=", ".join(missing)))
    logger.info("insights schema: baseline verified (no DDL issued by this process)")


async def ensure_insights_baseline(
    conn,
    *,
    require_platform: bool = True,
    apply: bool = True,
    lock_timeout: str | None = BOOTSTRAP_LOCK_TIMEOUT,
) -> None:
    """Create the Insights-owned tables and additive columns if they do not exist.

    ``require_platform=True`` (the API) raises :class:`PlatformSchemaMissing`
    when the Schema Modeler tables are absent. ``False`` (the migration runner)
    logs a warning and skips the one table that references them, so the SQL
    revisions can still be applied to a database that is provisioned later.

    ``apply=False`` (``SCHEMA_BOOTSTRAP_ON_START=false``, the shared-DB
    deployments) issues no DDL at all: it only verifies the baseline is present
    and raises :class:`InsightsSchemaMissing` otherwise, so schema changes on a
    database shared with Schema Modeler happen exclusively through the
    migration Job.

    ``lock_timeout`` bounds the DDL lock wait for the API's boot-time call;
    the migration runner passes ``None`` because it has already configured
    its own session timeouts and must keep governing them.
    """
    missing = await missing_platform_tables(conn)
    if missing:
        message = PLATFORM_MISSING_MESSAGE.format(missing=", ".join(missing))
        if require_platform:
            raise PlatformSchemaMissing(message)
        logger.warning("%s insights_prompts is not created until then.", message)

    if not apply:
        await verify_insights_baseline(conn)
        return

    # One transaction so a lock wait aborts the whole bootstrap quickly instead
    # of leaving half of it applied while a pod restarts.
    async with conn.transaction():
        if lock_timeout is not None:
            # Constant from this module, never configuration — safe to inline.
            await conn.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
        await _apply_baseline_ddl(conn, platform_missing=bool(missing))


async def _apply_baseline_ddl(conn, *, platform_missing: bool) -> None:
    missing = platform_missing
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key        VARCHAR PRIMARY KEY,
            value      TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    if not missing:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS insights_prompts (
                id           SERIAL PRIMARY KEY,
                prompt_place VARCHAR(100) NOT NULL,
                content      TEXT        NOT NULL,
                version      INTEGER     NOT NULL DEFAULT 1,
                is_active    BOOLEAN     NOT NULL DEFAULT true,
                is_custom    BOOLEAN     NOT NULL DEFAULT false,
                model_id     INTEGER     NULL
                                 REFERENCES admin_models(id) ON DELETE SET NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_prompts_active
                ON insights_prompts(prompt_place)
                WHERE is_active = true
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_insights_prompts_place
                ON insights_prompts(prompt_place)
        """)

    # Additive columns on a table that migration 001 creates. IF EXISTS keeps a
    # fresh database bootable before the revisions have run; IF NOT EXISTS keeps
    # the statements safe to repeat.
    await conn.execute("""
        ALTER TABLE IF EXISTS insights_conversation_sessions
            ADD COLUMN IF NOT EXISTS graph_time_ms INT
    """)
    # Durable result artifact for follow-up detection (see migration 011).
    await conn.execute("""
        ALTER TABLE IF EXISTS insights_conversation_sessions
            ADD COLUMN IF NOT EXISTS result_artifact JSONB
    """)
    # Slim per-node graph timings (see migration 020).
    await conn.execute("""
        ALTER TABLE IF EXISTS insights_conversation_sessions
            ADD COLUMN IF NOT EXISTS node_trace JSONB
    """)

    # ── MCP tables ────────────────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS insights_mcp_servers (
            id                  SERIAL PRIMARY KEY,
            is_active           BOOLEAN     NOT NULL DEFAULT false,
            server_name         TEXT        NOT NULL DEFAULT '',
            endpoint            TEXT        NOT NULL DEFAULT '',
            transport           VARCHAR(10) NOT NULL DEFAULT 'http'
                                    CHECK (transport IN ('stdio', 'sse', 'http')),
            auth_type           VARCHAR(20) NOT NULL DEFAULT 'none'
                                    CHECK (auth_type IN ('none', 'bearer', 'oauth')),
            bearer_token        TEXT,
            cache_ttl_seconds   INT  NOT NULL DEFAULT 900
                                    CHECK (cache_ttl_seconds IN (0,300,900,3600,86400)),
            health              JSONB,
            last_checked_at     TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_mcp_servers_active
            ON insights_mcp_servers(is_active)
            WHERE is_active = true
    """)
    # Envelope-encryption columns for the bearer token (migration 013). Added
    # here too so a fresh DB bootstrapped by the API stays consistent with the
    # SELECT column list before the migration script runs.
    for _col in (
        "token_algo", "token_kek_id", "token_ciphertext",
        "token_nonce", "token_wrapped_dek", "token_dek_nonce",
    ):
        await conn.execute(
            f"ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS {_col} TEXT"
        )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS insights_mcp_cache (
            id              SERIAL PRIMARY KEY,
            mcp_server_id   INT          NOT NULL
                                REFERENCES insights_mcp_servers(id) ON DELETE CASCADE,
            source_key      VARCHAR(255) NOT NULL,
            -- Keep in sync with migrations 016_mcp_cache_keys.sql and
            -- 024_mcp_cache_statistics_keys.sql. Includes the structured
            -- autocomplete datasets (tables_rich / knowledge_questions /
            -- columns_struct:<scope>) and the optional statistics / sample
            -- sections so fresh-DB bootstrap does not drift from the migrated schema.
            cache_key       VARCHAR(160) NOT NULL
                                CHECK (
                                    cache_key IN (
                                        'connections','tables','columns',
                                        'relationships','business_terms','knowledge_pairs',
                                        'tables_rich','knowledge_questions','columns_struct',
                                        'column_statistics','column_samples'
                                    )
                                    OR starts_with(cache_key, 'columns_struct:')
                                ),
            payload         JSONB        NOT NULL,
            fetched_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ  NOT NULL,
            is_stale        BOOLEAN      NOT NULL DEFAULT false,
            CONSTRAINT uq_mcp_cache_entry UNIQUE (mcp_server_id, source_key, cache_key)
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_cache_valid
            ON insights_mcp_cache(mcp_server_id, source_key, cache_key)
            WHERE is_stale = false
    """)
    # Remembered filter-grounding choices (keep in sync with migration
    # 025_filter_preferences.sql).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS insights_filter_preferences (
            id            SERIAL PRIMARY KEY,
            user_id       VARCHAR(255) NOT NULL,
            source_key    VARCHAR(255) NOT NULL,
            literal_norm  VARCHAR(255) NOT NULL DEFAULT '',
            role_column   VARCHAR(255) NOT NULL DEFAULT '',
            table_name    VARCHAR(255),
            column_name   VARCHAR(255),
            any_of        BOOLEAN      NOT NULL DEFAULT FALSE,
            chosen_value  TEXT,
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_insights_filter_pref UNIQUE (user_id, source_key, literal_norm, role_column)
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_insights_filter_pref_user_source
            ON insights_filter_preferences(user_id, source_key)
    """)
    # NOTE: insights_catalog_config was archived in migration 007. The catalog
    # source is now a single global app_settings.catalog_source value and the
    # cache TTL lives on the active MCP server, so no table is bootstrapped here.
