"""sql_repair node — one focused edit of a SQL statement that failed.

The first retry after a syntax or execution error does not need the whole
catalog, the conversation ledger and the full system prompt that
``sql_generator`` carries. It needs the failing SQL, the error, the dialect
rules, the columns of the tables that SQL references and the verified filters.
This node sends exactly that to the large model and routes the corrected SQL
straight back through ``sqlglot_validate``.

It runs at most once per question (``sql_repair_attempts``). If the edit
produces nothing usable, or the very same SQL again, ``repair_failed`` sends the
retry to ``sql_generator`` with the original error context intact.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from src.agent.langgraph_agent.nodes.catalog import _catalog_identifier_parts
from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.nodes.sql_gen import _extract_sql, sql_fingerprint
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage
from src.connectors.dialects import dialect_rules_for, sqlglot_dialect_for
from src.tools.sql_tool import RunSqlTool

logger = logging.getLogger(__name__)

# Fallback caps when the SQL cannot be parsed and the whole catalog is sent.
_MAX_FALLBACK_COLUMN_CHARS = 8000
_MAX_RELATIONSHIP_LINES = 40
_REPAIR_TEMPERATURE = 0.1


def referenced_tables(
    sql: str,
    database_type: Optional[str] = None,
    known_tables: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Lower-cased table names a statement reads from (CTE names included).

    A statement that does not parse is the likely reason we are here, so the
    fallback scans its identifiers; those are intersected with the catalog's
    *known_tables* (when given) so keywords and column names never pass for tables.
    """
    try:
        import sqlglot  # noqa: PLC0415
        from sqlglot import exp  # noqa: PLC0415

        tree = sqlglot.parse_one(sql, dialect=sqlglot_dialect_for(database_type))
        return {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
    except Exception:  # noqa: BLE001
        tokens = {tok.lower() for tok in re.findall(r"[A-Za-z_][\w$]*", sql)}
        known = {t.lower() for t in known_tables or ()}
        return tokens & known if known else tokens


def columns_for_tables(columns_text: str, tables: Set[str]) -> str:
    """The catalog's column lines that belong to *tables*.

    Falls back to a capped copy of the whole block when nothing matches, so a
    SQL that names no known table (the usual missing-table slip) still sees the
    schema it should have used.
    """
    kept: List[str] = []
    for line in columns_text.splitlines():
        stripped = line.lstrip("- ").strip()
        if not stripped:
            continue
        parts = _catalog_identifier_parts(stripped.split(" - ")[0].strip())
        if len(parts) >= 2 and parts[-2].lower() in tables:
            kept.append(line.rstrip())
    if kept:
        return "\n".join(kept)
    return columns_text[:_MAX_FALLBACK_COLUMN_CHARS] or "(none available)"


_RELATIONSHIP_SPLIT_RE = re.compile(r"\),\s*\(|\n")
_EDGE_RE = re.compile(r"([\w\"`\[\].$]+?)\s*->\s*([\w\"`\[\].$]+)")


def relationships_for_tables(relationships_text: str, tables: Set[str]) -> str:
    """The relationships that touch *tables*, one per line.

    The metadata loader renders them as a single ``[('a.x -> b.y',), ...]``
    literal, so entries are split out of that first. A ``table.column ->
    table.column`` entry matches on its table components only; any other entry
    matches on whole identifiers, never on substrings.
    """
    kept: List[str] = []
    for chunk in _RELATIONSHIP_SPLIT_RE.split(relationships_text):
        entry = chunk.strip().strip("[]()").rstrip(",").strip().strip("'\"").strip()
        if not entry:
            continue
        edge = _EDGE_RE.search(entry)
        if edge:
            sides = []
            for side in edge.groups():
                parts = _catalog_identifier_parts(side)
                sides.append(parts[-2].lower() if len(parts) >= 2 else (parts[0].lower() if parts else ""))
            touches = any(side in tables for side in sides)
        else:
            touches = any(tok.lower() in tables for tok in re.findall(r"[A-Za-z_][\w$]*", entry))
        if touches:
            kept.append(entry)
    return "\n".join(kept[:_MAX_RELATIONSHIP_LINES]) or "(none)"


def make_sql_repair(llm: LangChainLlmService, prompt_loader: PromptLoader):
    """Return an async ``sql_repair`` node."""

    async def sql_repair(state: AgentState) -> Dict[str, Any]:
        from src.api.llm_params import QUERY_PARAMS  # noqa: PLC0415

        failing_sql = (state.get("generated_sql") or "").strip()
        db_type = state.get("database_type", "")
        display_name = state.get("connection_display_name", "")
        source_key = state.get("source_key", "")
        database = state.get("connection_database") or ""
        catalog = state.get("connection_catalog") or ""
        schema = state.get("connection_schema") or ""
        bundle = state.get("metadata_bundle") or {}

        tables = referenced_tables(failing_sql, db_type, state.get("known_tables"))
        # The template's documentation header is stripped before any value is
        # substituted, so user text containing ``-->`` cannot end it early.
        prompt = await prompt_loader.arender(
            "sql_repair",
            strip_header=True,
            question=state.get("question", ""),
            failing_sql=failing_sql,
            error=fence_untrusted(state.get("error_context") or "", label="database error message")
            or "(no error text)",
            database_type=db_type,
            dialect_rules=state.get("dialect_rules") or dialect_rules_for(db_type),
            connection_display_name=display_name,
            connection_database=database or "not specified",
            connection_catalog=catalog or "not specified",
            connection_schema=schema or "not specified",
            columns=columns_for_tables(bundle.get("columns", ""), tables),
            relationships=relationships_for_tables(bundle.get("relationships", ""), tables),
            filter_plan=json.dumps(state.get("filter_plan") or {}, ensure_ascii=False),
        )

        tools = [
            RunSqlTool(
                None,  # type: ignore[arg-type]
                connection_display_name=display_name,
                database_type=db_type,
                source_key=source_key,
                catalog=catalog,
                schema=schema,
            ).get_schema()
        ]

        # A repair is still SQL generation, so it follows whichever model the
        # operator assigned to the generator prompt unless it has its own.
        model_override = await prompt_loader.model_override_for("sql_repair")
        if model_override is None:
            model_override = await prompt_loader.model_override_for("jeen_insights_system")

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Return the corrected query as one run_sql call."},
            ],
            temperature=_REPAIR_TEMPERATURE,
            max_tokens=QUERY_PARAMS.max_tokens,
            tools=tools,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        updates: Dict[str, Any] = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, response.get("usage") or {}),
            "node_prompts": {**(state.get("node_prompts") or {}), "sql_repair": prompt},
            "use_local_repair": False,
        }

        repaired = _extract_sql(response)
        seen: List[str] = list(state.get("previous_sql_hashes") or [])
        fingerprint = sql_fingerprint(repaired, db_type) if repaired else None

        if not fingerprint or fingerprint in seen:
            # Nothing new to validate: hand the retry to the full generator with
            # the error context untouched.
            logger.info(
                "sql_repair: %s — falling back to sql_generator",
                "no SQL returned" if not fingerprint else "identical SQL returned",
            )
            return {**updates, "repair_failed": True}

        logger.info("sql_repair: repaired SQL extracted (len=%d)", len(repaired or ""))
        return {
            **updates,
            "repair_failed": False,
            "generated_sql": repaired,
            "clarification": None,
            "error_context": None,
            "sqlglot_error": None,
            "exec_error": None,
            "dlp_blocked": False,
            "governance_error": None,
            "query_result": None,
            "is_trivial": False,
            "eval_result": None,
            "previous_sql_hashes": [*seen, fingerprint],
        }

    return sql_repair
