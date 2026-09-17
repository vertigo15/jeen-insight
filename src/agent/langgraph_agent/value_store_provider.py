"""Choose the metadata value store for one request.

The grounder must read value evidence over the same transport the planner's
catalog came from: a DB-backed catalog reads Schema Modeler's profile/capture
tables directly through the metadata pool; an MCP-backed catalog calls the
provider's ``get_column_profile`` / ``search_column_values`` tools. Both sides
key rows by the same ``source`` = ``source_key`` identity, so when the MCP
server has not mapped a profile/value tool the DB store stands in for it
(``SQL_FILTER_METADATA_DB_FALLBACK``); the filter's ``evidence`` records which
store answered so provenance stays honest.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agent.langgraph_agent.state import AgentState
from src.config import settings
from src.metadata.metadata_db import get_metadata_pool
from src.metadata.value_store import (
    McpValueStore,
    MetadataDbValueStore,
    NullValueStore,
    ValueStore,
)

logger = logging.getLogger(__name__)

_db_store: Optional[MetadataDbValueStore] = None


def db_value_store() -> MetadataDbValueStore:
    global _db_store
    if _db_store is None:
        _db_store = MetadataDbValueStore(pool_getter=get_metadata_pool)
    return _db_store


def reset_for_tests() -> None:
    global _db_store
    _db_store = None


def value_store_for(state: AgentState) -> ValueStore:
    """Return the store matching the catalog transport used for this request."""
    if not settings.SQL_FILTER_METADATA_EVIDENCE_ENABLED or not state.get(
        "filter_metadata_evidence_enabled", True
    ):
        return NullValueStore()
    if state.get("catalog_source_used") == "mcp":
        try:
            from src.api import state as app_state  # noqa: PLC0415

            client = app_state.mcp_catalog_client
        except Exception:  # noqa: BLE001
            client = None
        if client is not None:
            fallback = db_value_store() if settings.SQL_FILTER_METADATA_DB_FALLBACK else None
            return McpValueStore(client, fallback=fallback)
    return db_value_store()


__all__ = ["db_value_store", "reset_for_tests", "value_store_for"]
