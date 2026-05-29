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
from src.metadata import MetadataLoader, close_metadata_pool, get_metadata_pool

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
    """Insert a default v1 row for every registered prompt that has no active
    row yet.  Idempotent: never touches rows that already exist.
    """
    # Import here to avoid a circular import at module load time.
    from src.api.routes.settings import PROMPT_REGISTRY

    seeded = 0
    for entry in PROMPT_REGISTRY:
        place = entry["name"]
        exists = await conn.fetchval(
            "SELECT 1 FROM insights_prompts WHERE prompt_place = $1 AND is_active = true",
            place,
        )
        if exists:
            continue
        path = entry["path"]
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        await conn.execute(
            """
            INSERT INTO insights_prompts
                (prompt_place, content, version, is_active, is_custom, model_id)
            VALUES ($1, $2, 1, true, false, NULL)
            """,
            place,
            content,
        )
        seeded += 1

    if seeded:
        logger.info("startup: seeded %d prompt row(s) in insights_prompts", seeded)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise services on app startup; close them on shutdown."""
    logger.info("🚀 Starting Jeen Insights...")
    pool = await get_metadata_pool()

    state.metadata_loader = MetadataLoader(pool)
    state.connection_service = ConnectionService(pool)
    state.history_service = ConversationHistoryService(pool)

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

    # ── Pre-warm caches (metadata + system prompt) for all connections ────────
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
        state.agent_registry = None
        state.metadata_loader  = None
        state.connection_service = None
        state.history_service  = None
        state.llm_service      = None
        state.prompt_cache     = None
