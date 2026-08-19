"""DAX catalog + prompt-building nodes.

dax_catalog_lookup   Loads the curated metadata bundle for the Power BI dataset
                     and derives the DAX-specific catalog: MEASURES kept separate
                     from columns, typed columns, a best-effort date table, and
                     the raw relationships block. Fails closed (``catalog_blocked``)
                     when no catalog is registered.
dax_prompt_builder   Assembles the DAX system prompt (MEASURES vs COLUMNS,
                     RELATIONSHIPS, DATE, plan) and the ``structured_prompt`` dict
                     for the UI "Show Prompt" panel.

Measures source: the metadata DB is additive — a curated measure is a
``metadata_columns`` row whose ``data_type`` marks it as a measure (``measure``,
``dax_measure``, ``measure (dax)`` …). This needs no schema change; when a
dataset has no such rows the pipeline degrades gracefully to column aggregation
with a warning (``measures_available=False``).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.agent.langgraph_agent.nodes.catalog import (
    _acquire_catalog,
    _derive_catalog_state,
)
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.connectors.dax_dialect import dax_dialect_rules
from src.metadata import MetadataLoader

logger = logging.getLogger(__name__)

# data_type markers (lower-cased) that identify a curated DAX measure.
_MEASURE_TYPES = {
    "measure",
    "dax_measure",
    "dax measure",
    "measure (dax)",
    "measure(dax)",
    "calculated measure",
}

# Heuristics for locating a Date/Calendar table when metadata doesn't mark one.
_DATE_TABLE_HINTS = ("date", "calendar", "dimdate", "dim_date")
_DATE_COLUMN_TYPES = ("date", "datetime", "timestamp", "datetime2", "smalldatetime")


def _parse_dax_columns(
    columns_text: str,
) -> Tuple[Dict[str, List[str]], List[str], List[str], Dict[str, str], List[str]]:
    """Split the metadata ``columns`` block into columns vs measures.

    Returns ``(table_columns, known_columns, known_measures,
    measure_home_tables, column_lines)`` where ``column_lines`` are the original
    formatted lines for non-measure columns (re-used verbatim in the prompt).
    """
    table_columns: Dict[str, List[str]] = {}
    known_columns: List[str] = []
    known_measures: List[str] = []
    measure_home_tables: Dict[str, str] = {}
    column_lines: List[str] = []
    seen_cols: set = set()
    seen_measures: set = set()

    for raw in columns_text.splitlines():
        stripped = raw.lstrip("- ").strip()
        if not stripped:
            continue
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        table, _, column = qualified.partition(".")
        table = table.strip()
        column = column.strip()
        if not table or not column:
            continue

        data_type = _extract_type(stripped)
        is_measure = data_type in _MEASURE_TYPES

        if is_measure:
            mname = column.lower()
            if mname not in seen_measures:
                seen_measures.add(mname)
                known_measures.append(mname)
                measure_home_tables[mname] = table
            continue

        tl = table.lower()
        cl = column.lower()
        table_columns.setdefault(tl, [])
        if cl not in table_columns[tl]:
            table_columns[tl].append(cl)
        if cl not in seen_cols:
            seen_cols.add(cl)
            known_columns.append(cl)
        column_lines.append(stripped)

    return table_columns, known_columns, known_measures, measure_home_tables, column_lines


def _extract_type(line: str) -> str:
    """Pull the lower-cased ``data_type`` out of a formatted metadata column line."""
    marker = "type:"
    low = line.lower()
    idx = low.find(marker)
    if idx == -1:
        return ""
    rest = line[idx + len(marker):].strip()
    # Type runs until the next attribute separator (", Description:", ", PK:", …).
    for sep in (", Description:", ", PK:", ", NOT NULL", ","):
        pos = rest.find(sep)
        if pos != -1:
            rest = rest[:pos]
            break
    return rest.strip().lower()


def _measure_lines(columns_text: str) -> List[str]:
    """Return the original formatted lines that describe curated measures."""
    lines: List[str] = []
    for raw in columns_text.splitlines():
        stripped = raw.lstrip("- ").strip()
        if not stripped or "." not in stripped.split(" - ")[0]:
            continue
        if _extract_type(stripped) in _MEASURE_TYPES:
            lines.append(stripped)
    return lines


def _detect_date_table(
    known_tables: List[str], table_columns: Dict[str, List[str]], columns_text: str
) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort marked-date-table detection from names + column types."""
    # 1) A table whose name looks like a date/calendar dimension.
    for t in known_tables:
        low = t.lower()
        if any(h in low for h in _DATE_TABLE_HINTS):
            return t, _first_date_column(t, columns_text)
    # 2) Otherwise, the table owning the first date-typed column.
    for raw in columns_text.splitlines():
        stripped = raw.lstrip("- ").strip()
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        if _extract_type(stripped) in _DATE_COLUMN_TYPES:
            table, _, column = qualified.partition(".")
            return table.strip(), f"{table.strip()}[{column.strip()}]"
    return None, None


def _first_date_column(table: str, columns_text: str) -> Optional[str]:
    for raw in columns_text.splitlines():
        stripped = raw.lstrip("- ").strip()
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        t, _, column = qualified.partition(".")
        if t.strip().lower() == table.lower() and _extract_type(stripped) in _DATE_COLUMN_TYPES:
            return f"{table}[{column.strip()}]"
    return None


# ── dax_catalog_lookup ─────────────────────────────────────────────────────────


def make_dax_catalog_lookup(metadata_loader: MetadataLoader, require_catalog: bool = True):
    """Return an async ``dax_catalog_lookup`` node."""

    async def dax_catalog_lookup(state: DaxAgentState) -> Dict[str, Any]:
        source_key = state["source_key"]
        bundle, meta, load_ms, catalog_error = await _acquire_catalog(
            state,
            source_key,
            metadata_loader,
            log_label="dax_catalog_lookup",
            failure_message="Failed to load catalog metadata for this dataset.",
        )

        updates = _derive_catalog_state(
            bundle,
            meta,
            load_ms=load_ms,
            catalog_error=catalog_error,
            require_catalog=require_catalog,
            display=state.get("connection_display_name") or source_key,
            engine="dax",
        )

        # DAX-specific derivation: measures live in the same metadata_columns
        # rows as plain columns and are told apart by data_type, so the split
        # has to happen here rather than in the shared parser.
        columns_text = bundle.get("columns", "")
        (
            table_columns,
            known_columns,
            known_measures,
            measure_home_tables,
            _column_lines,
        ) = _parse_dax_columns(columns_text)
        known_tables = updates["known_tables"]
        date_table, date_column = _detect_date_table(known_tables, table_columns, columns_text)

        updates.update({
            "known_columns": known_columns,
            "table_columns": table_columns,
            "known_measures": known_measures,
            "measure_home_tables": measure_home_tables,
            "measures_available": bool(known_measures),
            "relationship_graph": [],  # raw relationships injected via the prompt
            "date_table": date_table,
            "date_column": date_column,
            "is_marked_date_table": False,
        })

        logger.info(
            "dax_catalog_lookup: %d tables, %d cols, %d measures, date_table=%s via %s (%dms)",
            len(known_tables), len(known_columns), len(known_measures), date_table,
            meta.get("source"), load_ms,
        )
        if updates["catalog_blocked"]:
            logger.warning("dax_catalog_lookup: blocking — no usable catalog for %s", source_key)

        return updates

    return dax_catalog_lookup


# ── dax_prompt_builder ─────────────────────────────────────────────────────────


def make_dax_prompt_builder(prompt_loader: DaxPromptLoader):
    """Return an async ``dax_prompt_builder`` node."""

    async def dax_prompt_builder(state: DaxAgentState) -> Dict[str, Any]:
        bundle = state.get("metadata_bundle") or {}
        display_name = state.get("connection_display_name", "")
        source_key = state.get("source_key", "")
        dataset_id = state.get("dataset_id") or "not specified"
        workspace_id = state.get("workspace_id") or "not specified"
        columns_text = bundle.get("columns", "")

        measures_block = _format_block(_measure_lines(columns_text)) or (
            "No curated measures registered. Fall back to aggregating raw columns "
            "and state the assumption."
        )
        # Non-measure columns re-rendered from the bundle.
        (_tc, _kc, _km, _mh, column_lines) = _parse_dax_columns(columns_text)
        columns_block = _format_block(column_lines) or "No columns registered."

        date_table = state.get("date_table")
        date_desc = (
            f"'{date_table}'" + (
                f" (date column {state.get('date_column')})"
                if state.get("date_column") else ""
            )
            if date_table
            else "No marked Date table detected — group by the most date-like column and note the assumption."
        )

        plan = state.get("query_plan")
        plan_text = json.dumps(plan, indent=2, default=str) if plan else "No plan produced."

        dialect_rules = dax_dialect_rules()
        system_prompt = await prompt_loader.arender(
            "jeen_insights_system_dax",
            connection_display_name=display_name,
            source_key=source_key,
            dataset_id=dataset_id,
            workspace_id=workspace_id,
            dialect_rules=dialect_rules,
            plan=plan_text,
            measures=measures_block,
            columns=columns_block,
            relationships=bundle.get("relationships", "No relationships registered."),
            date_table=date_desc,
            tables=bundle.get("tables", "No tables registered."),
            sources=bundle.get("sources", ""),
            knowledge_pairs=bundle.get("knowledge_pairs", ""),
            business_terms=bundle.get("business_terms", ""),
        )

        structured_prompt: Dict[str, Any] = {
            "engine": "dax",
            "measures": measures_block,
            "columns": columns_block,
            "relationships": bundle.get("relationships", ""),
            "date_table": date_desc,
            "tables": bundle.get("tables", ""),
            "knowledge_pairs": bundle.get("knowledge_pairs", ""),
            "business_terms": bundle.get("business_terms", ""),
            "plan": plan,
            "dialect_rules": dialect_rules,
            "current_question": state.get("question", ""),
            "full_text": system_prompt,
            "connection": {
                "source_key": source_key,
                "display_name": display_name,
                "database_type": "powerbi",
                "dataset_id": state.get("dataset_id"),
                "workspace_id": state.get("workspace_id"),
            },
        }

        logger.info("dax_prompt_builder: system prompt built (%d chars)", len(system_prompt))
        return {
            "system_prompt": system_prompt,
            "structured_prompt": structured_prompt,
            "dialect_rules": dialect_rules,
        }

    return dax_prompt_builder


def _format_block(lines: List[str]) -> str:
    cleaned = [ln for ln in lines if ln and ln.strip()]
    return "\n".join(f"- {ln}" for ln in cleaned) if cleaned else ""


__all__ = ["make_dax_catalog_lookup", "make_dax_prompt_builder"]
