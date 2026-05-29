"""Module-level state populated by the FastAPI lifespan.

We deliberately use module globals rather than `app.state` so route handlers
read the same handles the way they did when everything lived in `main.py`.
The lifespan in `src.api.lifespan` is the only place these are mutated.

Routes import the module and call the getter helpers in
`src.api.dependencies`, which raise an HTTPException(503) when the app is
not yet initialised (e.g. during graceful shutdown).
"""

from __future__ import annotations

from typing import Optional

from src.agent import AgentRegistry
from src.agent.conversation_history import ConversationHistoryService
from src.agent.llm_service import LangChainLlmService
from src.connections import ConnectionService
from src.metadata import MetadataLoader

# Populated on startup by `src.api.lifespan.lifespan`.
agent_registry: Optional[AgentRegistry] = None
metadata_loader: Optional[MetadataLoader] = None
connection_service: Optional[ConnectionService] = None
history_service: Optional[ConversationHistoryService] = None
llm_service: Optional[LangChainLlmService] = None
