"""Scope-aware DAX lexer + read-only safety gate (no external parser).

DAX has no ``sqlglot`` equivalent, so this module provides the pragmatic, single
pass lexer the text-to-DAX pipeline relies on for *structural* safety and symbol
extraction. It is intentionally dependency-free so both the execution client
(:mod:`src.connectors.powerbi`) and the graph validator
(``langgraph_agent_dax.nodes.dax_validate``) share one implementation and can
never drift.

What it does NOT do: understand DAX semantics. The Power BI engine remains the
authoritative compiler — a ``400`` from ``executeQueries`` drives the repair
loop. This module only guarantees the query is a single, well-formed, read-only
``EVALUATE``/``DEFINE`` statement with balanced delimiters and no MDX/DMV/DDL
smuggled in, and it surfaces the identifiers referenced so the validator can
resolve them against the catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# A "word" character for bare (unquoted) identifiers. Includes non-ASCII letters
# so Unicode table/column names are tokenised, not split.
_WORD_RE = re.compile(r"[^\W]", re.UNICODE)

# Structural keywords that only ever appear in MDX / SQL / DMV, never in a valid
# read-only DAX query. Names and string literals are blanked before this scan so
# a measure literally called ``[Select Total]`` can never trip it.
_BANNED_TOKEN_RE = re.compile(
    r"\b(SELECT|FROM|WHERE|DROP|ALTER|CREATE|DELETE|INSERT|UPDATE|MERGE|UPSERT|"
    r"GRANT|REVOKE|EXEC|EXECUTE|CALL|TRUNCATE|BACKUP|RESTORE|DISCOVER)\b",
    re.IGNORECASE,
)
# MDX/DMV constructs that regex word-boundaries alone would miss.
_BANNED_PHRASE_RE = re.compile(
    r"(\$SYSTEM|\bWITH\s+MEMBER\b|\bON\s+(COLUMNS|ROWS)\b|\.DISCOVER\b)",
    re.IGNORECASE,
)

_EVALUATE_RE = re.compile(r"\bEVALUATE\b", re.IGNORECASE)
_DEFINE_RE = re.compile(r"\bDEFINE\b", re.IGNORECASE)
_DEFINE_KIND_RE = re.compile(r"\b(MEASURE|VAR|COLUMN|TABLE)\b", re.IGNORECASE)


@dataclass
class DaxIdentifier:
    """A bracketed identifier reference found in the query.

    ``'Sales'[Amount]``  -> table='Sales', name='Amount', qualified=True
    ``[Total Sales]``    -> table=None,    name='Total Sales', qualified=False
    """

    table: Optional[str]
    name: str
    qualified: bool


@dataclass
class DaxLex:
    """Result of a single lexer pass."""

    # Comments removed; string literals, single-quoted table names and bracketed
    # identifiers blanked to spaces. Safe for keyword / banned-token scanning.
    tokens: str
    identifiers: List[DaxIdentifier] = field(default_factory=list)
    # Standalone table references (a ``'Table'`` NOT followed by ``[...]``).
    tables: List[str] = field(default_factory=list)
    balanced: bool = True
    balance_error: Optional[str] = None
    unterminated: Optional[str] = None

    @property
    def evaluate_count(self) -> int:
        return len(_EVALUATE_RE.findall(self.tokens))

    @property
    def has_define(self) -> bool:
        return bool(_DEFINE_RE.search(self.tokens))

    @property
    def define_kinds(self) -> List[str]:
        if not self.has_define:
            return []
        # Only the kinds appearing before the (single) EVALUATE belong to DEFINE.
        head = self.tokens
        m = _EVALUATE_RE.search(self.tokens)
        if m:
            head = self.tokens[: m.start()]
        return [k.upper() for k in _DEFINE_KIND_RE.findall(head)]


def lex_dax(text: str) -> DaxLex:
    """Tokenise *text* in a single pass.

    Tracks string literals (``"…"`` with ``""`` escape), single-quoted table
    names (``'…'`` with ``''`` escape), bracketed identifiers (``[…]`` with
    ``]]`` escape) and ``--``/``//``/``/* */`` comments so delimiter balancing
    and keyword scanning never see quoted content.
    """
    s = text or ""
    n = len(s)
    i = 0
    out: List[str] = []
    identifiers: List[DaxIdentifier] = []
    tables: List[str] = []
    stack: List[str] = []
    balance_error: Optional[str] = None
    unterminated: Optional[str] = None
    pending_table: Optional[str] = None  # qualifier captured just before a '['

    while i < n:
        c = s[i]

        # ── comments ──────────────────────────────────────────────────────
        if c == "-" and i + 1 < n and s[i + 1] == "-":
            i += 2
            while i < n and s[i] != "\n":
                i += 1
            out.append(" ")
            pending_table = None
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            i += 2
            while i < n and s[i] != "\n":
                i += 1
            out.append(" ")
            pending_table = None
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            closed = False
            while i < n:
                if s[i] == "*" and i + 1 < n and s[i + 1] == "/":
                    i += 2
                    closed = True
                    break
                i += 1
            if not closed:
                unterminated = unterminated or "Unterminated block comment"
            out.append(" ")
            pending_table = None
            continue

        # ── double-quoted string literal ─────────────────────────────────
        if c == '"':
            i += 1
            closed = False
            while i < n:
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                unterminated = unterminated or "Unterminated string literal"
            out.append(" ")
            pending_table = None
            continue

        # ── single-quoted table name ─────────────────────────────────────
        if c == "'":
            i += 1
            name_chars: List[str] = []
            closed = False
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        name_chars.append("'")
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                name_chars.append(s[i])
                i += 1
            if not closed:
                unterminated = unterminated or "Unterminated table name"
            out.append(" ")
            pending_table = "".join(name_chars).strip()
            # Standalone table reference unless a '[' follows (a qualifier).
            j = i
            while j < n and s[j] in " \t\r\n":
                j += 1
            if not (j < n and s[j] == "["):
                if pending_table:
                    tables.append(pending_table)
            continue

        # ── bracketed identifier ─────────────────────────────────────────
        if c == "[":
            i += 1
            id_chars: List[str] = []
            closed = False
            while i < n:
                if s[i] == "]":
                    if i + 1 < n and s[i + 1] == "]":
                        id_chars.append("]")
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                id_chars.append(s[i])
                i += 1
            if not closed:
                unterminated = unterminated or "Unterminated bracket identifier"
            out.append(" ")
            name = "".join(id_chars).strip()
            identifiers.append(
                DaxIdentifier(
                    table=pending_table, name=name, qualified=pending_table is not None
                )
            )
            pending_table = None
            continue

        # ── stray closing bracket (never opened; '[' is consumed above) ──
        if c == "]":
            balance_error = balance_error or "Unbalanced ']'"
            out.append(c)
            i += 1
            pending_table = None
            continue

        # ── grouping delimiters ──────────────────────────────────────────
        if c == "(":
            stack.append(")")
            out.append(c)
            i += 1
            pending_table = None
            continue
        if c == "{":
            stack.append("}")
            out.append(c)
            i += 1
            pending_table = None
            continue
        if c in ")}":
            if not stack:
                balance_error = balance_error or f"Unbalanced '{c}'"
            else:
                expected = stack.pop()
                if expected != c:
                    balance_error = balance_error or (
                        f"Mismatched delimiter: expected '{expected}', found '{c}'"
                    )
            out.append(c)
            i += 1
            pending_table = None
            continue

        # ── bare word (possible unquoted table qualifier) ────────────────
        if _WORD_RE.match(c):
            start = i
            while i < n and _WORD_RE.match(s[i]):
                i += 1
            word = s[start:i]
            out.append(word)
            # Only a qualifier when immediately followed by '[' (no space).
            pending_table = word if (i < n and s[i] == "[") else None
            continue

        # ── any other char ───────────────────────────────────────────────
        out.append(c)
        if c not in " \t\r\n":
            pending_table = None
        i += 1

    if stack:
        balance_error = balance_error or f"Unclosed delimiter(s): {''.join(reversed(stack))}"

    return DaxLex(
        tokens="".join(out),
        identifiers=identifiers,
        tables=tables,
        balanced=balance_error is None,
        balance_error=balance_error,
        unterminated=unterminated,
    )


def banned_token(lex: DaxLex) -> Optional[str]:
    """Return the first banned MDX/DMV/DDL token found, or ``None``."""
    m = _BANNED_TOKEN_RE.search(lex.tokens)
    if m:
        return m.group(1).upper()
    m = _BANNED_PHRASE_RE.search(lex.tokens)
    if m:
        return m.group(1).upper()
    return None


def is_read_only_dax(text: str) -> Tuple[bool, str]:
    """Return ``(ok, reason)`` for the hard read-only / shape gate.

    A read-only DAX query:
      * is terminated (no dangling string/comment/bracket),
      * has balanced ``()[]{}``,
      * contains exactly one top-level ``EVALUATE``,
      * begins with ``DEFINE`` or ``EVALUATE`` (after comments),
      * contains no MDX/DMV/DDL/mutation tokens.
    """
    if not text or not text.strip():
        return False, "Empty DAX query."
    lex = lex_dax(text)
    if lex.unterminated:
        return False, lex.unterminated
    if not lex.balanced:
        return False, lex.balance_error or "Unbalanced delimiters."

    stripped = lex.tokens.strip()
    head = stripped[:8].upper()
    if not (head.startswith("EVALUATE") or head.startswith("DEFINE")):
        return False, "Query must start with EVALUATE or DEFINE."

    count = lex.evaluate_count
    if count == 0:
        return False, "Query has no EVALUATE statement."
    if count > 1:
        return False, (
            "Query has more than one EVALUATE. The executeQueries endpoint "
            "returns only a single result table."
        )

    bad = banned_token(lex)
    if bad:
        return False, f"Query contains a disallowed token: {bad}."
    return True, ""


# ── Row-cap wrapper (no server-side row cap on the legacy JSON API) ──────────
# The legacy ``executeQueries`` endpoint has no ``resultSetRowCountLimit`` param
# (that only exists on the Fabric Arrow API), so a hard cap must be pushed into
# the DAX with TOPN. This is a *safety net*; the generator is instructed to emit
# TOPN for detail-grain queries already.

_LEADING_EVALUATE_RE = re.compile(r"^\s*EVALUATE\s+", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_START_AT_RE = re.compile(r"\bSTART\s+AT\b", re.IGNORECASE)
_LEADING_TOPN_RE = re.compile(r"^\s*TOPN\s*\(", re.IGNORECASE)


def apply_topn_cap(dax: str, max_rows: int) -> str:
    """Best-effort wrap a simple single-``EVALUATE`` query in ``TOPN(max_rows, …)``.

    Conservative by design: only rewrites the common
    ``EVALUATE <table-expr>`` shape with no ``DEFINE`` block, no ``ORDER BY`` /
    ``START AT`` clause and no leading ``TOPN`` already present. Anything more
    complex is returned unchanged and relies on the generator's TOPN plus the
    engine's 100k-row ceiling and post-fetch truncation detection.
    """
    if not dax or max_rows is None or max_rows <= 0:
        return dax
    lex = lex_dax(dax)
    if lex.has_define or lex.evaluate_count != 1:
        return dax
    m = _LEADING_EVALUATE_RE.match(dax)
    if not m:
        return dax
    body = dax[m.end():].strip()
    if not body:
        return dax
    if _ORDER_BY_RE.search(body) or _START_AT_RE.search(body):
        return dax
    if _LEADING_TOPN_RE.match(body):
        return dax
    return f"EVALUATE\nTOPN({int(max_rows)}, {body})"


__all__ = [
    "DaxIdentifier",
    "DaxLex",
    "lex_dax",
    "banned_token",
    "is_read_only_dax",
    "apply_topn_cap",
]
