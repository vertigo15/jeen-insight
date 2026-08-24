"""FastAPI lifespan: builds and tears down shared services.

This is the single source of truth for the app's startup/shutdown order.
Routes never instantiate services themselves; they read from `src.api.state`
(via `src.api.dependencies` getters).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI

from src.agent import AgentRegistry, DaxAgentRegistry
from src.agent.conversation_history import ConversationHistoryService
from src.agent.llm_service import LangChainLlmService
from src.agent.prompt_cache import PromptCache
from src.agent.user_resolver import SimpleUserResolver
from src.api import state
from src.config import settings
from src.connections import ConnectionService
from src.metadata import (
    MetadataLoader, close_metadata_pool, get_metadata_pool,
    McpServerService, McpCacheService, McpCatalogClient,
)

logger = logging.getLogger(__name__)


def get_agent():
    """Return the AgentRegistry for use by settings hot-reload."""
    return state.agent_registry


async def _ensure_schema(conn) -> None:
    """Create app tables and indexes if they don't exist yet."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key        VARCHAR PRIMARY KEY,
            value      TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
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
    # Additive columns — safe to run repeatedly via IF NOT EXISTS.
    await conn.execute("""
        ALTER TABLE insights_conversation_sessions
            ADD COLUMN IF NOT EXISTS graph_time_ms INT
    """)
    # Durable result artifact for follow-up detection (see migration 011).
    await conn.execute("""
        ALTER TABLE insights_conversation_sessions
            ADD COLUMN IF NOT EXISTS result_artifact JSONB
    """)
    # Slim per-node graph timings (see migration 020).
    await conn.execute("""
        ALTER TABLE insights_conversation_sessions
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
            -- Keep in sync with migration 016_mcp_cache_keys.sql. Includes the
            -- structured autocomplete datasets (tables_rich / knowledge_questions
            -- / columns_struct:<scope>) so fresh-DB bootstrap does not drift from
            -- the migrated schema.
            cache_key       VARCHAR(160) NOT NULL
                                CHECK (
                                    cache_key IN (
                                        'connections','tables','columns',
                                        'relationships','business_terms','knowledge_pairs',
                                        'tables_rich','knowledge_questions','columns_struct'
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
    # NOTE: insights_catalog_config was archived in migration 007. The catalog
    # source is now a single global app_settings.catalog_source value and the
    # cache TTL lives on the active MCP server, so no table is bootstrapped here.


async def _warm_caches(
    metadata_loader: Any,
    connection_service: Any,
    prompt_cache: Any,
) -> None:
    """Pre-warm metadata and prompt caches after startup.

    Runs concurrently for all active connections so the first real query
    never pays the cold-start penalty.
    """
    import asyncio as _asyncio
    try:
        connections = await connection_service.list_connections()
        if connections:
            tasks = [metadata_loader.load_all(c.source_key) for c in connections]
            results = await _asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if not isinstance(r, Exception))
            errors = sum(1 for r in results if isinstance(r, Exception))
            logger.info(
                "startup: pre-warmed metadata for %d connection(s)%s",
                ok,
                f" ({errors} failed)" if errors else "",
            )
        # Warm the system prompt from DB.
        await prompt_cache.get_content("jeen_insights_system")
        logger.info("startup: pre-warmed prompt cache")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: cache warm-up skipped: %s", exc)


async def _seed_prompts(conn) -> None:
    """Seed and refresh default prompts in the DB.

    - Inserts a v1 row for any prompt that has no active row yet.
    - Updates the content of non-custom rows whose file has changed since the
      row was last written (e.g. after a code update).  Custom rows are never
      touched so user edits are always preserved.
    """
    # Import here to avoid a circular import at module load time.
    from src.api.routes.settings import PROMPT_REGISTRY

    seeded = updated = 0
    for entry in PROMPT_REGISTRY:
        place = entry["name"]
        path  = entry["path"]
        file_content = path.read_text(encoding="utf-8") if path.exists() else ""

        row = await conn.fetchrow(
            "SELECT id, content, is_custom "
            "FROM insights_prompts WHERE prompt_place = $1 AND is_active = true",
            place,
        )

        if not row:
            # New prompt — insert default v1 row.
            await conn.execute(
                """
                INSERT INTO insights_prompts
                    (prompt_place, content, version, is_active, is_custom, model_id)
                VALUES ($1, $2, 1, true, false, NULL)
                """,
                place,
                file_content,
            )
            seeded += 1
        elif not row["is_custom"] and row["content"] != file_content:
            # Default row whose source file was updated — refresh in place.
            await conn.execute(
                "UPDATE insights_prompts SET content = $1, updated_at = NOW() "
                "WHERE id = $2",
                file_content,
                row["id"],
            )
            updated += 1

    if seeded:
        logger.info("startup: seeded %d prompt row(s) in insights_prompts", seeded)
    if updated:
        logger.info("startup: refreshed %d default prompt row(s) from updated files", updated)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise services on app startup; close them on shutdown."""
    logger.info("🚀 Starting Jeen Insights...")

    # Fail fast on unsafe internal-auth config (weak/missing signing secret).
    from src.security.internal_auth import assert_configured as _assert_internal_auth
    _assert_internal_auth()
    # Fail fast if APP_ENCRYPTION_KEY is set but weak.
    from src.security.crypto import assert_kek_valid as _assert_kek
    _assert_kek()

    pool = await get_metadata_pool()
    _app.state.map_tile_client = httpx.AsyncClient(
        timeout=max(0.1, float(settings.OSM_TILE_TIMEOUT_SECONDS)),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=12),
    )

    state.metadata_loader    = MetadataLoader(pool)
    state.connection_service  = ConnectionService(pool)
    state.history_service     = ConversationHistoryService(pool)
    state.mcp_server_service  = McpServerService(pool)
    state.mcp_cache_service   = McpCacheService(pool)
    state.mcp_catalog_client  = McpCatalogClient(
        state.mcp_server_service, state.mcp_cache_service
    )

    # ── Connector / integration platform services ───────────────────────────
    from src.connectors.identity_service import IdentityService
    from src.connectors.registry_service import ConnectorRegistryService
    from src.connectors.grant_service import GrantService
    from src.connectors.snapshot_service import SnapshotService
    from src.connectors.audit_service import AuditService
    from src.connectors.tool_result_service import ToolResultService
    from src.connectors.rate_limiter import RateLimiter
    from src.connectors.action_gate import ActionGate

    state.identity_service = IdentityService(pool)
    state.registry_service = ConnectorRegistryService(pool)
    state.grant_service    = GrantService(pool)
    state.snapshot_service = SnapshotService(pool)
    state.audit_service    = AuditService(pool)
    state.tool_result_service = ToolResultService(pool)
    state.rate_limiter     = RateLimiter(pool)
    state.action_gate      = ActionGate(
        pool,
        registry=state.registry_service,
        grants=state.grant_service,
        snapshots=state.snapshot_service,
        identities=state.identity_service,
        audit=state.audit_service,
        tool_results=state.tool_result_service,
        rate_limiter=state.rate_limiter,
    )

    # ── Schema + prompt seeding ─────────────────────────────────────────────
    async with pool.acquire() as conn:
        await _ensure_schema(conn)
        await _seed_prompts(conn)

    # ── Build LLM service from DB credentials ─────────────────────────────────
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'active_model'"
        )
    active_model: Optional[str] = row["value"] if row else None

    try:
        llm_service = await LangChainLlmService.from_db(pool, active_model)
    except Exception as exc:
        logger.warning(
            "startup: DB model load failed (%s); falling back to env-var Azure creds", exc
        )
        llm_service = LangChainLlmService.from_env_azure(pool, settings)

    state.llm_service = llm_service

    # ── Optional cheaper router/memory model ─────────────────────────────────
    # The router, memory-summarizer and memory-answer nodes are cheap
    # classification/condensation calls that don't need the strong SQL model.
    # When AZURE_OPENAI_ROUTER_DEPLOYMENT is set (and differs from the active
    # model) route those nodes to it, saving cost/latency. Falls back to the
    # main model whenever the cheaper one can't be built.
    router_llm_service = llm_service
    router_deployment = (settings.AZURE_OPENAI_ROUTER_DEPLOYMENT or "").strip()
    if router_deployment and router_deployment != llm_service.get_deployment():
        try:
            router_llm_service = await LangChainLlmService.from_db(pool, router_deployment)
            logger.info(
                "startup: router/memory nodes using cheaper model %r (DB)", router_deployment
            )
        except Exception as db_exc:  # noqa: BLE001
            try:
                router_llm_service = LangChainLlmService.from_env_azure(
                    pool, settings, deployment_override=router_deployment
                )
                logger.info(
                    "startup: router/memory nodes using Azure deployment %r (env)",
                    router_deployment,
                )
            except Exception as env_exc:  # noqa: BLE001
                logger.warning(
                    "startup: router model %r unavailable (%s / %s); reusing main model",
                    router_deployment, db_exc, env_exc,
                )
                router_llm_service = llm_service
    state.router_llm_service = router_llm_service

    # ── Warm the model-health cache in the background ────────────────────────
    # Probes every enabled model so the settings UI shows real status straight
    # away and auto-fallback has data without paying a probe cost on first
    # failure. Fire-and-forget — never blocks or fails startup.
    async def _warm_model_health() -> None:
        try:
            from src.agent import llm_health
            health = await llm_health.get_health(pool)
            healthy = [n for n, h in health.items() if h.healthy is True]
            logger.info(
                "startup: model health probed — %d/%d healthy (%s)",
                len(healthy), len(health), ", ".join(sorted(healthy)) or "none",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("startup: model health warm-up failed: %s", exc)

    state.health_warmup_task = asyncio.create_task(_warm_model_health())

    # ── Prompt cache (starts empty; fills lazily on first use) ───────────────
    state.prompt_cache = PromptCache(pool, llm_service)

    _user_resolver = SimpleUserResolver()
    state.agent_registry = AgentRegistry(
        llm_service=llm_service,
        router_llm_service=router_llm_service,
        prompt_cache=state.prompt_cache,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=_user_resolver,
    )

    # Separate registry for Power BI (text-to-DAX) connections. Shares the same
    # collaborators but is a distinct object so the SQL path is never touched.
    # The token factory is built here, where the connector services already
    # exist, so the DAX nodes receive their Power BI credentials instead of
    # reaching back into this module for them.
    from src.connectors.powerbi_token import make_provider_factory

    state.dax_agent_registry = DaxAgentRegistry(
        llm_service=llm_service,
        router_llm_service=router_llm_service,
        prompt_cache=state.prompt_cache,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=_user_resolver,
        token_provider_factory=make_provider_factory(
            identity_service=state.identity_service,
            registry_service=state.registry_service,
            grant_service=state.grant_service,
        ),
    )

    # ── Build LangGraph insights eval subgraph ────────────────────────────
    try:
        from src.agent.langgraph_agent import build_insights_eval_graph
        state.insights_eval_graph = build_insights_eval_graph(
            llm_service, state.prompt_cache
        )
        logger.info("✅ insights_eval_graph ready")
    except ImportError:
        logger.warning(
            "startup: langgraph not installed — insights eval graph disabled. "
            "Add 'langgraph>=0.2.0' to requirements.txt and rebuild the image."
        )
        state.insights_eval_graph = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: insights_eval_graph build failed: %s", exc)
        state.insights_eval_graph = None

    # ── Pre-warm MCP L1 cache from DB (if MCP mode is active) ────────────────
    try:
        catalog_src = await state.mcp_server_service.get_catalog_source()
        if catalog_src == "mcp":
            active_srv = await state.mcp_server_service.get_active()
            if active_srv:
                warmed = await state.mcp_cache_service.warm_from_db(active_srv.id)
                logger.info("startup: MCP mode active (%s) — warmed %d cache entries", active_srv.server_name, warmed)
            else:
                logger.warning("startup: catalog_source=mcp but no active server; falling back to DB")
        else:
            logger.info("startup: DB catalog mode")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: MCP cache warm-up skipped: %s", exc)

    # ── Pre-warm caches (metadata + system prompt) for all connections ────
    await _warm_caches(state.metadata_loader, state.connection_service, state.prompt_cache)

    logger.info("✅ Jeen Insights ready")
    try:
        yield
    finally:
        logger.info("👋 Shutting down Jeen Insights")
        tile_client = getattr(_app.state, "map_tile_client", None)
        if tile_client is not None:
            await tile_client.aclose()
            _app.state.map_tile_client = None
        if state.agent_registry:
            await state.agent_registry.close()
        if state.dax_agent_registry:
            await state.dax_agent_registry.close()
        await close_metadata_pool()
        # Reset handles so a hot-reload cycle doesn't leave stale references.
        state.agent_registry       = None
        state.dax_agent_registry   = None
        state.metadata_loader       = None
        state.connection_service    = None
        state.history_service       = None
        state.llm_service           = None
        state.router_llm_service    = None
        state.prompt_cache          = None
        state.insights_eval_graph   = None
        state.mcp_server_service    = None
        state.mcp_cache_service     = None
        state.mcp_catalog_client    = None
        state.identity_service      = None
        state.registry_service      = None
        state.grant_service         = None
        state.snapshot_service      = None
        state.audit_service         = None
        state.tool_result_service   = None
        state.rate_limiter          = None
        state.action_gate           = None
