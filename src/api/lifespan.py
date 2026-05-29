"""FastAPI lifespan: builds and tears down shared services.

This is the single source of truth for the app's startup/shutdown order.
Routes never instantiate services themselves; they read from `src.api.state`
(via `src.api.dependencies` getters).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.agent import AgentRegistry
from src.agent.conversation_history import ConversationHistoryService
from src.agent.llm_service import AzureOpenAILlmService
from src.agent.user_resolver import SimpleUserResolver
from src.api import state
from src.config import settings
from src.connections import ConnectionService
from src.metadata import MetadataLoader, close_metadata_pool, get_metadata_pool

logger = logging.getLogger(__name__)


def get_agent():
    """Return the AgentRegistry for use by settings hot-reload."""
    return state.agent_registry


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise services on app startup; close them on shutdown."""
    logger.info("🚀 Starting Jeen Insights...")
    pool = await get_metadata_pool()

    state.metadata_loader = MetadataLoader(pool)
    state.connection_service = ConnectionService(pool)
    state.history_service = ConversationHistoryService(pool)

    # ── Ensure app_settings table exists ─────────────────────────────────────
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   VARCHAR PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    # ── Build LLM service, then apply any persisted model selection ───────────
    llm_service = AzureOpenAILlmService(
        api_key=settings.AZURE_OPENAI_API_KEY,
        endpoint=settings.AZURE_OPENAI_ENDPOINT,
        deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_settings WHERE key = 'active_model'"
        )
        if row and row["value"]:
            # Look up the real Azure deployment_name for this model.
            dep_row = await conn.fetchrow(
                "SELECT deployment_name FROM admin_models WHERE name = $1",
                row["value"],
            )
            deployment = (dep_row["deployment_name"] if dep_row else None) or row["value"]
            logger.info(
                "startup: applying persisted model '%s' → deployment '%s'",
                row["value"], deployment,
            )
            llm_service.set_deployment(deployment)

    state.llm_service = llm_service

    state.agent_registry = AgentRegistry(
        llm_service=llm_service,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=SimpleUserResolver(),
    )

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
        state.metadata_loader = None
        state.connection_service = None
        state.history_service = None
        state.llm_service = None
