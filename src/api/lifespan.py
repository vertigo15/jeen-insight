"""FastAPI lifespan: builds and tears down shared services.

This is the single source of truth for the app's startup/shutdown order.
Routes never instantiate services themselves; they read from `src.api.state`
(via `src.api.dependencies` getters).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI

from src.agent import AgentRegistry
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
    # Additive column — safe to run repeatedly via IF NOT EXISTS.
    await conn.execute("""
        ALTER TABLE insights_conversation_sessions
            ADD COLUMN IF NOT EXISTS graph_time_ms INT
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
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS insights_mcp_cache (
            id              SERIAL PRIMARY KEY,
            mcp_server_id   INT          NOT NULL
                                REFERENCES insights_mcp_servers(id) ON DELETE CASCADE,
            source_key      VARCHAR(255) NOT NULL,
            cache_key       VARCHAR(50)  NOT NULL
                                CHECK (cache_key IN (
                                    'connections','tables','columns',
                                    'relationships','business_terms','knowledge_pairs'
                                )),
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
    pool = await get_metadata_pool()

    state.metadata_loader    = MetadataLoader(pool)
    state.connection_service  = ConnectionService(pool)
    state.history_service     = ConversationHistoryService(pool)
    state.mcp_server_service  = McpServerService(pool)
    state.mcp_cache_service   = McpCacheService(pool)
    state.mcp_catalog_client  = McpCatalogClient(
        state.mcp_server_service, state.mcp_cache_service
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

    # ── Prompt cache (starts empty; fills lazily on first use) ───────────────
    state.prompt_cache = PromptCache(pool, llm_service)

    state.agent_registry = AgentRegistry(
        llm_service=llm_service,
        prompt_cache=state.prompt_cache,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=SimpleUserResolver(),
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
        if state.agent_registry:
            await state.agent_registry.close()
        await close_metadata_pool()
        # Reset handles so a hot-reload cycle doesn't leave stale references.
        state.agent_registry       = None
        state.metadata_loader       = None
        state.connection_service    = None
        state.history_service       = None
        state.llm_service           = None
        state.prompt_cache          = None
        state.insights_eval_graph   = None
        state.mcp_server_service    = None
        state.mcp_cache_service     = None
        state.mcp_catalog_client    = None
