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
from src.agent.langgraph_agent import PromptLoader
from src.agent.llm_service import AzureOpenAILlmService
from src.agent.user_resolver import SimpleUserResolver
from src.api import state
from src.config import settings
from src.connections import ConnectionService
from src.metadata import MetadataLoader, close_metadata_pool, get_metadata_pool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise services on app startup; close them on shutdown."""
    logger.info("🚀 Starting Jeen Insights...")
    pool = await get_metadata_pool()

    state.metadata_loader = MetadataLoader(pool)
    state.connection_service = ConnectionService(pool)
    state.history_service = ConversationHistoryService(pool)

    # Primary (large-model) LLM service for SQL generation and evaluation
    llm_service = AzureOpenAILlmService(
        api_key=settings.AZURE_OPENAI_API_KEY,
        endpoint=settings.AZURE_OPENAI_ENDPOINT,
        deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    # Router LLM: use a separate cheaper deployment when configured,
    # otherwise reuse the primary service (same object, no extra cost).
    router_deployment = settings.AZURE_OPENAI_ROUTER_DEPLOYMENT or settings.AZURE_OPENAI_DEPLOYMENT_NAME
    if router_deployment != settings.AZURE_OPENAI_DEPLOYMENT_NAME:
        router_llm_service = AzureOpenAILlmService(
            api_key=settings.AZURE_OPENAI_API_KEY,
            endpoint=settings.AZURE_OPENAI_ENDPOINT,
            deployment=router_deployment,
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )
        logger.info("Router LLM using separate deployment: %s", router_deployment)
    else:
        router_llm_service = llm_service

    # Load all prompt templates from src/agent/prompts/
    prompt_loader = PromptLoader()

    state.agent_registry = AgentRegistry(
        llm_service=llm_service,
        router_llm_service=router_llm_service,
        metadata_loader=state.metadata_loader,
        connection_service=state.connection_service,
        history_service=state.history_service,
        user_resolver=SimpleUserResolver(),
        prompt_loader=prompt_loader,
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
