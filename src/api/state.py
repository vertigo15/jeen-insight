"""Module-level state populated by the FastAPI lifespan.

We deliberately use module globals rather than `app.state` so route handlers
read the same handles the way they did when everything lived in `main.py`.
The lifespan in `src.api.lifespan` is the only place these are mutated.

Routes import the module and call the getter helpers in
`src.api.dependencies`, which raise an HTTPException(503) when the app is
not yet initialised (e.g. during graceful shutdown).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from src.agent import AgentRegistry, DaxAgentRegistry
from src.agent.conversation_history import ConversationHistoryService
from src.agent.llm_service import LangChainLlmService
from src.agent.prompt_cache import PromptCache
from src.connections import ConnectionService
from src.metadata import MetadataLoader, McpServerService, McpCacheService, McpCatalogClient

# Populated on startup by `src.api.lifespan.lifespan`.
agent_registry: Optional[AgentRegistry] = None
# Separate registry for Power BI (text-to-DAX) connections.
dax_agent_registry: Optional[DaxAgentRegistry] = None
metadata_loader: Optional[MetadataLoader] = None
connection_service: Optional[ConnectionService] = None
history_service: Optional[ConversationHistoryService] = None
llm_service: Optional[LangChainLlmService] = None
# Optional cheaper model for router/memory nodes; falls back to llm_service.
router_llm_service: Optional[LangChainLlmService] = None
prompt_cache: Optional[PromptCache] = None
# Compiled LangGraph eval subgraph; None when langgraph is not installed.
insights_eval_graph: Optional[object] = None
# MCP catalog services (always initialised; active when catalog_source = 'mcp').
mcp_server_service: Optional[McpServerService] = None
mcp_cache_service: Optional[McpCacheService] = None
mcp_catalog_client: Optional[McpCatalogClient] = None
# Background task that warms the model-health cache on startup.
health_warmup_task: "Optional[asyncio.Task]" = None

# ── Connector / integration platform services ──────────────────────────────
identity_service: Optional[object] = None
registry_service: Optional[object] = None
grant_service: Optional[object] = None
snapshot_service: Optional[object] = None
audit_service: Optional[object] = None
tool_result_service: Optional[object] = None
rate_limiter: Optional[object] = None
action_gate: Optional[object] = None
