"""Turn ledger — the compact, shared memory of the last N conversation turns.

The window itself is bounded before the graph runs (``conversation_context_turns``
rows fetched by the agent). This module turns those rows into one structure that
every consumer reads the same way:

* ``fused_router`` — decides whether the question refers to a prior turn and
  names it by handle (``T3``).
* ``sql_generator`` — replays each prior turn as a tool call whose result line
  tells the model what the query returned and what was answered.
* ``memory_answer_generator`` / ``prior_data_binder`` — resolve a handle to the
  turn's ``query_id`` and fetch its rows from the result store.

Each turn carries question, SQL, a short answer line, the result artifact
(columns, types, row count, stats) and whether its rows can be recovered
(``data_status``). Rows themselves never live in the ledger: they are fetched
by reference, so the prompt cost of memory is independent of result size.

``context_composer`` is the first graph node; it builds the ledger once and
records the memory telemetry surfaced as ``metrics.memory``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.agent.langgraph_agent.nodes.artifacts import parse_artifact
from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)

LEDGER_MAX_ANSWER_CHARS = 200
LEDGER_MAX_SQL_CHARS = 160
LEDGER_MAX_COLS = 8
_CHARS_PER_TOKEN = 4

DATA_STORED = "stored"      # snapshot rows persisted → recoverable on any replica
DATA_RERUN = "rerun"        # no snapshot, but SQL exists → recoverable by re-running
DATA_NONE = "none"          # text-only turn (greeting, clarification, proposal…)


def estimate_tokens(text: str) -> int:
    """~4 chars per token; good enough for budgets and telemetry."""
    return max(1, len(text) // _CHARS_PER_TOKEN) if text else 0


def _one_line(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _data_status(qa: Dict[str, Any], artifact: Optional[Dict[str, Any]]) -> str:
    if qa.get("result_kind") and qa.get("result_kind") != "table":
        return DATA_NONE
    if qa.get("snapshot_status") == "stored":
        return DATA_STORED
    if qa.get("generated_sql") and (artifact or qa.get("row_count")):
        return DATA_RERUN
    return DATA_NONE


def build_ledger(history: Any) -> List[Dict[str, Any]]:
    """Oldest-first ledger entries with handles ``T1``…``TN`` (``TN`` = most recent)."""
    ledger: List[Dict[str, Any]] = []
    for qa in history or []:
        question = (qa.get("natural_language_query") or "").strip()
        if not question:
            continue
        artifact = parse_artifact(qa.get("result_artifact"))
        row_count = (artifact or {}).get("row_count")
        if row_count is None:
            row_count = qa.get("row_count")
        ledger.append({
            "handle": f"T{len(ledger) + 1}",
            "query_id": str(qa.get("id")) if qa.get("id") else None,
            "question": question,
            "sql": qa.get("generated_sql") or None,
            "answer": _one_line(qa.get("answer"), LEDGER_MAX_ANSWER_CHARS),
            "row_count": row_count,
            "columns": list((artifact or {}).get("columns") or []),
            "column_types": dict((artifact or {}).get("column_types") or {}),
            "stats": dict((artifact or {}).get("stats") or {}),
            "data_status": _data_status(qa, artifact),
            "result_kind": qa.get("result_kind"),
            "created_at": qa.get("created_at"),
        })
    return ledger


def ledger_for(state: AgentState) -> List[Dict[str, Any]]:
    """The ledger from state, or built on the fly (nodes used outside the graph)."""
    ledger = state.get("memory_ledger")
    if ledger is None:
        ledger = build_ledger(state.get("conversation_history"))
    return list(ledger)


def find_turn(ledger: List[Dict[str, Any]], ref: Any) -> Optional[Dict[str, Any]]:
    """Resolve a handle (``T3``, ``t3``, ``3``) or a query_id to its ledger entry."""
    key = str(ref or "").strip()
    if not key:
        return None
    lowered = key.lower()
    if lowered.startswith("t") and lowered[1:].isdigit():
        lowered = lowered[1:]
    for turn in ledger:
        if turn["handle"].lower() == f"t{lowered}" or turn.get("query_id") == key:
            return turn
    return None


def latest_turn_with_data(ledger: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for turn in reversed(ledger):
        if turn["data_status"] != DATA_NONE and turn.get("query_id"):
            return turn
    return None


def _columns_line(turn: Dict[str, Any]) -> str:
    cols = turn.get("columns") or []
    types = turn.get("column_types") or {}
    shown = [f"{c}({types[c]})" if types.get(c) else str(c) for c in cols[:LEDGER_MAX_COLS]]
    return ", ".join(shown) + (", …" if len(cols) > LEDGER_MAX_COLS else "")


def _stats_line(turn: Dict[str, Any]) -> str:
    bits: List[str] = []
    for col, s in (turn.get("stats") or {}).items():
        if isinstance(s, dict) and "min" in s:
            bits.append(f"{col}: {s['min']}..{s['max']}")
        if len(bits) >= 2:
            break
    return "; ".join(bits)


def render_turn(turn: Dict[str, Any], *, index: int, total: int, include_sql: bool = True) -> str:
    tag = " (most recent)" if index == total - 1 else ""
    parts = [f'{turn["handle"]}{tag} · Q: "{_one_line(turn["question"], 120)}"']
    if include_sql and turn.get("sql"):
        parts.append(f"SQL: {_one_line(turn['sql'], LEDGER_MAX_SQL_CHARS)}")
    if turn.get("row_count") is not None:
        result = f"result: {turn['row_count']} rows"
        cols = _columns_line(turn)
        if cols:
            result += f"; cols: {cols}"
        stats = _stats_line(turn)
        if stats:
            result += f"; {stats}"
        parts.append(result)
    if turn.get("answer"):
        parts.append(f'A: "{turn["answer"]}"')
    parts.append(f"data: {turn['data_status']}")
    return " · ".join(parts)


def render_ledger(ledger: List[Dict[str, Any]], *, include_sql: bool = True) -> str:
    """Multi-line ledger for prompts; empty string when there is no history."""
    if not ledger:
        return ""
    total = len(ledger)
    lines = [render_turn(t, index=i, total=total, include_sql=include_sql) for i, t in enumerate(ledger)]
    return "Prior turns in this conversation (oldest first):\n" + "\n".join(lines)


def render_turn_tool_result(turn: Dict[str, Any]) -> str:
    """The ``tool`` message content replayed after a prior ``run_sql`` call."""
    parts: List[str] = []
    if turn.get("row_count") is not None:
        parts.append(f"{turn['row_count']} rows")
        cols = _columns_line(turn)
        if cols:
            parts.append(f"columns: {cols}")
    if turn.get("answer"):
        parts.append(f"answer: {turn['answer']}")
    return "; ".join(parts) or "Query executed successfully."


def memory_telemetry(state: AgentState, ledger: List[Dict[str, Any]]) -> Dict[str, Any]:
    history = state.get("conversation_history") or []
    window = state.get("memory_window")
    return {
        "turns_loaded": len(history),
        "window": window,
        "ledger_turns": len(ledger),
        "ledger_tokens_est": estimate_tokens(render_ledger(ledger)),
        "turns_with_data": sum(1 for t in ledger if t["data_status"] != DATA_NONE),
        "window_saturated": bool(window and len(history) >= int(window)),
    }


def context_composer(state: AgentState) -> Dict[str, Any]:
    """First node: build the ledger once and record memory telemetry."""
    ledger = build_ledger(state.get("conversation_history"))
    telemetry = memory_telemetry(state, ledger)
    logger.info(
        "context_composer: %d/%s turns · ≈%d tokens · data on %d",
        telemetry["turns_loaded"], telemetry["window"],
        telemetry["ledger_tokens_est"], telemetry["turns_with_data"],
    )
    return {"memory_ledger": ledger, "memory_telemetry": telemetry}
