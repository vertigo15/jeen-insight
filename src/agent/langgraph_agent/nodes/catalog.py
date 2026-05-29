"""Catalog and prompt-building nodes.

catalog_lookup   Fetches the metadata bundle and extracts known table names.
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


# ── catalog_lookup ────────────────────────────────────────────────────────────


def make_catalog_lookup(metadata_loader: MetadataLoader):
    """Return an async node that loads the metadata bundle for the active source."""

    async def catalog_lookup(state: AgentState) -> Dict[str, Any]:
        source_key = state["source_key"]
        logger.info("catalog_lookup: loading metadata for source_key=%s", source_key)
        bundle = await metadata_loader.load_all(source_key)
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
