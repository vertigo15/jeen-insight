"""Helpers for reasoning about prior result artifacts.

A *result artifact* is the compact, durable summary persisted alongside each
executed query (see ``conversation_history.update_execution`` and
``output._build_result_artifact``). These helpers turn the raw conversation
history into signals the router and memory-answer nodes can use to decide
whether a question is a follow-up over already-retrieved data.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# How many prior results to surface in the router manifest.
_MANIFEST_MAX_ITEMS = 3
# How many columns to list per artifact so the manifest stays compact.
_MANIFEST_MAX_COLS = 8


def parse_artifact(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a result-artifact dict from a DB value (dict or JSON string)."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _artifact_columns_line(artifact: Dict[str, Any]) -> str:
    columns = artifact.get("columns") or []
    types = artifact.get("column_types") or {}
    shown = columns[:_MANIFEST_MAX_COLS]
    parts = []
    for col in shown:
        t = types.get(col)
        parts.append(f"{col}({t})" if t else str(col))
    suffix = ", …" if len(columns) > _MANIFEST_MAX_COLS else ""
    return ", ".join(parts) + suffix


def _artifact_stats_line(artifact: Dict[str, Any]) -> str:
    """Compact min..max hints for a couple of numeric columns."""
    stats = artifact.get("stats") or {}
    bits: List[str] = []
    for col, s in stats.items():
        if not isinstance(s, dict) or "min" not in s:
            continue
        bits.append(f"{col}: {s['min']}..{s['max']}")
        if len(bits) >= 2:
            break
    return "; ".join(bits)


def build_artifact_manifest(history: List[Dict[str, Any]], limit: int = _MANIFEST_MAX_ITEMS) -> str:
    """Return a compact manifest of recent results, newest first, or "".

    Example::

        Prior results available in this conversation (most recent first):
        [1] "total sales by year" — 12 rows; cols: orderyear(int), total(float); total: 25000000..29000000
    """
    if not history:
        return ""
    # History arrives oldest-first (agent reverses it); take the most recent
    # successful turns that carry an artifact.
    entries: List[str] = []
    for qa in reversed(history):
        artifact = parse_artifact(qa.get("result_artifact"))
        if not artifact:
            continue
        question = (qa.get("natural_language_query") or "").strip()
        row_count = artifact.get("row_count")
        if row_count is None:
            row_count = qa.get("row_count")
        cols = _artifact_columns_line(artifact)
        line = f'[{len(entries) + 1}] "{question[:80]}" — {row_count} rows'
        if cols:
            line += f"; cols: {cols}"
        stats = _artifact_stats_line(artifact)
        if stats:
            line += f"; {stats}"
        entries.append(line)
        if len(entries) >= limit:
            break
    if not entries:
        return ""
    header = "Prior results available in this conversation (most recent first):"
    return header + "\n" + "\n".join(entries)


def latest_result_ref(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return {query_id, question, artifact} for the most recent result, or None."""
    if not history:
        return None
    for qa in reversed(history):
        artifact = parse_artifact(qa.get("result_artifact"))
        if artifact:
            return {
                "query_id": qa.get("id"),
                "question": qa.get("natural_language_query"),
                "artifact": artifact,
            }
    return None
