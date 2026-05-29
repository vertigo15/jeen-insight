"""Shared FastAPI dependencies.

These helpers translate the lifecycle state in `src.api.state` into 503/404
HTTP errors at the boundary, so route handlers can remain free of plumbing.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from src.agent import AgentRegistry, JeenInsightsAgent
from src.agent.conversation_history import ConversationHistoryService
from src.api import state
from src.connections import (
    ConnectionNotFound,
    ConnectionService,
    UnsupportedConnectionType,
)
from src.agent.prompt_cache import PromptCache
from src.metadata import MetadataLoader


def _require(service: object, name: str) -> object:
    if service is None:
        raise HTTPException(
            status_code=503,
            detail=f"{name} not initialised (app is starting up or shutting down)",
        )
    return service


def get_agent_registry() -> AgentRegistry:
    return _require(state.agent_registry, "AgentRegistry")  # type: ignore[return-value]


def get_metadata_loader() -> MetadataLoader:
    return _require(state.metadata_loader, "MetadataLoader")  # type: ignore[return-value]


def get_connection_service() -> ConnectionService:
    return _require(state.connection_service, "ConnectionService")  # type: ignore[return-value]


def get_history_service() -> ConversationHistoryService:
    return _require(state.history_service, "History service")  # type: ignore[return-value]


def get_prompt_cache() -> PromptCache:
    return _require(state.prompt_cache, "PromptCache")  # type: ignore[return-value]


async def resolve_agent(source_key: Optional[str]) -> JeenInsightsAgent:
    """Resolve the per-connection agent or raise the appropriate HTTPException.

    - 400 if `source_key` is empty / missing.
    - 404 if the connection isn't registered in `settings_services`.
    - 501 if the connection's `service_type` isn't supported yet.
    - 503 if the registry isn't ready.
    """
    registry = get_agent_registry()
    if not source_key:
        raise HTTPException(
            status_code=400,
            detail="Missing 'connection' (source_key). Pick one from /api/connections.",
        )
    try:
        return await registry.get_agent(source_key)
    except ConnectionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except UnsupportedConnectionType as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
