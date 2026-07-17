"""Deterministic hybrid schema-linking for prompt-side catalog pruning.

Injecting an entire catalog (every table + every column) into the system prompt
is slow, expensive, and *reduces* accuracy on large schemas: the model drowns in
irrelevant columns. This module selects the tables/columns most relevant to the
current question so ``prompt_builder`` can inject a focused subset.

Design goals:
  * **Deterministic & cheap** — no extra LLM/embedding call. Scoring is lexical:
    token overlap between the question and table/column names + descriptions,
    with identifier splitting (``DimProduct`` → ``dim``, ``product``;
    ``fact_sales`` → ``fact``, ``sales``).
  * **Hybrid** — lexical scoring plus a relationship-graph expansion so joinable
    tables travel together even when only one side matched the question.
  * **Safe** — pruning affects *only* the prompt. The validation allowlist
    (``known_tables`` / ``known_columns``) is built from the full catalog in
    ``catalog_lookup``, so a pruned column can never cause a valid query to be
    rejected. When in doubt, the linker keeps more (falls back to the full
    bundle on any parse issue).

The public entry point is :func:`link_bundle`, which takes the formatted
metadata ``bundle`` (as produced by :class:`MetadataLoader`) and returns a new
bundle whose ``tables`` / ``columns`` strings are pruned. All other bundle keys
are passed through untouched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Tokens that carry no discriminative signal for schema linking.
_STOPWORDS: Set[str] = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "by", "with",
    "is", "are", "was", "were", "be", "at", "as", "from", "that", "this", "it",
    "how", "what", "which", "who", "when", "where", "why", "show", "list", "get",
    "give", "find", "count", "total", "sum", "avg", "average", "number", "many",
    "much", "top", "all", "each", "per", "me", "my", "we", "our", "you", "please",
    "id", "name", "date", "value", "amount",  # too generic across schemas
}

# Common description separators emitted by MetadataLoader / MCP prompts.
_TABLE_SEPARATORS = (" - ", " — ", " | ", ":")


def _split_identifier(token: str) -> List[str]:
    """Split a snake_case / camelCase / dotted identifier into lower subtokens."""
    # snake / kebab / dotted / whitespace → spaces
    spaced = re.sub(r"[_\-.\s]+", " ", token)
    # camelCase and letter/digit boundaries → spaces
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    spaced = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", spaced)
    parts = [p.lower() for p in spaced.split() if p]
    return parts


def _tokenize(text: str, *, keep_stopwords: bool = False) -> Set[str]:
    """Return a set of meaningful lower-case tokens from free text."""
    if not text:
        return set()
    raw = re.findall(r"[A-Za-z0-9_]+", text)
    tokens: Set[str] = set()
    for word in raw:
        for sub in _split_identifier(word):
            if len(sub) < 2:
                continue
            if not keep_stopwords and sub in _STOPWORDS:
                continue
            tokens.add(sub)
            # keep a naive singular/plural bridge (sales↔sale, orders↔order)
            if sub.endswith("s") and len(sub) > 3:
                tokens.add(sub[:-1])
    return tokens


# ── Bundle parsing ────────────────────────────────────────────────────────────


@dataclass
class _Column:
    table: str            # lower-cased table name
    name: str             # lower-cased column name
    line: str             # original formatted line (verbatim for re-emit)
    is_pk: bool = False
    tokens: Set[str] = field(default_factory=set)


@dataclass
class _Table:
    name: str             # lower-cased table name
    line: str             # original formatted line
    tokens: Set[str] = field(default_factory=set)
    columns: List[_Column] = field(default_factory=list)


def _parse_tables(tables_text: str) -> List[_Table]:
    tables: List[_Table] = []
    for raw_line in tables_text.splitlines():
        stripped = raw_line.lstrip("- ").strip()
        if not stripped:
            continue
        name = stripped
        desc = ""
        for sep in _TABLE_SEPARATORS:
            if sep in name:
                name, _, desc = name.partition(sep)
                name = name.strip()
                break
        if not name:
            continue
        low = name.lower()
        toks = _tokenize(name)
        toks |= _tokenize(desc)
        tables.append(_Table(name=low, line=raw_line.rstrip(), tokens=toks))
    return tables


def _parse_columns(columns_text: str) -> List[_Column]:
    cols: List[_Column] = []
    for raw_line in columns_text.splitlines():
        stripped = raw_line.lstrip("- ").strip()
        if not stripped:
            continue
        qualified = stripped.split(" - ")[0].strip()
        if "." not in qualified:
            continue
        table, _, column = qualified.partition(".")
        table = table.strip().lower()
        column = column.strip().lower()
        if not table or not column:
            continue
        is_pk = "PK: true" in stripped
        toks = _tokenize(column)
        # description tokens add signal ("revenue", "quantity", …)
        if " - " in stripped:
            toks |= _tokenize(stripped.split(" - ", 1)[1])
        cols.append(_Column(table=table, name=column, line=raw_line.rstrip(),
                            is_pk=is_pk, tokens=toks))
    return cols


def _build_adjacency(relationships_text: str, table_names: Set[str]) -> Dict[str, Set[str]]:
    """Best-effort table adjacency parsed from the free-form relationships blob.

    We don't assume a fixed format: for each relationship entry we collect the
    catalogued table names it mentions and connect them pairwise. Unrecognised
    text simply yields no edges.
    """
    adjacency: Dict[str, Set[str]] = {t: set() for t in table_names}
    if not relationships_text or not table_names:
        return adjacency
    # Split into rough entries; the formatter emits "[('a',), ('b',)]" or lines.
    chunks = re.split(r"\),\s*\(|\n", relationships_text)
    for chunk in chunks:
        toks = set(re.findall(r"[A-Za-z0-9_]+", chunk.lower()))
        mentioned = [t for t in table_names if t in toks]
        for i, a in enumerate(mentioned):
            for b in mentioned[i + 1:]:
                adjacency[a].add(b)
                adjacency[b].add(a)
    return adjacency


# ── Scoring & selection ─────────────────────────────────────────────────────


def _score_table(table: _Table, q_tokens: Set[str]) -> float:
    if not q_tokens:
        return 0.0
    score = 0.0
    # Table-name/description overlap (name matches weigh more via table.tokens).
    score += 3.0 * len(table.tokens & q_tokens)
    # Column overlap: each matching column contributes; PK matches a touch more.
    for col in table.columns:
        hit = len(col.tokens & q_tokens)
        if hit:
            score += 1.0 * hit + (0.5 if col.is_pk else 0.0)
    return score


def _select_columns_for_table(
    table: _Table, q_tokens: Set[str], cap: int
) -> List[_Column]:
    """Pick up to *cap* columns for a selected table: PKs + best question matches."""
    if len(table.columns) <= cap:
        return table.columns
    scored: List[Tuple[float, int, _Column]] = []
    for idx, col in enumerate(table.columns):
        s = float(len(col.tokens & q_tokens))
        if col.is_pk:
            s += 2.0  # always favour keys so joins/filters remain expressible
        scored.append((s, -idx, col))  # -idx keeps original order as tiebreak
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    chosen = [c for _, _, c in scored[:cap]]
    # Re-emit in original catalog order for readability.
    chosen_set = {id(c) for c in chosen}
    return [c for c in table.columns if id(c) in chosen_set]


def link_bundle(
    bundle: Dict[str, str],
    question: str,
    *,
    min_columns: int = 60,
    max_tables: int = 20,
    max_columns: int = 300,
    max_columns_per_table: int = 40,
) -> Tuple[Dict[str, str], bool]:
    """Return ``(bundle, pruned)`` with ``tables``/``columns`` focused on *question*.

    Returns the bundle unchanged (``pruned=False``) when the catalog is small
    enough (``<= min_columns``), when there's no question signal, or on any parse
    problem. Only the ``tables`` and ``columns`` keys are ever modified.
    """
    columns_text = bundle.get("columns", "") or ""
    tables_text = bundle.get("tables", "") or ""

    all_columns = _parse_columns(columns_text)
    if len(all_columns) <= min_columns:
        return bundle, False  # small schema → inject in full

    q_tokens = _tokenize(question)
    if not q_tokens:
        return bundle, False  # nothing to link against; keep full catalog

    tables = _parse_tables(tables_text)
    if not tables:
        return bundle, False

    by_name: Dict[str, _Table] = {t.name: t for t in tables}
    for col in all_columns:
        tbl = by_name.get(col.table)
        if tbl is not None:
            tbl.columns.append(col)

    scored = [(t, _score_table(t, q_tokens)) for t in tables]
    positive = [(t, s) for t, s in scored if s > 0]
    if not positive:
        # No lexical hits at all → don't guess; inject full catalog so the model
        # still has a chance. (Rare for a real question over a matching schema.)
        return bundle, False

    positive.sort(key=lambda ts: ts[1], reverse=True)
    selected: List[str] = [t.name for t, _ in positive[:max_tables]]
    selected_set: Set[str] = set(selected)

    # Relationship expansion: pull in 1-hop neighbours of selected tables so
    # joins remain expressible, respecting the table cap.
    adjacency = _build_adjacency(bundle.get("relationships", ""), set(by_name))
    if len(selected_set) < max_tables:
        for name in list(selected):
            for neighbour in adjacency.get(name, ()):  # deterministic-ish
                if neighbour not in selected_set:
                    selected_set.add(neighbour)
                    selected.append(neighbour)
                    if len(selected_set) >= max_tables:
                        break
            if len(selected_set) >= max_tables:
                break

    # Rebuild the tables block (original catalog order, selected only).
    kept_table_lines = [t.line for t in tables if t.name in selected_set]

    # Rebuild the columns block with per-table and global caps.
    kept_column_lines: List[str] = []
    remaining = max_columns
    for t in tables:  # original order
        if t.name not in selected_set or remaining <= 0:
            continue
        per_table_cap = min(max_columns_per_table, remaining)
        for col in _select_columns_for_table(t, q_tokens, per_table_cap):
            kept_column_lines.append(col.line)
            remaining -= 1
            if remaining <= 0:
                break

    if not kept_table_lines or not kept_column_lines:
        return bundle, False  # something went sideways → fail open to full bundle

    total_tables = len(tables)
    total_columns = len(all_columns)
    note = (
        f"(Showing {len(kept_table_lines)} of {total_tables} tables and "
        f"{len(kept_column_lines)} of {total_columns} columns most relevant to the "
        f"question. If a needed table/column isn't listed, it may still exist — "
        f"ask for it or rephrase.)"
    )

    new_bundle = dict(bundle)
    new_bundle["tables"] = "\n".join(kept_table_lines) + "\n" + note
    new_bundle["columns"] = "\n".join(kept_column_lines) + "\n" + note
    logger.info(
        "schema_linker: pruned catalog to %d/%d tables, %d/%d columns",
        len(kept_table_lines), total_tables, len(kept_column_lines), total_columns,
    )
    return new_bundle, True
