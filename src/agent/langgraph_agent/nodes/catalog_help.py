"""catalog_help_answer node - lists what the connected source can be asked about.

Answers questions like "what measures can I ask about?" / "which dimensions are
available?" deterministically from the metadata bundle (no LLM), grouping each
table's columns into measures, dimensions and dates. The answer is Markdown so
the UI renders it as formatted HTML (the ``catalog_help`` route is on the
frontend's ``MARKDOWN_ROUTES`` allowlist).

Governance: columns flagged by the DLP governed-columns setting, or matching the
built-in sensitive-name pattern, are never listed. The metadata loader already
drops hidden/deleted columns; this is the extra DLP filter on top.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)

# Curated-measure data_type markers (shared with the DAX catalog node).
_MEASURE_TYPES = {
    "measure", "dax_measure", "dax measure", "measure (dax)", "measure(dax)", "calculated measure",
}
_DATE_TYPE_HINTS = ("date", "datetime", "timestamp", "time", "smalldatetime", "datetimeoffset", "year")
_NUMERIC_TYPE_HINTS = (
    "int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "double",
    "real", "money", "number", "serial", "bit",
)
_KEY_SUFFIXES = ("key", "id", "guid", "uuid", "code", "sk", "pk")

# Built-in sensitive-name pattern (mirrors nodes/filtering._GOVERNED_NAME_RE), so
# a catalog listing never surfaces obviously sensitive columns even if the DLP
# setting is empty.
_SENSITIVE_RE = re.compile(
    r"(?:^|_)(?:password|passwd|ssn|social_security(?:_number)?|national_id|credit_card|"
    r"card_number|pin|secret|private_key|api_key|access_token|refresh_token)(?:$|_)",
    re.IGNORECASE,
)


def _norm(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or ""))
    return re.sub(r"[\s\-.]+", "_", spaced).lower()


def _governed_set(governed_columns: Optional[Sequence[str]]) -> set:
    # Normalise the same way as column names (camelCase -> snake_case) so
    # "EmailAddress" in the setting matches an EmailAddress column.
    return {
        _norm(part)
        for item in (governed_columns or [])
        for part in str(item).replace(",", " ").split()
        if part.strip()
    }


def _md_code(name: str) -> str:
    """Wrap an identifier in an inline-code span, dropping backticks so it cannot
    break out. Markdown-active characters inside a code span are shown verbatim."""
    return "`" + str(name).replace("`", "") + "`"


def _extract_type(line: str) -> str:
    m = re.search(r"Type:\s*([^,]+)", line, re.IGNORECASE)
    return (m.group(1).strip().lower() if m else "")


def _classify(column: str, dtype: str, is_pk: bool) -> str:
    """One of: measure | date | numeric | dimension."""
    if dtype in _MEASURE_TYPES:
        return "measure"
    if any(h in dtype for h in _DATE_TYPE_HINTS):
        return "date"
    name = column.lower()
    looks_like_key = is_pk or name.endswith(_KEY_SUFFIXES)
    if not looks_like_key and any(h in dtype for h in _NUMERIC_TYPE_HINTS):
        return "numeric"
    return "dimension"


def _parse_bundle_columns(
    columns_text: str, governed: set,
) -> "Dict[str, Dict[str, List[str]]]":
    """``{table: {category: [columns]}}`` from the bundle's columns block,
    excluding governed/sensitive columns."""
    tables: "Dict[str, Dict[str, List[str]]]" = {}
    for raw in (columns_text or "").splitlines():
        stripped = raw.lstrip("- ").strip()
        if not stripped:
            continue
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        table, _, column = qualified.partition(".")
        table, column = table.strip(), column.strip()
        if not table or not column:
            continue
        if _norm(column) in governed or _SENSITIVE_RE.search(column):
            continue
        dtype = _extract_type(stripped)
        is_pk = bool(re.search(r",\s*PK:\s*true", stripped, re.IGNORECASE))
        category = _classify(column, dtype, is_pk)
        tables.setdefault(table, {"measure": [], "numeric": [], "date": [], "dimension": []})
        bucket = tables[table][category]
        if column not in bucket:
            bucket.append(column)
    return tables


def _example_questions(knowledge_pairs_text: str, limit: int = 4) -> List[str]:
    """Best-effort: pull a few example questions from the knowledge-pairs block."""
    out: List[str] = []
    for raw in (knowledge_pairs_text or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        # Common shapes: "Q: ...?", "question: ...?", or a plain line ending in ?
        m = re.match(r"(?:q|question)\s*[:\-]\s*(.+)", line, re.IGNORECASE)
        candidate = (m.group(1) if m else line).strip()
        if candidate.endswith("?") and 6 <= len(candidate) <= 160:
            if candidate not in out:
                out.append(candidate)
        if len(out) >= limit:
            break
    return out


def _render(display: str, tables: "Dict[str, Dict[str, List[str]]]", examples: List[str]) -> str:
    _LABELS: List[Tuple[str, str]] = [
        ("measure", "Measures"),
        ("numeric", "Numeric fields"),
        ("dimension", "Dimensions"),
        ("date", "Dates"),
    ]
    lines: List[str] = [f"Here's what you can ask about in **{display}**, grouped by table:", ""]
    for table in sorted(tables):
        cats = tables[table]
        if not any(cats.values()):
            continue
        lines.append(f"### {table}")
        for key, label in _LABELS:
            cols = cats.get(key) or []
            if cols:
                shown = ", ".join(_md_code(c) for c in cols)
                lines.append(f"- **{label}:** {shown}")
        lines.append("")
    if examples:
        lines.append("### Example questions")
        for q in examples:
            lines.append(f"- {q}")
    return "\n".join(lines).strip()


def make_catalog_help_answer(*, governed_columns: Optional[Sequence[str]] = None):
    """Return a node that answers "what can I ask about?" from the catalog.

    ``governed_columns`` is the deployment's DLP governed-columns list; those
    columns (and built-in sensitive names) are never listed.
    """
    governed = _governed_set(governed_columns)

    async def catalog_help_answer(state: AgentState) -> Dict[str, Any]:
        bundle = state.get("metadata_bundle") or {}
        display = state.get("connection_display_name") or state.get("source_key") or "this data source"
        tables = _parse_bundle_columns(bundle.get("columns", ""), governed)
        examples = _example_questions(bundle.get("knowledge_pairs", ""))

        if not tables:
            answer = (
                f"I don't have any registered columns for **{display}** yet, so I can't list what "
                "you can ask about. Ask an admin to register the tables and columns in Settings."
            )
        else:
            answer = _render(display, tables, examples)

        logger.info("catalog_help_answer: listed %d table(s) for %s", len(tables), display)
        return {"answer": answer}

    return catalog_help_answer
