"""Typed filter planning and value grounding for the SQL LangGraph pipeline.

The SQL generator used to infer both a column and literal spelling while writing
SQL.  This module separates those concerns: a small planning call binds the
user's requested predicates to catalogued columns, then deterministic code
normalizes ranges and verifies categorical literals before the SQL-writing call.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage
from src.connectors import SqlRunner
from src.metadata.identifiers import split_qualified_identifier, table_column_from_identifier
from src.metadata.value_index import (
    ValueDomain,
    exact_value,
    is_refinement_set,
    match_values,
    search_tokens,
    tokenize,
    value_domain_cache,
)

logger = logging.getLogger(__name__)

_TEXT_TYPES = (
    "char", "text", "string", "varchar", "nvarchar", "character",
    "uuid", "json", "object",
)
_NUMERIC_TYPES = (
    "int", "decimal", "numeric", "float", "double", "real", "money",
    "number", "smallint", "bigint",
)
_DATE_TYPES = ("date", "time", "timestamp", "datetime")
_RESOLVABLE_OPS = {"equals", "=", "in", "contains"}
_MAX_FILTERS = 4
_MAX_LOOKUP_VALUES = 25
_MAX_SUGGESTIONS = 5
_SUGGESTION_THRESHOLD = 55.0
_MAX_DOMAIN_VALUES = 1000
_NUMERIC_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_FILTER_INTENT_RE = re.compile(
    r"\b(?:where|with|between|after|before|since|until|from|for|during|"
    r"equals?|equal to|named|called|last\s+\d+|this\s+(?:week|month|year)|"
    r"\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)


def _extract_json(content: str) -> str:
    text = (content or "").strip()
    if "```" in text:
        start = text.find("```") + 3
        if text[start:].startswith("json"):
            start += 4
        end = text.find("```", start)
        text = text[start:end].strip() if end > start else text
    start = text.find("{")
    end = text.rfind("}") + 1
    return text[start:end] if start >= 0 and end > start else text


def _has_filter_intent(question: str) -> bool:
    """Avoid an extra LLM round-trip when a question has no predicate signal."""
    return bool(_FILTER_INTENT_RE.search(question or ""))


def column_types(columns_text: str) -> Dict[Tuple[str, str], str]:
    """Return declared catalog data types keyed by normalised table/column."""
    types: Dict[Tuple[str, str], str] = {}
    for line in (columns_text or "").splitlines():
        stripped = line.lstrip("- ").strip()
        table, column = table_column_from_identifier(stripped.split(" - ", 1)[0].strip())
        if not table or not column:
            continue
        match = re.search(r"\btype\s*:\s*([^,|]+)", stripped, re.IGNORECASE)
        types[(table, column)] = match.group(1).strip().lower() if match else ""
    return types


def _kind_for_type(data_type: str) -> str:
    lowered = (data_type or "").lower()
    if any(token in lowered for token in _DATE_TYPES):
        return "date"
    if any(token in lowered for token in _NUMERIC_TYPES):
        return "number"
    return "text"


def _normalise_op(value: Any) -> str:
    raw = str(value or "equals").strip().lower()
    aliases = {
        "=": "equals", "equal": "equals", "eq": "equals",
        ">": "gt", ">=": "gte", "<": "lt", "<=": "lte",
        "greater": "gt", "greater_than": "gt",
        "less": "lt", "less_than": "lt",
    }
    return aliases.get(raw, raw)


def _canonical_target(filter_dict: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    table = str(filter_dict.get("table") or "").strip()
    column = str(filter_dict.get("column") or "").strip()
    if not table or not column:
        target = str(filter_dict.get("target") or "").strip()
        if target:
            table, column = table_column_from_identifier(target)
    return (table.lower(), column.lower()) if table and column else None


def _normalise_number(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    text = str(value or "").strip()
    if not _NUMERIC_RE.fullmatch(text):
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    if percent:
        number /= Decimal("100")
    rendered = format(number.normalize(), "f")
    return (
        rendered.rstrip("0").rstrip(".")
        if "." in rendered
        else rendered
    )


def _normalise_date(value: Any, *, today: Optional[date] = None) -> Optional[str]:
    """Accept ISO dates and a small, explicit set of relative date expressions."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    anchor = today or datetime.now(timezone.utc).date()
    relative = {
        "today": anchor,
        "yesterday": anchor - timedelta(days=1),
        "tomorrow": anchor + timedelta(days=1),
    }
    if text in relative:
        return relative[text].isoformat()
    if re.fullmatch(r"last\s+\d+\s+days?", text):
        days = int(re.search(r"\d+", text).group())
        return (anchor - timedelta(days=days)).isoformat()
    # Deliberately do not guess whether 03/04 is March 4 or April 3.
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("z", "+00:00")).date().isoformat()
        except ValueError:
            return None


def normalize_typed_filter(
    filter_dict: Dict[str, Any],
    data_type: str,
    *,
    today: Optional[date] = None,
    normalize_numeric_scalar: bool = True,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Normalize number/date predicate operands or return a user-facing reason."""
    kind = _kind_for_type(data_type)
    op = _normalise_op(filter_dict.get("op"))
    out = dict(filter_dict)
    out["op"] = op
    if kind == "text":
        return out, None
    # DAX treats scalar numeric values as expressions and keeps its planner's
    # distinction intact; SQL opts in to canonical numeric literals. Both
    # engines normalize range bounds, where ordering and locale formatting
    # materially affect semantics.
    if kind == "number" and op != "between" and not normalize_numeric_scalar:
        return out, None

    normalise = _normalise_date if kind == "date" else _normalise_number
    raw = out.get("value")
    if op == "between":
        values = raw if isinstance(raw, (list, tuple)) else []
        if len(values) != 2:
            return out, f"A {kind} range needs a start and end value."
        if kind == "date":
            normalized = [_normalise_date(v, today=today) for v in values]
        else:
            normalized = [normalise(v) for v in values]
        if any(v is None for v in normalized):
            return out, f"I couldn't read that {kind} range unambiguously."
        if Decimal(normalized[0].replace("-", "")) > Decimal(normalized[1].replace("-", "")):
            return out, f"The {kind} range starts after it ends."
        out["value"] = normalized
        out["resolved"] = True
        return out, None

    normalized = _normalise_date(raw, today=today) if kind == "date" else normalise(raw)
    if normalized is None:
        return out, f"I couldn't read '{raw}' as a {kind} for this filter."
    out["value"] = normalized
    out["resolved"] = True
    return out, None


def _quote_identifier(value: str, database_type: str) -> str:
    """Quote a catalog identifier after splitting it; identifiers are never raw SQL."""
    quote = "`" if (database_type or "").lower() in {"databricks", "spark"} else '"'
    escaped = [part.replace(quote, quote * 2) for part in split_qualified_identifier(value)]
    return ".".join(f"{quote}{part}{quote}" for part in escaped)


class SqlValueProbe:
    """Bounded, read-only column-value probe for non-MCP SQL connections."""

    def __init__(self, runner: SqlRunner, database_type: str) -> None:
        self._runner = runner
        self._database_type = database_type

    async def values(
        self, table: str, column: str, query: str, *, limit: int
    ) -> Dict[str, Any]:
        table_sql = _quote_identifier(table, self._database_type)
        column_sql = _quote_identifier(column, self._database_type)
        bounded = max(1, min(int(limit), _MAX_DOMAIN_VALUES))
        search_parts = search_tokens(query)
        # A complete distinct domain is useful for proving absence and safe
        # refinement. For long text, a bounded server-side search is cheaper but
        # intentionally marked incomplete.
        if search_parts:
            cast_type = "STRING" if (self._database_type or "").lower() in {"databricks", "spark"} else "VARCHAR"
            predicates = [
                f"LOWER(CAST({column_sql} AS {cast_type})) LIKE '%{part.replace('%', '%%').replace('_', '__')}%'"
                for part in search_parts
            ]
            sql = (
                f"SELECT DISTINCT {column_sql} AS value FROM {table_sql} "
                f"WHERE {column_sql} IS NOT NULL AND {' AND '.join(predicates)} "
                f"LIMIT {bounded + 1}"
            )
        else:
            sql = (
                f"SELECT DISTINCT {column_sql} AS value FROM {table_sql} "
                f"WHERE {column_sql} IS NOT NULL LIMIT {bounded + 1}"
            )
        result = await self._runner.run_sql(
            sql, limit=bounded + 1, max_rows=bounded + 1, statement_timeout_ms=5000
        )
        if result.get("error"):
            return {"values": [], "complete": False, "source": "db"}
        rows = result.get("rows") or []
        values = [
            str(row.get("value")).strip()
            for row in rows
            if isinstance(row, dict) and row.get("value") is not None
        ]
        return {
            "values": values[:bounded],
            "complete": not search_parts and len(values) <= bounded,
            "source": "db",
        }


async def _lookup_values(
    state: AgentState,
    probe: SqlValueProbe,
    table: str,
    column: str,
    needle: str,
    limit: int,
) -> Dict[str, Any]:
    """Use MCP only when it declares caller visibility; otherwise probe SQL."""
    if state.get("catalog_source_used") == "mcp":
        try:
            from src.api import state as app_state
            if (
                app_state.mcp_catalog_client
                and await app_state.mcp_catalog_client.value_search_preserves_user_visibility()
            ):
                result = await app_state.mcp_catalog_client.search_column_values(
                    str(state.get("source_key") or ""),
                    table=table,
                    column=column,
                    query=needle,
                    limit=limit,
                )
                if result.get("values"):
                    return result
        except Exception:  # noqa: BLE001 - value grounding must fail open
            logger.debug("filter_grounder: MCP value search unavailable", exc_info=True)
    return await probe.values(table, column, needle, limit=limit)


def _classify(needle: str, values: Sequence[str], threshold: float) -> Tuple[str, List[str], bool]:
    exact = exact_value(needle, values)
    if exact is not None:
        return "single", [exact], True
    matches = match_values(needle, values, limit=_MAX_LOOKUP_VALUES + 1, threshold=threshold)
    covered = [match for match in matches if match.covers_needle]
    if len(covered) == 1:
        return "single", [covered[0].value], False
    if is_refinement_set(needle, covered) and len(covered) <= _MAX_LOOKUP_VALUES:
        return "refinement", [match.value for match in covered], False
    if matches:
        return "ambiguous", [match.value for match in matches[:_MAX_SUGGESTIONS]], False
    return "none", [], False


def _clarification(items: Sequence[Dict[str, Any]]) -> str:
    messages: List[str] = []
    for item in items:
        candidates = item.get("candidates") or []
        value = str(item.get("value") or "")
        column = str(item.get("column") or "that field")
        if candidates:
            quoted = ", ".join(f"'{candidate}'" for candidate in candidates)
            messages.append(f'I found several possible values for "{value}" in {column}: {quoted}. Which did you mean?')
        else:
            messages.append(f'I could not verify "{value}" in {column}. Please provide the exact value.')
    return " ".join(messages)


def make_filter_planner(llm: LangChainLlmService, prompt_loader: PromptLoader):
    """Create the small-model planner that binds filter intent to catalog columns."""

    async def filter_planner(state: AgentState) -> Dict[str, Any]:
        bundle = state.get("metadata_bundle") or {}
        if not _has_filter_intent(state.get("question", "")):
            return {
                "filter_plan": {"filters": [], "invalid_filters": []},
                "filter_clarification_required": False,
                "filter_resolution_attempts": 0,
            }
        prompt = await prompt_loader.arender(
            "sql_filter_planner",
            question=state.get("question", ""),
            columns=bundle.get("columns", ""),
            column_statistics=bundle.get("column_statistics", ""),
            column_samples=bundle.get("column_samples", ""),
            business_terms=bundle.get("business_terms", ""),
        )
        model_override = await prompt_loader.model_override_for("sql_filter_planner")
        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": state.get("question", "")},
            ],
            temperature=0.0,
            max_tokens=700,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        content = response.get("content") or ""
        try:
            parsed = json.loads(_extract_json(content))
            raw_filters = parsed.get("filters") if isinstance(parsed, dict) else []
            filters = raw_filters if isinstance(raw_filters, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            filters = []

        table_columns = state.get("table_columns") or {}
        types = column_types(bundle.get("columns", ""))
        valid: List[Dict[str, Any]] = []
        invalid: List[Dict[str, Any]] = []
        for raw in filters[:_MAX_FILTERS]:
            if not isinstance(raw, dict):
                continue
            target = _canonical_target(raw)
            if not target or target[0] not in table_columns or target[1] not in table_columns[target[0]]:
                invalid.append(raw)
                continue
            item = dict(raw)
            item["table"], item["column"] = target
            item["target"] = f"{target[0]}.{target[1]}"
            item["op"] = _normalise_op(item.get("op"))
            item["data_type"] = types.get(target, "")
            item["raw_value"] = item.get("value")
            item["resolved"] = False
            valid.append(item)

        clarification = ""
        if invalid:
            clarification = "I need clarification about which database field to use for the requested filter."
        usage = response.get("usage") or {}
        return {
            "filter_plan": {"filters": valid, "invalid_filters": invalid},
            "filter_clarification_required": bool(clarification),
            "clarification": clarification or None,
            "answer": clarification or None,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "filter_planner": prompt},
        }

    return filter_planner


def make_filter_grounder(
    sql_runner: SqlRunner,
    *,
    enabled: bool = True,
    max_domain_values: int = _MAX_DOMAIN_VALUES,
    match_threshold: float = 78.0,
    governed_columns: Optional[Sequence[str]] = None,
):
    """Ground text filters and normalize typed ranges before SQL generation."""
    probe = SqlValueProbe(sql_runner, sql_runner.database_type)
    governed = {
        part.strip().lower()
        for item in (governed_columns or [])
        for part in str(item).replace(",", " ").split()
        if part.strip()
    }
    governed_pattern = re.compile(
        r"(?:^|_)(?:password|ssn|social_security|credit_card|card_number|pin|"
        r"secret|private_key|api_key|access_token)(?:$|_)",
        re.IGNORECASE,
    )

    async def filter_grounder(state: AgentState) -> Dict[str, Any]:
        attempts = int(state.get("filter_resolution_attempts") or 0) + 1
        if not state.get("filter_resolution_enabled", enabled):
            return {"filter_resolution_attempts": attempts}
        plan = dict(state.get("filter_plan") or {})
        filters = list(plan.get("filters") or [])
        if not filters:
            return {
                "filter_resolution_attempts": attempts,
                "resolved_filters": [],
                "unresolved_filters": [],
                "filter_ambiguities": [],
            }

        user_id = str(state.get("user_id") or "")
        source_key = str(state.get("source_key") or "")
        resolved: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        ambiguous: List[Dict[str, Any]] = []
        normalized_filters: List[Dict[str, Any]] = []
        for raw in filters:
            item = dict(raw)
            target = _canonical_target(item)
            if not target:
                unresolved.append({"target": item.get("target"), "reason": "unparseable target"})
                normalized_filters.append(item)
                continue
            table, column = target
            item["table"], item["column"], item["target"] = table, column, f"{table}.{column}"
            if (
                column.lower() in governed
                or item["target"].lower() in governed
                or governed_pattern.search(column)
            ):
                unresolved.append(
                    {"target": item["target"], "value": item.get("value"), "reason": "governed column"}
                )
                normalized_filters.append(item)
                continue
            data_type = str(item.get("data_type") or "")
            item, error = normalize_typed_filter(item, data_type)
            if error:
                ambiguous.append({"column": column, "value": item.get("value"), "candidates": [], "reason": error})
                normalized_filters.append(item)
                continue
            if _kind_for_type(data_type) != "text" or item.get("op") not in _RESOLVABLE_OPS:
                if item.get("resolved"):
                    resolved.append(item)
                normalized_filters.append(item)
                continue

            raw_values = item.get("value")
            needles = raw_values if isinstance(raw_values, list) else [raw_values]
            if not all(isinstance(value, str) and tokenize(value) for value in needles):
                unresolved.append({"target": item["target"], "value": raw_values, "reason": "non-text literal"})
                normalized_filters.append(item)
                continue

            canonical_values: List[str] = []
            failed = False
            for needle in needles:
                cache_key = value_domain_cache.key(source_key, user_id, table, column, needle)
                cached = value_domain_cache.get(cache_key)
                if cached is not None:
                    lookup = {"values": list(cached.values), "complete": cached.complete, "source": "cache"}
                else:
                    lookup = await _lookup_values(
                        state, probe, table, column, needle,
                        int(state.get("filter_max_domain_values") or max_domain_values),
                    )
                    if lookup.get("complete") and lookup.get("values"):
                        value_domain_cache.put(
                            cache_key,
                            ValueDomain(tuple(lookup["values"]), complete=True),
                        )
                kind, candidates, _exact = _classify(
                    needle, lookup.get("values") or [],
                    float(state.get("filter_match_threshold") or match_threshold),
                )
                if kind == "single":
                    canonical_values.extend(candidates)
                elif kind == "refinement" and lookup.get("complete"):
                    canonical_values.extend(candidates)
                elif kind == "ambiguous":
                    ambiguous.append({"column": column, "value": needle, "candidates": candidates})
                    failed = True
                    break
                elif lookup.get("complete"):
                    suggestions = [
                        match.value for match in match_values(
                            needle, lookup.get("values") or [],
                            limit=_MAX_SUGGESTIONS, threshold=_SUGGESTION_THRESHOLD,
                        )
                    ]
                    ambiguous.append({"column": column, "value": needle, "candidates": suggestions})
                    failed = True
                    break
                else:
                    unresolved.append({"target": item["target"], "value": needle, "reason": "lookup incomplete"})
                    failed = True
                    break
            if failed:
                normalized_filters.append(item)
                continue
            deduped = list(dict.fromkeys(canonical_values))
            item["value"] = deduped[0] if len(deduped) == 1 else deduped
            item["op"] = "equals" if len(deduped) == 1 else "in"
            item["resolved"] = True
            item["lookup_source"] = lookup.get("source")
            resolved.append(item)
            normalized_filters.append(item)

        plan["filters"] = normalized_filters
        clarification = _clarification(ambiguous) if ambiguous else None
        return {
            "filter_plan": plan,
            "resolved_filters": resolved,
            "unresolved_filters": unresolved,
            "filter_ambiguities": ambiguous,
            "filter_resolution_attempts": attempts,
            "filter_clarification_required": bool(clarification),
            "clarification": clarification,
            "answer": clarification,
        }

    return filter_grounder


def empty_filter_result_check(state: AgentState) -> Dict[str, Any]:
    """Request one extra grounding pass for an empty result with unresolved text."""
    result = state.get("query_result") or {}
    rows = result.get("rows") or []
    diagnostics = int(state.get("empty_filter_diagnostics") or 0)
    retry = bool(not rows and state.get("unresolved_filters") and diagnostics < 1)
    return {
        "needs_filter_reground": retry,
        "empty_filter_diagnostics": diagnostics + 1 if retry else diagnostics,
    }


__all__ = [
    "SqlValueProbe",
    "column_types",
    "empty_filter_result_check",
    "make_filter_grounder",
    "make_filter_planner",
    "normalize_typed_filter",
]
