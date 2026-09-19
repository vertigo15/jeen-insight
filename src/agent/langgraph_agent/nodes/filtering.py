"""Typed filter planning and value grounding for the SQL LangGraph pipeline.

The SQL generator used to infer both a column and literal spelling while writing
SQL.  This module separates those concerns: a small planning call binds the
user's requested predicates to catalogued columns, then deterministic code
normalizes ranges and verifies categorical literals before the SQL-writing call.

Value evidence is read **metadata first** (Schema Modeler's column profiles and
captured values, see :mod:`src.metadata.value_store`) and the customer's
warehouse is touched only to confirm what the metadata could not certify:

  T0  in-process cache of a column's domain (per source / visibility / snapshot)
  T1  metadata store: complete + fresh domain → typo corrected locally;
      partial → candidates; reverse lookup → which columns hold the value
  T2  one-row point probe ``WHERE col = <canonical> LIMIT 1`` to confirm a
      single strong candidate from a partial or stale snapshot
  T3  ``SELECT DISTINCT`` of a small, uncaptured domain (opt-in; only on
      connectors that stop the statement server-side)
  T4  bounded ``LIKE`` search — candidates only, never auto-applied

Which column a literal belongs to is decided deterministically from the
planner's choice, the reverse lookup, join reachability and the words of the
question (see :func:`decide_column`); competing business roles ("customer city"
vs "dealer city") are put to the user as a structured choice instead of being
guessed, and a single unambiguous retarget is applied *and disclosed*.
"""

from __future__ import annotations

import asyncio
import calendar
import fnmatch
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage
from src.connectors import SqlRunner
from src.connectors.base import sql_string_literal
from src.metadata.identifiers import split_qualified_identifier, table_column_from_identifier
from src.metadata.value_index import (
    SHARED_VISIBILITY,
    ValueDomain,
    bidi_isolate,
    exact_value,
    is_refinement_set,
    match_values,
    normalize,
    search_tokens,
    tokenize,
    value_domain_cache,
)
from src.metadata.value_store import (
    CaptureContract,
    ColumnHit,
    ColumnProfile,
    DomainEvidence,
    NullValueStore,
    ValueStore,
    rank_hits,
    reachable_tables,
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
_MAX_OPTIONS = 4
_SUGGESTION_THRESHOLD = 55.0
_MAX_DOMAIN_VALUES = 1000
# A T3 domain fetch is only affordable when the profile proves the table small.
_MAX_DISTINCT_ROWS_FOR_SOURCE_DISTINCT = 250_000
# Source probes per request, across all filters (a semaphore bounds concurrency).
_MAX_SOURCE_PROBES = 2
_MAX_REVERSE_NEEDLES = 6
# A fuzzy candidate strong enough to be *confirmed* by one point probe rather
# than put to the user.
_STRONG_CANDIDATE_SCORE = 88.0
# Reverse-lookup similarity (0-1) that makes the planner run even without a
# predicate cue word in the question.
_REVERSE_TRIGGER_SIMILARITY = 0.6
_NUMERIC_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_FILTER_INTENT_RE = re.compile(
    r"\b(?:where|with|between|after|before|since|until|from|for|during|in|on|"
    r"equals?|equal to|named|called|last\s+\d+|this\s+(?:week|month|year)|"
    r"\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[\"'“”‘’«»](.+?)[\"'“”‘’«»]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_STOPWORDS: Set[str] = {
    # English function/analytics words
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "by", "with",
    "is", "are", "was", "were", "be", "at", "as", "from", "that", "this", "it",
    "how", "what", "which", "who", "when", "where", "why", "show", "list", "get",
    "give", "find", "count", "total", "sum", "avg", "average", "number", "many",
    "much", "top", "all", "each", "per", "me", "my", "we", "our", "you", "please",
    "than", "more", "less", "last", "first", "between", "during", "after", "before",
    "since", "until", "into", "over", "under", "about", "per", "vs", "versus",
    "year", "years", "month", "months", "week", "weeks", "day", "days", "today",
    "yesterday", "quarter", "sales", "sale", "revenue", "profit", "orders", "order",
    "customers", "customer", "products", "product", "data", "rows", "results",
    "cars", "car", "items", "item", "amount", "value", "values", "name", "names",
    # Hebrew function words (prefix-attached forms are handled by the matcher)
    "של", "את", "על", "עם", "כל", "לפי", "בין", "מה", "כמה", "איזה", "אילו", "תראה",
    "הצג", "רשימה", "סכום", "ממוצע", "מספר", "או", "גם", "לא", "יש", "אין", "זה",
}
# Identifier tokens that never distinguish one business role from another.
_GENERIC_ROLE_TOKENS: Set[str] = {
    "dim", "fact", "fct", "tbl", "table", "id", "key", "name", "code", "desc",
    "description", "type", "value", "text", "str", "data", "col", "column",
}
# Column names that are never probed or shown whatever the profile says
# (matched after separator/camel-case normalisation, see _governed_pattern_text).
_GOVERNED_NAME_RE = re.compile(
    r"(?:^|_)(?:password|passwd|ssn|social_security(?:_number)?|national_id|credit_card|"
    r"card_number|pin|secret|private_key|api_key|access_token|refresh_token)(?:$|_)",
    re.IGNORECASE,
)
# Bumped whenever normalize()/tokenize() semantics change so cached domains
# built under the old normalisation are never reused.
_NORMALIZATION_VERSION = "n2"
# A value the user typed into a clarification is request input; cap it like
# any other literal before it can become a predicate.
_MAX_USER_VALUE_CHARS = 512


def _governed_pattern_text(column: str) -> str:
    """Normalise ``Social Security Number`` / ``socialSecurityNumber`` to snake_case."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(column or ""))
    return re.sub(r"[\s\-.]+", "_", spaced).lower()


def _governed_set(governed_columns: Optional[Sequence[str]]) -> Set[str]:
    return {
        part.strip().lower()
        for item in (governed_columns or [])
        for part in str(item).replace(",", " ").split()
        if part.strip()
    }


def _denylist(state: AgentState) -> Tuple[str, ...]:
    return tuple(
        p.strip().lower() for p in str(state.get("filter_probe_denylist") or "").split(",") if p.strip()
    )


def denied_by_name(table: str, column: str, governed: Set[str], denylist: Sequence[str]) -> bool:
    """Name-level governance shared by the planner (display) and grounder (probe)."""
    target = f"{table}.{column}".lower()
    if column.lower() in governed or target in governed:
        return True
    if _GOVERNED_NAME_RE.search(_governed_pattern_text(column)):
        return True
    return any(fnmatch.fnmatchcase(target, pattern) for pattern in denylist)


# ── JSON / intent helpers ─────────────────────────────────────────────────────


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


def column_descriptions(columns_text: str) -> Dict[Tuple[str, str], str]:
    """Return catalog descriptions keyed by normalised table/column."""
    out: Dict[Tuple[str, str], str] = {}
    for line in (columns_text or "").splitlines():
        stripped = line.lstrip("- ").strip()
        table, column = table_column_from_identifier(stripped.split(" - ", 1)[0].strip())
        if not table or not column:
            continue
        match = re.search(r"\bdescription\s*:\s*([^|]+?)(?:,\s*(?:PK|NOT NULL)\b.*)?$", stripped, re.IGNORECASE)
        if match:
            out[(table, column)] = match.group(1).strip()
    return out


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
    """Lower-cased ``(table, column)`` the planner proposed, in catalog-key form.

    The model echoes identifiers the way the prompt showed them, which for a
    schema-qualified catalog is ``public.factinternetsales`` or
    ``"public"."factinternetsales"``; the allowlist is keyed by the bare table
    name, so the proposal is reduced the same way the catalog lines were.
    """
    table = str(filter_dict.get("table") or "").strip()
    column = str(filter_dict.get("column") or "").strip()
    if table:
        parts = split_qualified_identifier(table)
        table = parts[-1] if parts else table
    if column:
        parts = split_qualified_identifier(column)
        column = parts[-1] if parts else column
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


# Exact month names or three-letter abbreviations only: "marketing 2008" is not March.
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december")
_MONTH_NUMBER: Dict[str, int] = {name: i for i, name in enumerate(_MONTH_NAMES, start=1)}
_MONTH_NUMBER.update({name[:3]: i for i, name in enumerate(_MONTH_NAMES, start=1)})
_MONTH_NUMBER["sept"] = 9
_MONTH_WORD = r"(?:" + "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True)) + r")"
_MONTH_YEAR_RE = re.compile(r"(\d{1,2})[/-](\d{4})")                       # 7/2008, 07-2008
_YEAR_MONTH_RE = re.compile(r"(\d{4})[/-](\d{1,2})")                       # 2008-07, 2008/7
_MONTH_NAME_YEAR_RE = re.compile(rf"({_MONTH_WORD})\.?,?[\s-]*(\d{{4}})")  # jul 2008, july 2008, jul-2008
_YEAR_MONTH_NAME_RE = re.compile(rf"(\d{{4}})[\s-]+({_MONTH_WORD})")       # 2008 jul
_QUARTER_YEAR_RE = re.compile(r"q([1-4])[\s-]*(\d{4})")                    # q3 2008, q3-2008
_YEAR_QUARTER_RE = re.compile(r"(\d{4})[\s-]*q([1-4])")                    # 2008 q3, 2008-q3
_YEAR_RE = re.compile(r"\d{4}")
# Years a business date column can plausibly hold; also keeps ``date()`` in range.
_YEAR_MIN, _YEAR_MAX = 1900, 2200


def _month_span(year: int, month: int) -> Optional[Tuple[date, date]]:
    if not (1 <= month <= 12 and _YEAR_MIN <= year <= _YEAR_MAX):
        return None
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _quarter_span(year: int, quarter: int) -> Optional[Tuple[date, date]]:
    first = _month_span(year, 3 * quarter - 2)
    last = _month_span(year, 3 * quarter)
    return (first[0], last[1]) if first and last else None


def _date_period(value: Any, *, today: Optional[date] = None) -> Optional[Tuple[date, date]]:
    """The closed ``[first, last]`` day range a date literal denotes.

    A full date is one day. A month written unambiguously — ``7/2008``,
    ``2008-07``, ``Jul 2008`` — is its first to last day; a quarter (``Q3
    2008``) and a bare year are likewise whole. A day-month-year with three
    numeric parts is refused: whether 03/04 is March 4 or April 3 is a guess
    this code deliberately never makes. Never raises; unreadable is ``None``.
    """
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
        return relative[text], relative[text]
    if re.fullmatch(r"last\s+\d+\s+days?", text):
        days = int(re.search(r"\d+", text).group())
        start = anchor - timedelta(days=days)
        return start, start
    try:
        day = date.fromisoformat(text)
        return day, day
    except ValueError:
        pass
    try:
        day = datetime.fromisoformat(text.replace("z", "+00:00")).date()
        return day, day
    except ValueError:
        pass
    if m := _MONTH_YEAR_RE.fullmatch(text):
        return _month_span(int(m.group(2)), int(m.group(1)))
    if m := _YEAR_MONTH_RE.fullmatch(text):
        return _month_span(int(m.group(1)), int(m.group(2)))
    if m := _MONTH_NAME_YEAR_RE.fullmatch(text):
        return _month_span(int(m.group(2)), _MONTH_NUMBER[m.group(1)])
    if m := _YEAR_MONTH_NAME_RE.fullmatch(text):
        return _month_span(int(m.group(1)), _MONTH_NUMBER[m.group(2)])
    if m := _QUARTER_YEAR_RE.fullmatch(text):
        return _quarter_span(int(m.group(2)), int(m.group(1)))
    if m := _YEAR_QUARTER_RE.fullmatch(text):
        return _quarter_span(int(m.group(1)), int(m.group(2)))
    if _YEAR_RE.fullmatch(text) and _YEAR_MIN <= int(text) <= _YEAR_MAX:
        year = int(text)
        return date(year, 1, 1), date(year, 12, 31)
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

    raw = out.get("value")
    # "Last N days" denotes an interval, not one calendar day. Convert it
    # before scalar date normalization so the SQL/DAX generator cannot turn a
    # range request into `date_column = <start-date>`.
    if kind == "date" and op == "equals":
        relative_days = re.fullmatch(r"last\s+(\d+)\s+days?", str(raw or "").strip().lower())
        if relative_days:
            anchor = today or datetime.now(timezone.utc).date()
            days = int(relative_days.group(1))
            out["op"] = "between"
            out["value"] = [
                (anchor - timedelta(days=days)).isoformat(),
                anchor.isoformat(),
            ]
            out["resolved"] = True
            return out, None
    if op == "between":
        values = raw if isinstance(raw, (list, tuple)) else []
        if len(values) != 2:
            return out, f"A {kind} range needs a start and end value."
        if kind == "date":
            # A month or year as a bound covers the whole of it: "7/2008 to
            # 1/2009" runs from 1 Jul 2008 to 31 Jan 2009.
            periods = [_date_period(v, today=today) for v in values]
            if any(p is None for p in periods):
                return out, f"I couldn't read that {kind} range unambiguously."
            normalized = [periods[0][0].isoformat(), periods[1][1].isoformat()]
        else:
            normalized = [_normalise_number(v) for v in values]
            if any(v is None for v in normalized):
                return out, f"I couldn't read that {kind} range unambiguously."
        if Decimal(normalized[0].replace("-", "")) > Decimal(normalized[1].replace("-", "")):
            return out, f"The {kind} range starts after it ends."
        out["value"] = normalized
        out["resolved"] = True
        return out, None

    if kind == "date":
        period = _date_period(raw, today=today)
        if period is None:
            return out, f"I couldn't read '{raw}' as a {kind} for this filter."
        first, last = period
        if first != last and op == "equals":
            # "= July 2008" means the whole month, not its first day.
            out["op"] = "between"
            out["value"] = [first.isoformat(), last.isoformat()]
        else:
            # >= / < start at the first day; > / <= run to the last.
            out["value"] = (last if op in ("gt", "lte") else first).isoformat()
        out["resolved"] = True
        return out, None

    normalized = _normalise_number(raw)
    if normalized is None:
        return out, f"I couldn't read '{raw}' as a {kind} for this filter."
    out["value"] = normalized
    out["resolved"] = True
    return out, None


# ── Source probes (T2 / T3 / T4) ──────────────────────────────────────────────


def _quote_identifier(value: str, database_type: str) -> str:
    """Quote a catalog identifier after splitting it; identifiers are never raw SQL."""
    quote = "`" if (database_type or "").lower() in {"databricks", "spark"} else '"'
    escaped = [part.replace(quote, quote * 2) for part in split_qualified_identifier(value)]
    return ".".join(f"{quote}{part}{quote}" for part in escaped)


class SqlValueProbe:
    """Bounded, read-only column-value probes against the source itself.

    Every probe is issued with a statement timeout; callers only use a probe on
    a runner whose ``supports_server_side_timeout`` is true, so a probe the
    client abandons cannot keep scanning the warehouse.
    """

    def __init__(self, runner: SqlRunner, database_type: str) -> None:
        self._runner = runner
        self._database_type = database_type

    @property
    def can_probe(self) -> bool:
        return bool(getattr(self._runner, "supports_server_side_timeout", False))

    def _cast_type(self) -> str:
        return "STRING" if (self._database_type or "").lower() in {"databricks", "spark"} else "VARCHAR"

    async def distinct_values(self, table: str, column: str, *, limit: int, timeout_ms: int) -> Dict[str, Any]:
        """T3: the column's distinct values; complete when they fit under ``limit``."""
        table_sql = _quote_identifier(table, self._database_type)
        column_sql = _quote_identifier(column, self._database_type)
        bounded = max(1, min(int(limit), _MAX_DOMAIN_VALUES))
        sql = (
            f"SELECT DISTINCT {column_sql} AS value FROM {table_sql} "
            f"WHERE {column_sql} IS NOT NULL LIMIT {bounded + 1}"
        )
        result = await self._runner.run_sql(
            sql, limit=bounded + 1, max_rows=bounded + 1, statement_timeout_ms=timeout_ms
        )
        if result.get("error"):
            return {"values": [], "complete": False, "source": "db", "error": result.get("error")}
        values = [str(row.get("value")) for row in (result.get("rows") or [])
                  if isinstance(row, dict) and row.get("value") is not None]
        return {"values": values[:bounded], "complete": len(values) <= bounded, "source": "db"}

    async def contains_values(self, table: str, column: str, query: str, *, limit: int, timeout_ms: int) -> Dict[str, Any]:
        """T4: values containing every search fragment of *query*. Never complete."""
        table_sql = _quote_identifier(table, self._database_type)
        column_sql = _quote_identifier(column, self._database_type)
        bounded = max(1, min(int(limit), _MAX_DOMAIN_VALUES))
        # ``search_tokens`` yields normalised word fragments only (no quotes or
        # wildcards survive ``normalize``), so the fragments are literal-safe.
        parts = search_tokens(query)
        if not parts:
            return {"values": [], "complete": False, "source": "db"}
        cast_type = self._cast_type()
        predicates = [
            f"LOWER(CAST({column_sql} AS {cast_type})) LIKE "
            f"{sql_string_literal('%' + part + '%', self._database_type)}"
            for part in parts
        ]
        sql = (
            f"SELECT DISTINCT {column_sql} AS value FROM {table_sql} "
            f"WHERE {column_sql} IS NOT NULL AND {' AND '.join(predicates)} "
            f"LIMIT {bounded + 1}"
        )
        result = await self._runner.run_sql(
            sql, limit=bounded + 1, max_rows=bounded + 1, statement_timeout_ms=timeout_ms
        )
        if result.get("error"):
            return {"values": [], "complete": False, "source": "db", "error": result.get("error")}
        values = [str(row.get("value")) for row in (result.get("rows") or [])
                  if isinstance(row, dict) and row.get("value") is not None]
        return {"values": values[:bounded], "complete": False, "source": "db"}

    async def exists(self, table: str, column: str, value: str, *, timeout_ms: int) -> Optional[bool]:
        """T2: whether *value* (exact bytes) is present. ``None`` on probe error."""
        table_sql = _quote_identifier(table, self._database_type)
        column_sql = _quote_identifier(column, self._database_type)
        sql = (
            f"SELECT 1 AS present FROM {table_sql} "
            f"WHERE {column_sql} = {sql_string_literal(value, self._database_type)} LIMIT 1"
        )
        result = await self._runner.run_sql(sql, limit=1, max_rows=1, statement_timeout_ms=timeout_ms)
        if result.get("error"):
            return None
        return bool(result.get("rows"))

    # Backwards-compatible name used by older callers/tests.
    async def values(self, table: str, column: str, query: str, *, limit: int) -> Dict[str, Any]:
        if search_tokens(query):
            return await self.contains_values(table, column, query, limit=limit, timeout_ms=5000)
        return await self.distinct_values(table, column, limit=limit, timeout_ms=5000)


# ── Classification ────────────────────────────────────────────────────────────


def _classify(needle: str, values: Sequence[str], threshold: float) -> Tuple[str, List[str], bool]:
    """Classify *needle* against *values*: (kind, candidates, is_exact).

    kind is ``single`` (one canonical value), ``refinement`` (the phrase plus
    qualifiers → IN), ``ambiguous`` (competing values) or ``none``.
    """
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


def _best_score(needle: str, value: str) -> float:
    matches = match_values(needle, [value], limit=1, threshold=0.0)
    return matches[0].score if matches else 0.0


def _is_forecast_question(state: AgentState) -> bool:
    """On the analysis route with a forecast cue, a named period is the horizon."""
    if state.get("route") != "needs_analysis":
        return False
    from src.agent.analysis_planner import detect_analysis_intent  # noqa: PLC0415

    return detect_analysis_intent(str(state.get("question") or "")) == "forecast"


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in str(text or ""))


# ── Literal extraction & role words ───────────────────────────────────────────


def _identifier_tokens(*names: str) -> Set[str]:
    tokens: Set[str] = set()
    for name in names:
        spaced = re.sub(r"[_\-.\s]+", " ", str(name or ""))
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
        for part in spaced.lower().split():
            if len(part) >= 2:
                tokens.add(part)
                if part.endswith("s") and len(part) > 3:
                    tokens.add(part[:-1])
    return tokens


def extract_candidate_literals(question: str, table_columns: Dict[str, Sequence[str]]) -> List[str]:
    """Words of the question that could be a value stored in some column.

    Quoted phrases are taken whole. Otherwise a token qualifies when it is a
    word of three or more letters, is not a stopword, carries no digit
    (numbers and codes are grounded as typed literals, not looked up) and is
    not a token of a table or column name (those are schema references). The
    result only *ranks* columns for the planner; it never creates a filter.
    """
    text = str(question or "")
    literals: List[str] = []
    seen: Set[str] = set()

    def add(candidate: str) -> None:
        cleaned = " ".join(candidate.split())
        key = normalize(cleaned)
        if cleaned and key and key not in seen and not _has_digit(cleaned):
            seen.add(key)
            literals.append(cleaned)

    quoted_words: Set[str] = set()
    for quoted in _QUOTED_RE.findall(text):
        if 1 <= len(tokenize(quoted)) <= 4:
            add(quoted)
            quoted_words |= set(tokenize(quoted))
    schema_tokens: Set[str] = set()
    for table, columns in (table_columns or {}).items():
        schema_tokens |= _identifier_tokens(table, *columns)
    for word in _WORD_RE.findall(text):
        lowered = normalize(word)
        if len(lowered) < 3 or lowered in _STOPWORDS or lowered in schema_tokens or lowered in quoted_words:
            continue
        add(word)
    return literals[:_MAX_REVERSE_NEEDLES]


def _role_tokens(table: str, column: str, others: Sequence[Tuple[str, str]]) -> Set[str]:
    """Identifier tokens that distinguish (table, column) from the other candidates."""
    own = _identifier_tokens(table, column) - _GENERIC_ROLE_TOKENS
    shared: Optional[Set[str]] = None
    for other_table, other_column in others:
        tokens = _identifier_tokens(other_table, other_column)
        shared = tokens if shared is None else shared & tokens
    return own - (shared or set()) if others else own


def _question_names_role(question: str, table: str, column: str, others: Sequence[Tuple[str, str]]) -> bool:
    q_tokens = {normalize(w) for w in _WORD_RE.findall(str(question or ""))}
    q_tokens |= {t[:-1] for t in q_tokens if t.endswith("s") and len(t) > 3}
    return bool(_role_tokens(table, column, others) & q_tokens)


# ── Structured clarifications ─────────────────────────────────────────────────


def _label(table: str, column: str, descriptions: Dict[Tuple[str, str], str]) -> str:
    description = descriptions.get((table.lower(), column.lower()))
    if description:
        return description.split(".")[0][:60]
    words = [w for w in _identifier_tokens(table) if w not in _GENERIC_ROLE_TOKENS]
    col_words = [w for w in _identifier_tokens(column) if w not in {"id", "key"}]
    words = [w for w in words if w not in col_words]
    return " ".join(words + col_words).strip().capitalize() or f"{table}.{column}"


def _column_clarification(
    literal: str,
    options: Sequence[Dict[str, Any]],
    *,
    show_values: bool,
) -> Dict[str, Any]:
    shown = list(options)[:_MAX_OPTIONS]
    names = ", ".join(o["label"] for o in shown)
    display = bidi_isolate(literal)
    message = (
        f'"{display}" could refer to more than one field ({names}). Which one should I filter on?'
    )
    return {
        "kind": "column",
        "literal": literal,
        "message": message,
        "options": [
            {
                "id": f'{o["table"]}.{o["column"]}',
                "table": o["table"],
                "column": o["column"],
                "label": o["label"],
                "value": o.get("value") if show_values else None,
                "description": o.get("description"),
            }
            for o in shown
        ],
        "allow_any": len(shown) > 1,
        "allow_other": True,
    }


def _value_clarification(
    literal: str,
    table: str,
    column: str,
    candidates: Sequence[str],
    *,
    label: str,
    reason: str = "",
    show_values: bool,
    not_found: bool = False,
) -> Dict[str, Any]:
    display = bidi_isolate(literal)
    shown = [str(c) for c in candidates][:_MAX_OPTIONS] if show_values else []
    if shown and not_found:
        quoted = ", ".join(f"'{bidi_isolate(c)}'" for c in shown)
        message = f'I could not find "{display}" in {label}. Did you mean {quoted}?'
    elif shown:
        quoted = ", ".join(f"'{bidi_isolate(c)}'" for c in shown)
        message = f'I found several possible values for "{display}" in {label}: {quoted}. Which did you mean?'
    elif reason and reason.startswith("I couldn't verify"):
        message = (f'{reason} in {label} against the data. Please give the exact value as it '
                   'appears there, or tell me to use it as written.')
    elif reason:
        hint = ('Please write dates as YYYY-MM-DD or as a month like "Jul 2008".'
                if "date" in reason.lower() else "Please give the exact value.")
        message = f'{reason.rstrip(".")} ("{display}" for {label}). {hint}'
    else:
        message = f'I could not find "{display}" in {label}. Please give the exact value.'
    return {
        "kind": "value",
        "literal": literal,
        "message": message,
        "table": table,
        "column": column,
        "options": [
            {"id": f"{table}.{column}={c}", "table": table, "column": column, "label": c, "value": c}
            for c in shown
        ],
        "allow_any": False,
        "allow_other": True,
    }


def _clarification(items: Sequence[Dict[str, Any]]) -> str:
    """Prose fallback for older callers/tests: join the messages of ambiguities."""
    messages: List[str] = []
    for item in items:
        if item.get("message"):
            messages.append(str(item["message"]))
            continue
        candidates = item.get("candidates") or []
        raw = item.get("value")
        value = " to ".join(str(v) for v in raw) if isinstance(raw, (list, tuple)) else str(raw or "")
        column = str(item.get("column") or "that field")
        reason = str(item.get("reason") or "")
        if candidates:
            quoted = ", ".join(f"'{candidate}'" for candidate in candidates)
            messages.append(f'I found several possible values for "{value}" in {column}: {quoted}. Which did you mean?')
        elif reason:
            hint = ('Please write dates as YYYY-MM-DD or as a month like "Jul 2008".'
                    if "date" in reason.lower() else "Please give the exact value.")
            messages.append(f'{reason.rstrip(".")} ("{value}" for {column}). {hint}')
        else:
            messages.append(f'I could not verify "{value}" in {column}. Please provide the exact value.')
    return " ".join(messages)


# ── Planner ───────────────────────────────────────────────────────────────────


StoreProvider = Callable[[AgentState], ValueStore]


def _default_store_provider(state: AgentState) -> ValueStore:
    return NullValueStore()


def _metadata_evidence_allowed(state: AgentState) -> bool:
    return bool(state.get("filter_metadata_evidence_enabled", True)) and (
        str(state.get("filter_value_visibility") or "source_wide") != "none"
    )


def _candidate_section(hits: Sequence[ColumnHit], *, show_values: bool) -> str:
    """Candidate columns for the planner prompt.

    Values are database content: they are shown only when the source's
    visibility policy lets every user see them, and always fenced as data.
    """
    if not hits:
        return ""
    lines = []
    for hit in hits[:12]:
        kind = f" ({hit.semantic_type})" if hit.semantic_type else ""
        what = f"contains '{hit.value}'" if show_values else "contains a matching value"
        lines.append(f"- {hit.table}.{hit.column}{kind} {what} "
                     f"(similar to a word in the question, similarity {hit.similarity:.2f})")
    return "<<<BEGIN_UNTRUSTED_DATA>>>\n" + "\n".join(lines) + "\n<<<END_UNTRUSTED_DATA>>>"


async def _displayable_hits(
    state: AgentState, store: ValueStore, hits: Sequence[ColumnHit], governed: Set[str]
) -> List[ColumnHit]:
    """Drop hits on columns that must never be offered: governed names, the
    denylist, non-text catalog types, and (per the latest profile) sensitive or
    hidden columns. The store already filters in SQL; this re-checks with the
    request's own policy and the catalog the planner is about to see."""
    denylist = _denylist(state)
    table_columns = state.get("table_columns") or {}
    types = column_types((state.get("metadata_bundle") or {}).get("columns", ""))
    kept: List[ColumnHit] = []
    checked: Dict[Tuple[str, str], bool] = {}
    for hit in hits:
        key = (hit.table.lower(), hit.column.lower())
        if key not in checked:
            ok = key[0] in table_columns and key[1] in table_columns.get(key[0], [])
            ok = ok and not denied_by_name(key[0], key[1], governed, denylist)
            declared = types.get(key)
            ok = ok and (declared is None or _kind_for_type(declared) == "text")
            if ok and store.available:
                try:
                    profile = await asyncio.wait_for(
                        store.column_profile(str(state.get("source_key") or ""), key[0], key[1]), timeout=2.0
                    )
                except Exception:  # noqa: BLE001
                    profile = None
                if profile is not None and (profile.is_sensitive or profile.is_hidden or not profile.is_text):
                    ok = False
            checked[key] = ok
        if checked[key]:
            kept.append(hit)
    return kept


def make_filter_planner(
    llm: LangChainLlmService,
    prompt_loader: PromptLoader,
    *,
    value_store_provider: Optional[StoreProvider] = None,
    match_threshold: float = 78.0,
    governed_columns: Optional[Sequence[str]] = None,
):
    """Create the small-model planner that binds filter intent to catalog columns.

    A reverse lookup of the question's literals over captured column values
    runs first (bounded, metadata only). Its hits are shown to the planner as
    candidate columns, shrink the catalog it reads on large schemas, and are
    attached to each planned filter so the grounder can decide the column
    deterministically. A hit also runs the planner when the question has no
    English predicate cue ("Moscow sales", Hebrew questions).
    """
    store_for = value_store_provider or _default_store_provider
    governed = _governed_set(governed_columns)

    async def filter_planner(state: AgentState) -> Dict[str, Any]:
        bundle = state.get("metadata_bundle") or {}
        question = str(state.get("question") or "")
        table_columns = state.get("table_columns") or {}
        threshold = float(state.get("filter_match_threshold") or match_threshold)
        show_values = str(state.get("filter_value_visibility") or "source_wide") == "source_wide"

        literals = extract_candidate_literals(question, table_columns)
        store = store_for(state)
        hits: List[ColumnHit] = []
        if literals and store.available and _metadata_evidence_allowed(state):
            raw_hits = await _safe_reverse_lookup(store, str(state.get("source_key") or ""), literals)
            hits = await _displayable_hits(
                state, store, rank_hits(literals, raw_hits, threshold=threshold), governed
            )

        intent = _has_filter_intent(question) or any(
            h.similarity >= _REVERSE_TRIGGER_SIMILARITY for h in hits
        )
        if not intent:
            return {
                "filter_plan": {"filters": [], "invalid_filters": []},
                "filter_candidates": [_hit_dict(h) for h in hits],
                "filter_clarification_required": False,
                "filter_resolution_attempts": 0,
            }

        prompt_bundle = bundle
        try:
            from src.config import settings as _settings  # noqa: PLC0415
            from src.metadata import link_bundle  # noqa: PLC0415

            if _settings.SCHEMA_LINK_ENABLED and question:
                hint_text = question
                if hits:
                    hint_text += " " + " ".join(f"{h.table} {h.column}" for h in hits[:12])
                prompt_bundle, _ = link_bundle(
                    bundle, hint_text,
                    min_columns=_settings.SCHEMA_LINK_MIN_COLUMNS,
                    max_tables=_settings.SCHEMA_LINK_MAX_TABLES,
                    max_columns=_settings.SCHEMA_LINK_MAX_COLUMNS,
                    max_columns_per_table=_settings.SCHEMA_LINK_MAX_COLUMNS_PER_TABLE,
                )
        except Exception:  # noqa: BLE001 - pruning is an optimisation only
            prompt_bundle = bundle

        candidate_text = _candidate_section(hits, show_values=show_values) if hits else ""
        prompt = await prompt_loader.arender(
            "sql_filter_planner",
            question=question,
            columns=prompt_bundle.get("columns", ""),
            column_statistics=prompt_bundle.get("column_statistics", ""),
            column_samples=prompt_bundle.get("column_samples", ""),
            business_terms=prompt_bundle.get("business_terms", ""),
            candidate_columns=candidate_text or "None found.",
        )
        model_override = await prompt_loader.model_override_for("sql_filter_planner")
        t0 = time.monotonic()
        try:
            response = await llm.generate(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": question},
                ],
                temperature=0.0,
                max_tokens=700,
                model_override=model_override,
                timeout=state.get("llm_timeout_seconds"),
            )
        except Exception as exc:  # noqa: BLE001 - planning is best effort; SQL generation still runs
            logger.warning("filter_planner: LLM call failed (%s); continuing without a filter plan", type(exc).__name__)
            response = {"content": "", "usage": {}}
        latency_ms = int((time.monotonic() - t0) * 1000)
        content = response.get("content") or ""
        try:
            parsed = json.loads(_extract_json(content))
            raw_filters = parsed.get("filters") if isinstance(parsed, dict) else []
            filters = raw_filters if isinstance(raw_filters, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            filters = []

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
            item["candidate_columns"] = [
                _hit_dict(h) for h in _hits_for_literal(item.get("value"), hits, threshold)
            ]
            valid.append(item)

        clarification = ""
        if invalid and not valid:
            clarification = "I need clarification about which database field to use for the requested filter."
        usage = response.get("usage") or {}
        return {
            "filter_plan": {"filters": valid, "invalid_filters": invalid},
            "filter_candidates": [_hit_dict(h) for h in hits],
            "filter_clarification_required": bool(clarification),
            "clarification": clarification or None,
            "answer": clarification or None,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "filter_planner": prompt},
        }

    return filter_planner


async def _safe_reverse_lookup(store: ValueStore, source: str, literals: Sequence[str]) -> List[ColumnHit]:
    try:
        return await asyncio.wait_for(store.find_columns_for_value(source, literals, limit=12), timeout=2.5)
    except Exception as exc:  # noqa: BLE001 - reverse lookup is a hint, never a blocker
        logger.info("filter_planner: reverse lookup unavailable (%s)", type(exc).__name__)
        return []


def _hit_dict(hit: ColumnHit) -> Dict[str, Any]:
    return {
        "table": hit.table, "column": hit.column, "value": hit.value,
        "similarity": hit.similarity, "count": hit.count,
        "semantic_type": hit.semantic_type, "source": hit.source,
    }


def _hits_for_literal(value: Any, hits: Sequence[ColumnHit], threshold: float) -> List[ColumnHit]:
    needles = value if isinstance(value, list) else [value]
    needles = [str(n) for n in needles if isinstance(n, str) and tokenize(n)]
    if not needles:
        return []
    out: List[ColumnHit] = []
    for hit in hits:
        if any(_best_score(n, hit.value) >= threshold for n in needles):
            out.append(hit)
    return out


# ── Column decision ───────────────────────────────────────────────────────────


@dataclass
class ColumnDecision:
    table: str
    column: str
    action: str                     # resolve | disclose | ask
    assumption: Optional[str] = None
    options: List[Dict[str, Any]] = field(default_factory=list)
    matched_value: Optional[str] = None


def decide_column(
    *,
    planner_table: str,
    planner_column: str,
    literal: str,
    hits: Sequence[Dict[str, Any]],
    question: str,
    eligible: Callable[[str, str], bool],
    reachable: Optional[Set[str]],
    remembered: Optional[Tuple[str, str]],
    descriptions: Dict[Tuple[str, str], str],
) -> ColumnDecision:
    """Pick the column a literal filters, or decide to ask.

    ``hits`` are reverse-lookup hits for this literal; ``eligible`` says whether
    a column may be filtered/shown at all; ``reachable`` is the set of tables
    joinable from the question's tables (``None`` = unknown, do not filter).
    """
    planner = (planner_table.lower(), planner_column.lower())
    if remembered:
        return ColumnDecision(remembered[0], remembered[1], "resolve")

    by_column: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for hit in hits:
        key = (str(hit.get("table", "")).lower(), str(hit.get("column", "")).lower())
        if not key[0] or not key[1] or not eligible(*key):
            continue
        if reachable is not None and key[0] not in reachable and key != planner:
            continue
        current = by_column.get(key)
        if current is None or float(hit.get("similarity") or 0) > float(current.get("similarity") or 0):
            by_column[key] = hit
    candidates = list(by_column.keys())

    if not candidates or candidates == [planner]:
        return ColumnDecision(planner[0], planner[1], "resolve",
                              matched_value=(by_column.get(planner) or {}).get("value"))

    others = [c for c in candidates if c != planner]
    planner_hit = planner in by_column
    if planner_hit and _question_names_role(question, planner[0], planner[1], others):
        return ColumnDecision(planner[0], planner[1], "resolve", matched_value=by_column[planner].get("value"))

    if not planner_hit and len(candidates) == 1:
        table, column = candidates[0]
        value = by_column[candidates[0]].get("value")
        display = bidi_isolate(literal)
        assumption = (
            f"'{display}' was not found in {planner_column}; it matched "
            f"{_label(table, column, descriptions)} = '{bidi_isolate(str(value))}' instead."
        )
        return ColumnDecision(table, column, "disclose", assumption=assumption, matched_value=value)

    options = []
    ordered = ([planner] if planner_hit else []) + others
    for table, column in ordered[:_MAX_OPTIONS]:
        hit = by_column[(table, column)]
        options.append({
            "table": table, "column": column,
            "label": _label(table, column, descriptions),
            "value": hit.get("value"),
            "description": descriptions.get((table, column)),
        })
    return ColumnDecision(planner[0], planner[1], "ask", options=options)


# ── Grounder ──────────────────────────────────────────────────────────────────


@dataclass
class _Ctx:
    state: AgentState
    store: ValueStore
    probe: SqlValueProbe
    contract: CaptureContract
    source_key: str
    visibility: str
    cache_identity: str
    threshold: float
    max_domain_values: int
    timeout_ms: int
    cache_ttl: int
    metadata_enabled: bool
    probes_enabled: bool
    distinct_enabled: bool
    unverified_mode: str
    escalate: bool
    governed: Set[str]
    denylist: Tuple[str, ...]
    descriptions: Dict[Tuple[str, str], str]
    types: Dict[Tuple[str, str], str]
    table_columns: Dict[str, List[str]]
    probe_budget: List[int]
    probe_gate: asyncio.Semaphore
    profiles: Dict[Tuple[str, str], Optional[ColumnProfile]] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=lambda: {"tiers": {}, "source_probes": 0, "metadata_reads": 0})

    @property
    def show_values(self) -> bool:
        return self.visibility == "source_wide"

    def take_probe(self) -> bool:
        if not self.probes_enabled or not self.probe.can_probe:
            return False
        if self.probe_budget[0] >= _MAX_SOURCE_PROBES:
            return False
        self.probe_budget[0] += 1
        self.metrics["source_probes"] += 1
        return True

    def tier(self, name: str) -> None:
        self.metrics["tiers"][name] = self.metrics["tiers"].get(name, 0) + 1


@dataclass
class ValueOutcome:
    kind: str                       # resolved | ask | unverified
    values: List[str] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    reason: str = ""
    tier: str = ""
    evidence: str = ""             # metadata | source | cache


async def _profile(ctx: _Ctx, table: str, column: str) -> Optional[ColumnProfile]:
    key = (table.lower(), column.lower())
    if key in ctx.profiles:
        return ctx.profiles[key]
    profile = None
    if ctx.metadata_enabled and ctx.store.available:
        try:
            profile = await asyncio.wait_for(
                ctx.store.column_profile(ctx.source_key, table, column), timeout=ctx.timeout_ms / 1000
            )
            ctx.metrics["metadata_reads"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.info("filter_grounder: profile unavailable for %s.%s (%s)", table, column, type(exc).__name__)
    ctx.profiles[key] = profile
    return profile


def _is_denied(ctx: _Ctx, table: str, column: str) -> bool:
    return denied_by_name(table, column, ctx.governed, ctx.denylist)


async def _eligible(ctx: _Ctx, table: str, column: str) -> bool:
    """A column whose values may be looked up and shown to the user."""
    if _is_denied(ctx, table, column):
        return False
    declared = ctx.types.get((table.lower(), column.lower()))
    if declared is not None and _kind_for_type(declared) != "text":
        return False
    profile = await _profile(ctx, table, column)
    if profile is not None:
        if profile.is_sensitive or profile.is_hidden:
            return False
        if not profile.is_text and declared is None:
            return False
    return True


async def _domain_evidence(ctx: _Ctx, table: str, column: str, needle: str) -> Optional[DomainEvidence]:
    if not ctx.metadata_enabled or not ctx.store.available:
        return None
    try:
        evidence = await asyncio.wait_for(
            ctx.store.domain(ctx.source_key, table, column, needle=needle, limit=ctx.max_domain_values),
            timeout=ctx.timeout_ms / 1000,
        )
        ctx.metrics["metadata_reads"] += 1
        return evidence
    except Exception as exc:  # noqa: BLE001
        logger.info("filter_grounder: metadata domain unavailable for %s.%s (%s)", table, column, type(exc).__name__)
        return None


async def _confirm(ctx: _Ctx, table: str, column: str, value: str) -> Optional[bool]:
    """T2 point probe under the concurrency gate and per-request budget."""
    if not ctx.take_probe():
        return None
    ctx.tier("T2")
    async with ctx.probe_gate:
        try:
            return await asyncio.wait_for(
                ctx.probe.exists(table, column, value, timeout_ms=ctx.timeout_ms),
                timeout=ctx.timeout_ms / 1000 + 1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("filter_grounder: point probe failed for %s.%s (%s)", table, column, type(exc).__name__)
            return None


async def _source_domain(ctx: _Ctx, table: str, column: str, needle: str, profile: Optional[ColumnProfile]) -> Optional[Dict[str, Any]]:
    """T3 when the profile proves the column small, else T4; ``None`` when not allowed."""
    if not ctx.probes_enabled or not ctx.probe.can_probe:
        return None
    small = (
        profile is not None
        and profile.status in (None, "success")
        and profile.distinct_count is not None
        and not profile.distinct_is_approximate
        and profile.distinct_count <= ctx.max_domain_values
        and (profile.row_count is None or (not profile.row_count_is_estimate and profile.row_count <= _MAX_DISTINCT_ROWS_FOR_SOURCE_DISTINCT))
    )
    if not ctx.take_probe():
        return None
    async with ctx.probe_gate:
        try:
            if small and ctx.distinct_enabled:
                ctx.tier("T3")
                return await asyncio.wait_for(
                    ctx.probe.distinct_values(table, column, limit=ctx.max_domain_values, timeout_ms=ctx.timeout_ms),
                    timeout=ctx.timeout_ms / 1000 + 1,
                )
            ctx.tier("T4")
            return await asyncio.wait_for(
                ctx.probe.contains_values(table, column, needle, limit=ctx.max_domain_values, timeout_ms=ctx.timeout_ms),
                timeout=ctx.timeout_ms / 1000 + 1,
            )
        except asyncio.TimeoutError:
            return {"values": [], "complete": False, "source": "timeout", "error": "timeout"}
        except Exception as exc:  # noqa: BLE001
            logger.info("filter_grounder: source probe failed for %s.%s (%s)", table, column, type(exc).__name__)
            return {"values": [], "complete": False, "source": "db", "error": str(exc)}


async def resolve_value(ctx: _Ctx, table: str, column: str, needle: str) -> ValueOutcome:
    """Ground one literal in one column through the evidence tiers."""
    profile = await _profile(ctx, table, column)
    domain: Optional[ValueDomain] = None
    evidence_source = ""

    # T0 — cache, keyed by visibility, normalisation version and the profile
    # snapshot so a re-profiled column never serves a domain cached from the
    # previous run (and a normaliser change never reuses old token forms).
    cache_key = value_domain_cache.key(
        ctx.source_key, ctx.cache_identity, table, column,
        f"{ctx.visibility}:{_NORMALIZATION_VERSION}",
        snapshot=profile.snapshot if profile is not None else "",
    )
    cached = None if ctx.escalate else value_domain_cache.get(cache_key, max_age_seconds=ctx.cache_ttl)
    if cached is not None:
        ctx.tier("T0")
        domain, evidence_source = cached, "cache"

    # T1 — metadata store
    evidence: Optional[DomainEvidence] = None
    if domain is None:
        evidence = await _domain_evidence(ctx, table, column, needle)
        if evidence is not None and evidence.values:
            ctx.tier("T1")
            domain, evidence_source = evidence.as_domain(), "metadata"
            if domain.complete:
                value_domain_cache.put(cache_key, domain)

    if domain is not None and domain.values:
        kind, candidates, is_exact = _classify(needle, domain.values, ctx.threshold)
        trusted = domain.complete or evidence_source == "cache"
        if ctx.escalate and kind in ("single", "refinement") and evidence_source in ("metadata", "cache"):
            # Zero-row diagnostic: the snapshot said the value existed, the
            # source returned nothing. Check whether it is still there before
            # trusting the snapshot a second time.
            confirmed = await _confirm(ctx, table, column, candidates[0])
            if confirmed is False:
                nearest = [m.value for m in match_values(needle, domain.values, limit=_MAX_SUGGESTIONS, threshold=_SUGGESTION_THRESHOLD)
                           if m.value != candidates[0]]
                return ValueOutcome("ask", candidates=nearest, reason="not found", tier="T2")
            if confirmed:
                return ValueOutcome("resolved", candidates, tier="T2", evidence="source")
        if ctx.visibility == "user_scoped":
            # Captured values were read under the capture identity, not the
            # asking user's: a hit is a candidate to confirm, never evidence.
            if kind == "single":
                confirmed = await _confirm(ctx, table, column, candidates[0])
                if confirmed:
                    return ValueOutcome("resolved", candidates, tier="T2", evidence="source")
                return ValueOutcome("unverified", reason="not confirmed for this user", tier="T2")
        elif is_exact:
            return ValueOutcome("resolved", candidates, tier="T1" if evidence_source == "metadata" else "T0", evidence=evidence_source)
        elif kind == "single" and trusted:
            return ValueOutcome("resolved", candidates, tier="T1", evidence=evidence_source)
        elif kind == "refinement" and trusted:
            return ValueOutcome("resolved", candidates, tier="T1", evidence=evidence_source)
        elif kind == "ambiguous":
            return ValueOutcome("ask", candidates=candidates, reason="ambiguous", tier="T1")
        elif kind in ("single", "refinement"):
            # Partial or stale snapshot: confirm the strongest candidate once.
            best = candidates[0]
            if kind == "single" and _best_score(needle, best) >= _STRONG_CANDIDATE_SCORE:
                confirmed = await _confirm(ctx, table, column, best)
                if confirmed:
                    return ValueOutcome("resolved", [best], tier="T2", evidence="source")
                if confirmed is False:
                    return ValueOutcome("ask", candidates=candidates, reason="not found", tier="T2")
            return ValueOutcome("ask", candidates=candidates, reason="unconfirmed", tier="T1")
        elif kind == "none" and domain.fresh_for_absence and domain.complete:
            nearest = [m.value for m in match_values(needle, domain.values, limit=_MAX_SUGGESTIONS, threshold=_SUGGESTION_THRESHOLD)]
            return ValueOutcome("ask", candidates=nearest, reason="not found", tier="T1")

    # T3 / T4 — the source itself
    lookup = await _source_domain(ctx, table, column, needle, profile)
    if lookup is None:
        if domain is not None and domain.values:
            return ValueOutcome("unverified", reason="metadata inconclusive", tier="T1")
        return ValueOutcome("unverified", reason="no evidence available", tier="")
    if lookup.get("error"):
        return ValueOutcome("unverified", reason="probe error" if lookup.get("source") != "timeout" else "probe timeout", tier="T3")
    values = list(lookup.get("values") or [])
    complete = bool(lookup.get("complete"))
    if complete and values:
        value_domain_cache.put(cache_key, ValueDomain(tuple(values), complete=True, source="db"))
    kind, candidates, is_exact = _classify(needle, values, ctx.threshold)
    if kind == "single" and (is_exact or complete):
        return ValueOutcome("resolved", candidates, tier="T3" if complete else "T4", evidence="source")
    if kind == "refinement" and complete:
        return ValueOutcome("resolved", candidates, tier="T3", evidence="source")
    if kind in ("single", "refinement", "ambiguous"):
        return ValueOutcome("ask", candidates=candidates, reason="unconfirmed", tier="T4")
    if complete:
        nearest = [m.value for m in match_values(needle, values, limit=_MAX_SUGGESTIONS, threshold=_SUGGESTION_THRESHOLD)]
        return ValueOutcome("ask", candidates=nearest, reason="not found", tier="T3")
    return ValueOutcome("unverified", reason="not found in bounded search", tier="T4")


def _remembered_choice(state: AgentState, literal: str) -> Optional[Dict[str, Any]]:
    """The user's answer for this exact literal: this request's choices first,
    then what was remembered for them on this connection."""
    wanted = normalize(literal)
    if not wanted:
        return None
    for choice in list(state.get("filter_choices") or []) + list(state.get("filter_preferences") or []):
        if not isinstance(choice, dict):
            continue
        if normalize(choice.get("literal")) == wanted:
            return choice
    return None


def _role_preference(state: AgentState, candidates: Sequence[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """A remembered role-level answer that names one of the competing columns.

    "For this user, *city* means the dealer's city" (a role-level preference,
    stored with an empty literal) settles a new literal when the candidates
    include that column; it never introduces a column the evidence did not.
    """
    wanted = {(t.lower(), c.lower()) for t, c in candidates}
    for pref in state.get("filter_preferences") or []:
        if not isinstance(pref, dict) or normalize(pref.get("literal")) or pref.get("any"):
            continue
        target = (str(pref.get("table") or "").lower(), str(pref.get("column") or "").lower())
        if target in wanted:
            return target
    return None


async def _ground_one(ctx: _Ctx, item: Dict[str, Any]) -> Dict[str, Any]:
    """Ground one planned filter; returns an outcome dict merged by the caller."""
    state = ctx.state
    result: Dict[str, Any] = {"item": item, "resolved": None, "unresolved": None, "ambiguity": None,
                              "clarification": None, "assumption": None}
    target = _canonical_target(item)
    if not target:
        result["unresolved"] = {"target": item.get("target"), "reason": "unparseable target"}
        return result
    table, column = target
    item["table"], item["column"], item["target"] = table, column, f"{table}.{column}"

    if _is_denied(ctx, table, column):
        result["unresolved"] = {"target": item["target"], "value": item.get("value"), "reason": "governed column"}
        return result

    data_type = str(item.get("data_type") or "")
    item, error = normalize_typed_filter(item, data_type)
    result["item"] = item
    if error:
        if _kind_for_type(data_type) == "date" and _is_forecast_question(state):
            # A forecast's named period is the horizon to project, not a filter
            # on the history; leave it unresolved with the reason instead.
            result["unresolved"] = {"target": item["target"], "value": item.get("value"), "reason": error}
            return result
        result["ambiguity"] = {"column": column, "value": item.get("value"), "candidates": [], "reason": error}
        result["clarification"] = _value_clarification(
            str(item.get("value")), table, column, [], label=column, reason=error, show_values=ctx.show_values,
        )
        return result
    if _kind_for_type(data_type) != "text" or item.get("op") not in _RESOLVABLE_OPS:
        if item.get("resolved"):
            result["resolved"] = item
        return result

    raw_values = item.get("value")
    needles = raw_values if isinstance(raw_values, list) else [raw_values]
    if not all(isinstance(value, str) and tokenize(value) for value in needles):
        result["unresolved"] = {"target": item["target"], "value": raw_values, "reason": "non-text literal"}
        return result

    # Governance on the planner's own column (sensitive / hidden per profile).
    profile = await _profile(ctx, table, column)
    if profile is not None and (profile.is_sensitive or profile.is_hidden):
        result["unresolved"] = {"target": item["target"], "value": raw_values, "reason": "governed column"}
        return result

    # ── Column decision ────────────────────────────────────────────────────
    literal = needles[0] if len(needles) == 1 else " / ".join(needles)
    hits = list(item.get("candidate_columns") or [])
    reachable: Optional[Set[str]] = None
    if hits and ctx.store.available:
        try:
            edges = await ctx.store.relationships(ctx.source_key)
        except Exception:  # noqa: BLE001
            edges = []
        if edges:
            anchors = {table} | {
                str(f.get("table") or "").lower()
                for f in (state.get("filter_plan") or {}).get("filters", []) if isinstance(f, dict)
            }
            q_tokens = {normalize(w) for w in _WORD_RE.findall(str(state.get("question") or ""))}
            for known_table in ctx.table_columns:
                if _identifier_tokens(known_table) & q_tokens:
                    anchors.add(known_table)
            reachable = reachable_tables(anchors, edges)
    eligibility: Dict[Tuple[str, str], bool] = {}
    for hit in hits:
        key = (str(hit.get("table") or "").lower(), str(hit.get("column") or "").lower())
        if key[0] and key[1] and key not in eligibility:
            eligibility[key] = await _eligible(ctx, *key)
    # The columns the evidence actually offers for this literal: eligible hits
    # joinable from the question's tables. A remembered answer may pick among
    # these (or keep the planner's column); it can never introduce a column
    # the evidence did not offer.
    offered: Set[Tuple[str, str]] = {
        k for k, ok in eligibility.items() if ok and (reachable is None or k[0] in reachable)
    }
    planner_pair = (table, column)

    remembered = _remembered_choice(state, literal)
    remembered_target: Optional[Tuple[str, str]] = None
    if remembered and remembered.get("table") and remembered.get("column"):
        candidate = (str(remembered["table"]).lower(), str(remembered["column"]).lower())
        known = candidate[0] in ctx.table_columns and candidate[1] in ctx.table_columns.get(candidate[0], [])
        # Still subject to today's governance and today's evidence: a column
        # tagged sensitive since, or one no longer holding the value, is not used.
        if known and (candidate in offered or candidate == planner_pair) and await _eligible(ctx, *candidate):
            remembered_target = candidate
        else:
            remembered = None
    if remembered_target is None and not (remembered and remembered.get("any")) and len(offered) > 1:
        # No answer for this literal: a role-level preference ("city" means the
        # dealer's city for this user) settles competing candidates — unless
        # the question itself names one of the competing roles.
        names_a_role = any(
            _question_names_role(str(state.get("question") or ""), t, c, [o for o in offered if o != (t, c)])
            for t, c in offered
        )
        if not names_a_role:
            remembered_target = _role_preference(state, sorted(offered))
    if remembered and remembered.get("any"):
        chosen_columns = sorted(offered) or [planner_pair]
        decision = ColumnDecision(table, column, "resolve")
    else:
        decision = decide_column(
            planner_table=table, planner_column=column, literal=literal, hits=hits,
            question=str(state.get("question") or ""),
            eligible=lambda t, c: eligibility.get((t, c), False),
            reachable=reachable, remembered=remembered_target, descriptions=ctx.descriptions,
        )
        chosen_columns = [(decision.table, decision.column)]
    if decision.action == "ask":
        result["clarification"] = _column_clarification(literal, decision.options, show_values=ctx.show_values)
        result["ambiguity"] = {"column": column, "value": literal, "candidates": [o["label"] for o in decision.options],
                               "message": result["clarification"]["message"]}
        return result
    if decision.action == "disclose":
        result["assumption"] = decision.assumption

    # A value the user picked from an earlier question is the canonical needle:
    # they told us the exact spelling, so it is looked up as-is (and accepted
    # even when no evidence can be read). It is still request input: control
    # characters are dropped and the length capped before it becomes a literal.
    user_value: Optional[str] = None
    if remembered and remembered.get("value") is not None:
        cleaned = "".join(ch for ch in str(remembered["value"]) if ch >= " " or ch == "\t")[:_MAX_USER_VALUE_CHARS]
        user_value = cleaned.strip() or None
    if user_value:
        needles = [user_value]

    # ── Value grounding per needle (and per chosen column for "any of") ─────
    table, column = chosen_columns[0]
    canonical: List[str] = []
    tiers: List[str] = []
    evidence_kinds: List[str] = []
    grounded_columns: List[Tuple[str, str]] = []
    first_failure: Optional[Tuple[str, str, str, ValueOutcome]] = None
    for cand_table, cand_column in chosen_columns:
        column_values: List[str] = []
        failed: Optional[ValueOutcome] = None
        for needle in needles:
            outcome = await resolve_value(ctx, cand_table, cand_column, needle)
            tiers.append(outcome.tier)
            if outcome.kind == "resolved":
                column_values.extend(outcome.values)
                evidence_kinds.append(outcome.evidence)
            elif user_value and outcome.kind == "unverified":
                column_values.append(user_value)
                evidence_kinds.append("user")
            else:
                failed = outcome
                break
        if failed is None:
            grounded_columns.append((cand_table, cand_column))
            canonical.extend(column_values)
        elif first_failure is None:
            first_failure = (cand_table, cand_column, needle, failed)

    if not grounded_columns:
        table, column, needle, outcome = first_failure  # type: ignore[misc]
        label = _label(table, column, ctx.descriptions)
        if outcome.kind == "ask":
            result["clarification"] = _value_clarification(
                needle, table, column, outcome.candidates, label=label, show_values=ctx.show_values,
                not_found=outcome.reason == "not found",
            )
            result["ambiguity"] = {"column": column, "value": needle,
                                   "candidates": outcome.candidates if ctx.show_values else [],
                                   "message": result["clarification"]["message"]}
            return result
        # Unverified. Transient probe failures fail open (disclosed); anything
        # the evidence could not settle is put to the user unless the source's
        # policy explicitly allows unverified literals to run.
        transient = outcome.reason in ("probe error", "probe timeout")
        result["unresolved"] = {"target": f"{table}.{column}", "value": needle, "reason": outcome.reason, "tier": outcome.tier}
        if ctx.unverified_mode == "ask" and not transient:
            result["clarification"] = _value_clarification(
                needle, table, column, outcome.candidates, label=label, show_values=ctx.show_values,
                reason=f'I couldn\'t verify "{bidi_isolate(needle)}"' if not outcome.candidates else "",
            )
            result["ambiguity"] = {"column": column, "value": needle, "candidates": [], "message": result["clarification"]["message"]}
        return result

    table, column = grounded_columns[0]
    item["table"], item["column"], item["target"] = table, column, f"{table}.{column}"
    if len(grounded_columns) > 1:
        item["any_of_columns"] = [f"{t}.{c}" for t, c in grounded_columns]

    deduped = list(dict.fromkeys(canonical))
    item["value"] = deduped[0] if len(deduped) == 1 else deduped
    if item.get("op") == "contains":
        # An explicit substring request keeps its semantics; several matching
        # values can only be expressed as a set.
        item["op"] = "contains" if len(deduped) == 1 else "in"
    else:
        item["op"] = "equals" if len(deduped) == 1 else "in"
    item["resolved"] = True
    item["evidence"] = evidence_kinds[0] if evidence_kinds else ""
    item["tier"] = tiers[0] if tiers else ""
    originals = raw_values if isinstance(raw_values, list) else [raw_values]
    if len(deduped) == len(originals) and any(normalize(v) != normalize(n) for v, n in zip(deduped, originals)):
        corrections = [f"'{bidi_isolate(n)}' → '{bidi_isolate(v)}'" for v, n in zip(deduped, originals) if normalize(v) != normalize(n)]
        if corrections and not result["assumption"]:
            result["assumption"] = f"Matched {', '.join(corrections)} in {_label(table, column, ctx.descriptions)}."
    result["resolved"] = item
    return result


def make_filter_grounder(
    sql_runner: SqlRunner,
    *,
    enabled: bool = True,
    max_domain_values: int = _MAX_DOMAIN_VALUES,
    match_threshold: float = 78.0,
    lookup_timeout_ms: int = 5000,
    cache_ttl_seconds: int = 900,
    governed_columns: Optional[Sequence[str]] = None,
    value_store_provider: Optional[StoreProvider] = None,
):
    """Ground text filters and normalize typed ranges before SQL generation."""
    probe = SqlValueProbe(sql_runner, sql_runner.database_type)
    store_for = value_store_provider or _default_store_provider
    governed = _governed_set(governed_columns)

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
                "filter_clarification": None,
                "needs_filter_reground": False,
            }

        bundle = state.get("metadata_bundle") or {}
        visibility = str(state.get("filter_value_visibility") or "source_wide")
        user_id = str(state.get("user_id") or "")
        existence_hours = int(state.get("filter_existence_max_age_hours") or 168)
        absence_hours = int(state.get("filter_absence_max_age_hours") or 24)
        ctx = _Ctx(
            state=state,
            store=store_for(state),
            probe=probe,
            contract=CaptureContract(
                existence_max_age_seconds=existence_hours * 3600,
                absence_max_age_seconds=absence_hours * 3600,
            ),
            source_key=str(state.get("source_key") or ""),
            visibility=visibility,
            cache_identity=SHARED_VISIBILITY if visibility == "source_wide" else user_id,
            threshold=float(state.get("filter_match_threshold") or match_threshold),
            max_domain_values=int(state.get("filter_max_domain_values") or max_domain_values),
            timeout_ms=int(state.get("filter_lookup_timeout_ms") or lookup_timeout_ms),
            cache_ttl=int(state.get("filter_cache_ttl_seconds") or cache_ttl_seconds),
            metadata_enabled=_metadata_evidence_allowed(state),
            probes_enabled=bool(state.get("filter_source_probe_enabled", True)),
            distinct_enabled=bool(state.get("filter_source_distinct_enabled", True)),
            unverified_mode=str(state.get("filter_unverified_execution") or "ask"),
            escalate=bool(state.get("needs_filter_reground")),
            governed=governed,
            denylist=_denylist(state),
            descriptions=column_descriptions(bundle.get("columns", "")),
            types=column_types(bundle.get("columns", "")),
            table_columns=state.get("table_columns") or {},
            probe_budget=[0],
            probe_gate=asyncio.Semaphore(_MAX_SOURCE_PROBES),
        )
        # Store contract may carry its own freshness policy; align it.
        if hasattr(ctx.store, "contract"):
            try:
                ctx.store.contract = ctx.contract  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

        t0 = time.monotonic()
        # The whole stage is bounded: one metadata read plus at most one source
        # probe per filter, so twice the probe deadline is a generous ceiling.
        stage_cap = max(2.0, ctx.timeout_ms / 1000 * 2)

        async def bounded(raw: Dict[str, Any]) -> Dict[str, Any]:
            return await asyncio.wait_for(_ground_one(ctx, dict(raw)), timeout=stage_cap)

        outcomes = await asyncio.gather(*(bounded(raw) for raw in filters), return_exceptions=True)

        resolved: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        ambiguous: List[Dict[str, Any]] = []
        assumptions: List[str] = list(state.get("plan_assumptions") or [])
        clarification_payload: Optional[Dict[str, Any]] = None
        normalized_filters: List[Dict[str, Any]] = []
        for raw, outcome in zip(filters, outcomes):
            if isinstance(outcome, Exception):
                reason = "probe timeout" if isinstance(outcome, asyncio.TimeoutError) else "probe error"
                logger.warning("filter_grounder: grounding failed for %s (%s)", raw.get("target"), type(outcome).__name__)
                unresolved.append({"target": raw.get("target"), "value": raw.get("value"), "reason": reason})
                normalized_filters.append(dict(raw))
                continue
            item = outcome["item"]
            normalized_filters.append(item)
            if outcome.get("resolved") is not None:
                resolved.append(outcome["resolved"])
            if outcome.get("unresolved") is not None:
                unresolved.append(outcome["unresolved"])
            if outcome.get("ambiguity") is not None:
                ambiguous.append(outcome["ambiguity"])
            if outcome.get("clarification") is not None and clarification_payload is None:
                clarification_payload = outcome["clarification"]
            if outcome.get("assumption"):
                assumptions.append(outcome["assumption"])

        plan["filters"] = normalized_filters
        clarification = _clarification(ambiguous) if ambiguous else None
        ctx.metrics["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return {
            "filter_plan": plan,
            "resolved_filters": resolved,
            "unresolved_filters": unresolved,
            "filter_ambiguities": ambiguous,
            "filter_resolution_attempts": attempts,
            "filter_clarification_required": bool(clarification),
            "filter_clarification": clarification_payload,
            "clarification": clarification,
            "answer": clarification,
            "plan_assumptions": list(dict.fromkeys(assumptions)),
            "filter_metrics": ctx.metrics,
            "needs_filter_reground": False,
        }

    return filter_grounder


def empty_filter_result_check(state: AgentState) -> Dict[str, Any]:
    """Request one escalated grounding pass for an empty result.

    Triggers when a filter stayed unverified *or* was resolved from metadata
    alone (a snapshot cannot prove the value still exists), and only once; the
    grounder then bypasses its cache and confirms against the source.
    """
    result = state.get("query_result") or {}
    rows = result.get("rows") or []
    diagnostics = int(state.get("empty_filter_diagnostics") or 0)
    metadata_only = any(
        isinstance(f, dict) and f.get("evidence") in ("metadata", "cache")
        for f in (state.get("resolved_filters") or [])
    )
    retry = bool(not rows and (state.get("unresolved_filters") or metadata_only) and diagnostics < 1)
    return {
        "needs_filter_reground": retry,
        "empty_filter_diagnostics": diagnostics + 1 if retry else diagnostics,
    }


__all__ = [
    "ColumnDecision",
    "SqlValueProbe",
    "ValueOutcome",
    "column_descriptions",
    "column_types",
    "decide_column",
    "empty_filter_result_check",
    "extract_candidate_literals",
    "make_filter_grounder",
    "make_filter_planner",
    "normalize_typed_filter",
    "resolve_value",
]
