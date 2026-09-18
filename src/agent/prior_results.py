"""Recover the rows of an earlier turn by reference.

Conversation memory keeps *references* to prior results (the ledger handle and
``query_id``), never the rows themselves. When a follow-up needs the data —
replay, a computation over it, or values to bind into a new query — this store
recovers it, cheapest tier first:

1. ``result_cache`` — the in-process cache filled by the API after each answer.
   Fastest, but per-replica and TTL-bounded.
2. The stored snapshot — ``insights_turn_artifacts.result_snapshot`` written by
   ``save_to_memory`` (≤ ``CONVERSATION_SNAPSHOT_MAX_ROWS`` / ``_MAX_BYTES``).
   Survives restarts and is visible from every replica.
3. Re-running the turn's stored SQL through the connection's read-only runner
   (SQL agent only). Covers results that were too large to snapshot or have
   been pruned; the data may differ from what was shown if the source changed.

Rows produced by a memory computation carry ``MEMORY_SQL_MARKER`` in their SQL
(it runs over stored rows in the metadata database, not at the source) and are
never re-run.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.agent.conversation_artifacts import coerce_json_safe_rows
from src.agent.snapshot_sql import MEMORY_SQL_MARKER

logger = logging.getLogger(__name__)

SOURCE_CACHE = "cache"
SOURCE_SNAPSHOT = "snapshot"
SOURCE_RERUN = "rerun"

# ``rerun(sql, max_rows) -> {"columns": [...], "rows": [...], "error"?: str}``
Rerun = Callable[[str, int], Awaitable[Dict[str, Any]]]


class PriorResultStore:
    def __init__(
        self,
        *,
        history_service: Any = None,
        rerun: Optional[Rerun] = None,
        max_rows: int = 2000,
    ) -> None:
        self._history = history_service
        self._rerun = rerun
        self._max_rows = max(1, int(max_rows))

    async def rows(
        self,
        *,
        user_id: Any,
        connection: Any,
        query_id: Any,
        session_id: Any = None,
        sql: Optional[str] = None,
        allow_rerun: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """``{columns, rows, row_count, source, truncated}`` or ``None`` when the
        data cannot be recovered from any tier.

        ``allow_rerun=False`` restricts recovery to the cheap tiers (cache and
        stored snapshot). Callers use it before a model has decided the rows
        are actually needed, so a live query at the source is only ever issued
        on demand.
        """
        if not query_id:
            return None

        cached = self._from_cache(user_id, connection, query_id)
        if cached is not None:
            return cached

        snapshot = await self._from_snapshot(user_id, session_id, query_id)
        if snapshot is not None:
            return snapshot

        return await self._from_rerun(sql) if allow_rerun else None

    # ── Tiers ────────────────────────────────────────────────────────────

    def _from_cache(self, user_id: Any, connection: Any, query_id: Any) -> Optional[Dict[str, Any]]:
        try:
            from src.api.result_cache import result_cache  # lazy: avoids import cycle

            dataset = result_cache.get(user_id=user_id, connection=connection, query_id=query_id)
        except Exception:  # noqa: BLE001 — a cache problem must never block memory
            logger.debug("prior_results: cache lookup failed", exc_info=True)
            return None
        if not dataset or not dataset.get("rows"):
            return None
        return self._envelope(dataset.get("columns"), dataset["rows"], SOURCE_CACHE)

    async def _from_snapshot(self, user_id: Any, session_id: Any, query_id: Any) -> Optional[Dict[str, Any]]:
        if self._history is None or not session_id or not hasattr(self._history, "get_turn_artifact"):
            return None
        try:
            artifact = await self._history.get_turn_artifact(
                conversation_id=session_id, turn_id=query_id, user_id=str(user_id or "")
            )
        except Exception:  # noqa: BLE001
            logger.debug("prior_results: snapshot lookup failed", exc_info=True)
            return None
        results = (artifact or {}).get("results") if isinstance(artifact, dict) else None
        if not isinstance(results, dict) or not results.get("rows"):
            return None
        return self._envelope(results.get("columns"), results["rows"], SOURCE_SNAPSHOT)

    async def _from_rerun(self, sql: Optional[str]) -> Optional[Dict[str, Any]]:
        if self._rerun is None or not sql or sql.lstrip().startswith(MEMORY_SQL_MARKER):
            return None
        try:
            result = await self._rerun(sql, self._max_rows + 1)
        except Exception:  # noqa: BLE001
            logger.warning("prior_results: re-run of prior SQL failed", exc_info=True)
            return None
        if not isinstance(result, dict) or result.get("error") or not result.get("rows"):
            return None
        return self._envelope(result.get("columns"), coerce_json_safe_rows(result["rows"]), SOURCE_RERUN)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _envelope(self, columns: Any, rows: List[Any], source: str) -> Dict[str, Any]:
        rows = list(rows)
        truncated = len(rows) > self._max_rows
        if truncated:
            rows = rows[: self._max_rows]
        cols = [str(c) for c in (columns or [])]
        if not cols and rows and isinstance(rows[0], dict):
            cols = list(rows[0].keys())
        return {
            "columns": cols,
            "rows": rows,
            "row_count": len(rows),
            "source": source,
            "truncated": truncated,
        }


def make_sql_rerun(sql_runner: Any, *, statement_timeout_ms: int = 30000) -> Optional[Rerun]:
    """Adapt a ``SqlRunner`` to the store's ``rerun`` callable (None when the
    runner cannot execute, e.g. the DAX metadata introspector)."""
    run_sql = getattr(sql_runner, "run_sql", None)
    if run_sql is None:
        return None

    async def _rerun(sql: str, max_rows: int) -> Dict[str, Any]:
        return await run_sql(sql, limit=max_rows, max_rows=max_rows, statement_timeout_ms=statement_timeout_ms)

    return _rerun
