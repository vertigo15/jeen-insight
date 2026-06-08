"""Catalog and prompt-building nodes.

catalog_lookup   Fetches the metadata bundle and extracts known table names.
                 Routes to MCP or DB depending on the per-connection
                 ``catalog_source`` setting in ``insights_catalog_config``.
prompt_builder   Assembles the system prompt and the ``structured_prompt`` dict
                 (used by the UI's "Show Prompt" panel).

Both are factory functions that close over their dependencies.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.metadata import MetadataLoader

logger = logging.getLogger(__name__)


# ── Catalog router ───────────────────────────────────────────────────────────────────


async def _load_catalog_bundle(
    source_key: str,
    metadata_loader: MetadataLoader,
) -> Dict[str, str]:
    """
    Load the catalog bundle for *source_key*, routing to MCP or DB depending
    on the per-connection ``insights_catalog_config.catalog_source`` setting.

    Falls back silently to the metadata DB on any MCP error.
    """
    # Lazy import avoids a circular dependency at module load time.
    try:
        from src.api import state as _state  # noqa: PLC0415
        if _state.mcp_server_service and _state.mcp_catalog_client:
            catalog_source = await _state.mcp_server_service.get_catalog_source(source_key)
            if catalog_source == "mcp":
                logger.info(
                    "catalog_lookup: using MCP provider for source_key=%s", source_key
                )
                return await _state.mcp_catalog_client.load_all(source_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "catalog_lookup: MCP routing failed (%s) — falling back to metadata DB", exc
        )
    return await metadata_loader.load_all(source_key)


# ── catalog_lookup ───────────────────────────────────────────────────────────────────────


def make_catalog_lookup(metadata_loader: MetadataLoader):
    """Return an async node that loads the metadata bundle for the active source."""

    async def catalog_lookup(state: AgentState) -> Dict[str, Any]:
        source_key = state["source_key"]
        logger.info("catalog_lookup: loading metadata for source_key=%s", source_key)
        bundle = await _load_catalog_bundle(source_key, metadata_loader)
        known_tables = _extract_table_names(bundle.get("tables", ""))
        logger.info("catalog_lookup: %d known tables", len(known_tables))
        return {
            "metadata_bundle": bundle,
            "known_tables": known_tables,
        }

    return catalog_lookup


# ── prompt_builder ────────────────────────────────────────────────────────────


def make_prompt_builder(prompt_loader: PromptLoader):
    """Return a sync node that builds the system prompt and structured_prompt."""

    def prompt_builder(state: AgentState) -> Dict[str, Any]:
        bundle = state.get("metadata_bundle") or {}
        display_name = state.get("connection_display_name", "")
        db_type = state.get("database_type", "")
        question = state.get("question", "")
        history = state.get("conversation_history") or []

        system_prompt = prompt_loader.render(
            "jeen_insights_system",
            connection_display_name=display_name,
            database_type=db_type,
            tables=bundle.get("tables", ""),
            columns=bundle.get("columns", ""),
            relationships=bundle.get("relationships", ""),
            sources=bundle.get("sources", ""),
            knowledge_pairs=bundle.get("knowledge_pairs", ""),
            business_terms=bundle.get("business_terms", ""),
        )

        # structured_prompt is forwarded as-is to the UI "Show Prompt" panel
        structured_prompt: Dict[str, Any] = {
            "tables": bundle.get("tables", ""),
            "columns": bundle.get("columns", ""),
            "relationships": bundle.get("relationships", ""),
            "sources": bundle.get("sources", ""),
            "knowledge_pairs": bundle.get("knowledge_pairs", ""),
            "business_terms": bundle.get("business_terms", ""),
            "conversation_history": [
                {
                    "question": qa.get("natural_language_query"),
                    "sql": qa.get("generated_sql"),
                }
                for qa in history
                if qa.get("natural_language_query") and qa.get("generated_sql")
            ],
            "current_question": question,
            "full_text": system_prompt,
            "connection": {
                "source_key": state.get("source_key"),
                "display_name": display_name,
                "database_type": db_type,
            },
        }

        logger.info("prompt_builder: system prompt built (%d chars)", len(system_prompt))
        return {
            "system_prompt": system_prompt,
            "structured_prompt": structured_prompt,
        }

    return prompt_builder


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_table_names(tables_text: str) -> List[str]:
    """Parse lower-cased table names from the metadata ``tables`` string.

    Lines look like ``- TableName`` or ``- TableName - description``.
    """
    names: List[str] = []
    for line in tables_text.splitlines():
        stripped = line.lstrip("- ").strip()
        if not stripped:
            continue
        table_name = stripped.split(" - ")[0].strip()
        if table_name:
            names.append(table_name.lower())
    return names
