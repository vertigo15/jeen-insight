"""Conversation history service for Jeen Insights.

Tracks the complete query lifecycle (input -> LLM -> execution -> insights ->
feedback) in the shared metadata DB. Every row is partitioned by `source_key`
(the active connection) so multiple connections can share the same DB.

Backed by:
  * insights_conversation_sessions
  * insights_query_insights
  * insights_pinned_questions
  * insights_get_next_sequence_number(session_id)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import asyncpg

logger = logging.getLogger(__name__)


def insight_text(content: Any) -> str:
    """Flatten a rich insight value into plain text for storage.

    Summaries (and occasionally findings) arrive as a highlight-fragment array —
    ``[{"t": "Revenue rose", "hl": "pos"}, …]`` — which the UI renders itself.
    ``insights_query_insights.content`` is text, so keep the words and drop the
    styling rather than failing the whole write.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(
            str(seg.get("t") or "") if isinstance(seg, dict) else str(seg)
            for seg in content
        )
    if isinstance(content, dict):
        return str(content.get("t") or "")
    return str(content)


class ConversationHistoryService:
    """Reads/writes Jeen Insights operational tables.

    The pool is shared with `MetadataLoader` and `ConnectionService` (it points
    at METADATA_DB_*). Pass it in via the constructor.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def initialize(self) -> None:
        # Pool is already initialized by `get_metadata_pool()`. This method is
        # retained for API compatibility with the previous code.
        return None

    async def close(self) -> None:
        # Pool lifecycle is managed by `close_metadata_pool()`. No-op here.
        return None

    # ------------------------------------------------------------------
    # Sequence helper
    # ------------------------------------------------------------------
    async def get_next_sequence_number(self, session_id: UUID) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT insights_get_next_sequence_number($1)", session_id
            )
            return int(result or 1)

    # ------------------------------------------------------------------
    # Query lifecycle
    # ------------------------------------------------------------------
    async def log_query(
        self,
        *,
        user_id: str,
        source_key: str,
        session_id: UUID,
        natural_language_query: str,
        dataset_id: Optional[str] = None,
        schema_context: Optional[Dict[str, Any]] = None,
        rag_context: Optional[Dict[str, Any]] = None,
        parent_query_id: Optional[UUID] = None,
    ) -> UUID:
        try:
            sequence_number = await self.get_next_sequence_number(session_id)
            async with self.pool.acquire() as conn:
                # Link each turn to the previous one in the same session so the
                # conversation forms a chain the router can walk for follow-ups.
                if parent_query_id is None:
                    parent_query_id = await conn.fetchval(
                        """
                        SELECT id
                        FROM insights_conversation_sessions
                        WHERE session_id = $1 AND user_id = $2
                        ORDER BY sequence_number DESC
                        LIMIT 1
                        """,
                        session_id,
                        user_id,
                    )
                query_id = await conn.fetchval(
                    """
                    INSERT INTO insights_conversation_sessions (
                        user_id, source_key, session_id, sequence_number, parent_query_id,
                        natural_language_query, dataset_id, schema_context, rag_context,
                        execution_status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending')
                    RETURNING id
                    """,
                    user_id,
                    source_key,
                    session_id,
                    sequence_number,
                    parent_query_id,
                    natural_language_query,
                    dataset_id,
                    json.dumps(schema_context) if schema_context else None,
                    json.dumps(rag_context) if rag_context else None,
                )
                logger.info(
                    "📝 Logged query %s for session %s (seq %s, source=%s)",
                    query_id,
                    session_id,
                    sequence_number,
                    source_key,
                )
                return query_id
        except Exception:
            logger.exception("Failed to log query")
            return uuid4()

    async def update_llm_response(
        self,
        *,
        query_id: UUID,
        generated_sql: Optional[str],
        llm_model: str,
        llm_latency_ms: int,
        tokens_used: int,
    ) -> None:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE insights_conversation_sessions
                    SET generated_sql = $1,
                        llm_model = $2,
                        llm_latency_ms = $3,
                        tokens_used = $4
                    WHERE id = $5
                    """,
                    generated_sql,
                    llm_model,
                    llm_latency_ms,
                    tokens_used,
                    query_id,
                )
        except Exception:
            logger.exception("Failed to update LLM response")

    async def update_execution(
        self,
        *,
        query_id: UUID,
        execution_status: str,
        execution_time_ms: Optional[int] = None,
        row_count: Optional[int] = None,
        result_preview: Optional[List[Dict[str, Any]]] = None,
        error_message: Optional[str] = None,
        graph_time_ms: Optional[int] = None,
        result_artifact: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            if result_preview and len(result_preview) > 10:
                result_preview = result_preview[:10]
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE insights_conversation_sessions
                    SET execution_status = $1,
                        execution_time_ms = $2,
                        row_count = $3,
                        result_preview = $4,
                        error_message = $5,
                        graph_time_ms = $6,
                        result_artifact = $7
                    WHERE id = $8
                    """,
                    execution_status,
                    execution_time_ms,
                    row_count,
                    json.dumps(result_preview) if result_preview else None,
                    error_message,
                    graph_time_ms,
                    json.dumps(result_artifact) if result_artifact else None,
                    query_id,
                )
        except Exception:
            logger.exception("Failed to update execution")

    async def update_node_trace(
        self,
        *,
        query_id: UUID,
        node_trace: List[Dict[str, Any]],
    ) -> None:
        """Store the slim per-node timings for this run.

        Written after the graph finishes rather than from ``save_to_memory``,
        because the trace is not complete until the tail nodes have run. This
        is telemetry: a failure here must never affect the answer, so it is
        swallowed like the other updates on this service.
        """
        if not node_trace:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE insights_conversation_sessions
                    SET node_trace = $1
                    WHERE id = $2
                    """,
                    json.dumps(node_trace),
                    query_id,
                )
        except Exception:
            logger.exception("Failed to persist node trace")

    # ------------------------------------------------------------------
    # Insights
    # ------------------------------------------------------------------
    async def add_insight(
        self,
        *,
        query_id: UUID,
        insight_type: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
        llm_model: Optional[str] = None,
        llm_execution_time_ms: Optional[int] = None,
        tokens_input: Optional[int] = None,
        tokens_output: Optional[int] = None,
    ) -> UUID:
        try:
            async with self.pool.acquire() as conn:
                insight_id = await conn.fetchval(
                    """
                    INSERT INTO insights_query_insights (
                        query_id, insight_type, content, metadata,
                        llm_model, llm_execution_time_ms, tokens_input, tokens_output
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    query_id,
                    insight_type,
                    insight_text(content),
                    json.dumps(metadata) if metadata else None,
                    llm_model,
                    llm_execution_time_ms,
                    tokens_input,
                    tokens_output,
                )
                return insight_id
        except Exception:
            logger.exception("Failed to add insight")
            return uuid4()

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------
    async def record_feedback(
        self,
        *,
        query_id: UUID,
        user_id: str,
        user_feedback: str,
        corrected_sql: Optional[str] = None,
        feedback_notes: Optional[str] = None,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_conversation_sessions
                    SET user_feedback = $1,
                        corrected_sql = $2,
                        feedback_notes = $3
                    WHERE id = $4 AND user_id = $5
                    """,
                    user_feedback,
                    corrected_sql,
                    feedback_notes,
                    query_id,
                    user_id,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to record feedback")
            return False

    # ------------------------------------------------------------------
    # Read APIs
    # ------------------------------------------------------------------
    async def get_conversation_context(
        self,
        *,
        session_id: UUID,
        user_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, parent_query_id, sequence_number,
                           natural_language_query, generated_sql,
                           execution_status, row_count, result_preview,
                           result_artifact, created_at
                    FROM insights_conversation_sessions
                    WHERE session_id = $1 AND user_id = $2
                    ORDER BY sequence_number DESC
                    LIMIT $3
                    """,
                    session_id,
                    user_id,
                    limit,
                )
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Failed to get conversation context")
            return []

    async def get_conversation_history(
        self,
        *,
        session_id: UUID,
        user_id: str,
        include_insights: bool = True,
    ) -> Dict[str, Any]:
        try:
            async with self.pool.acquire() as conn:
                queries = await conn.fetch(
                    """
                    SELECT *
                    FROM insights_conversation_sessions
                    WHERE session_id = $1 AND user_id = $2
                    ORDER BY sequence_number ASC
                    """,
                    session_id,
                    user_id,
                )
                queries_list: List[Dict[str, Any]] = []
                for q in queries:
                    qd = dict(q)
                    if include_insights:
                        ins = await conn.fetch(
                            """
                            SELECT insight_type, content, metadata,
                                   llm_model, llm_execution_time_ms,
                                   tokens_input, tokens_output, created_at
                            FROM insights_query_insights
                            WHERE query_id = $1
                            ORDER BY created_at ASC
                            """,
                            qd["id"],
                        )
                        qd["insights"] = [dict(i) for i in ins]
                    queries_list.append(qd)
                return {
                    "session_id": str(session_id),
                    "query_count": len(queries_list),
                    "queries": queries_list,
                }
        except Exception:
            logger.exception("Failed to get conversation history")
            return {"session_id": str(session_id), "query_count": 0, "queries": []}

    async def query_belongs_to_user(
        self,
        *,
        query_id: UUID | str,
        user_id: str,
        source_key: Optional[str] = None,
    ) -> bool:
        try:
            qid = query_id if isinstance(query_id, UUID) else UUID(str(query_id))
            async with self.pool.acquire() as conn:
                if source_key:
                    owner = await conn.fetchval(
                        """
                        SELECT 1
                        FROM insights_conversation_sessions
                        WHERE id = $1 AND user_id = $2 AND source_key = $3
                        """,
                        qid,
                        user_id,
                        source_key,
                    )
                else:
                    owner = await conn.fetchval(
                        """
                        SELECT 1
                        FROM insights_conversation_sessions
                        WHERE id = $1 AND user_id = $2
                        """,
                        qid,
                        user_id,
                    )
                return bool(owner)
        except Exception:
            logger.exception("Failed to verify query ownership")
            return False

    async def session_belongs_to_user(
        self,
        *,
        session_id: UUID | str,
        user_id: str,
    ) -> bool:
        """Return true when a session is empty or already owned by this user."""
        try:
            sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
            async with self.pool.acquire() as conn:
                owner = await conn.fetchval(
                    """
                    SELECT user_id
                    FROM insights_conversation_sessions
                    WHERE session_id = $1
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    sid,
                )
                return owner is None or owner == user_id
        except Exception:
            logger.exception("Failed to verify session ownership")
            return False

    # ------------------------------------------------------------------
    # Recent / pinned questions (per user + connection)
    # ------------------------------------------------------------------
    async def get_user_recent_questions(
        self,
        *,
        user_id: str,
        source_key: str,
        limit: int = 15,
    ) -> List[str]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT natural_language_query, MAX(created_at) AS last_asked
                    FROM insights_conversation_sessions
                    WHERE user_id = $1
                      AND source_key = $2
                      AND natural_language_query NOT IN (
                        SELECT question
                        FROM insights_pinned_questions
                        WHERE user_id = $1 AND source_key = $2
                      )
                    GROUP BY natural_language_query
                    ORDER BY last_asked DESC
                    LIMIT $3
                    """,
                    user_id,
                    source_key,
                    limit,
                )
                return [r["natural_language_query"] for r in rows]
        except Exception:
            logger.exception("Failed to get user recent questions")
            return []

    async def get_user_pinned_questions(
        self,
        *,
        user_id: str,
        source_key: str,
    ) -> List[str]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT question
                    FROM insights_pinned_questions
                    WHERE user_id = $1 AND source_key = $2
                    ORDER BY pinned_at DESC
                    """,
                    user_id,
                    source_key,
                )
                return [r["question"] for r in rows]
        except Exception:
            logger.exception("Failed to get user pinned questions")
            return []

    async def get_history_log(
        self,
        *,
        user_id: str,
        source_key: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return all queries for a user+connection, newest first.

        Each entry includes the question text, execution status, token usage,
        LLM and execution latency, row count, session/conversation id, and ISO
        timestamp so the UI can render a full audit / activity log.
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        id,
                        session_id,
                        natural_language_query,
                        generated_sql,
                        execution_status,
                        llm_latency_ms,
                        tokens_used,
                        execution_time_ms,
                        graph_time_ms,
                        row_count,
                        error_message,
                        created_at
                    FROM insights_conversation_sessions
                    WHERE user_id = $1 AND source_key = $2
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    user_id,
                    source_key,
                    limit,
                )
            return [
                {
                    "query_id":   str(r["id"]),
                    "session_id": str(r["session_id"]) if r["session_id"] else None,
                    "question":   r["natural_language_query"],
                    "status":     r["execution_status"],
                    "tokens":     r["tokens_used"],
                    "llm_ms":     r["llm_latency_ms"],
                    "exec_ms":    r["execution_time_ms"],
                    "graph_ms":   r["graph_time_ms"],
                    "row_count":  r["row_count"],
                    "error":      r["error_message"],
                    "asked_at":   r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to get history log")
            return []

    # ------------------------------------------------------------------
    # Saved analyses (durable table/chart/insights snapshots)
    # ------------------------------------------------------------------
    async def save_analysis(
        self,
        *,
        user_id: str,
        source_key: str,
        name: str,
        question: str,
        generated_sql: Optional[str],
        query_id: Optional[UUID | str],
        columns: List[Any],
        rows: List[Any],
        chart_spec: Optional[Dict[str, Any]] = None,
        chart_config: Optional[Dict[str, Any]] = None,
        insights_payload: Optional[Dict[str, Any]] = None,
        connection_id: Optional[str] = None,
    ) -> UUID:
        if query_id and not await self.query_belongs_to_user(
            query_id=query_id, user_id=user_id, source_key=source_key
        ):
            raise PermissionError("query does not belong to user")
        qid = UUID(str(query_id)) if query_id else None
        snapshot = {"columns": columns or [], "rows": rows or []}
        async with self.pool.acquire() as conn:
            saved_id = await conn.fetchval(
                """
                INSERT INTO insights_saved_analyses (
                    user_id, source_key, connection_id, query_id, name, question,
                    generated_sql, columns, row_count, result_snapshot,
                    chart_spec, chart_config, insights_payload
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING id
                """,
                user_id,
                source_key,
                connection_id,
                qid,
                name.strip()[:180] or question.strip()[:80] or "Saved analysis",
                question,
                generated_sql,
                json.dumps(columns or []),
                len(rows or []),
                json.dumps(snapshot),
                json.dumps(chart_spec) if chart_spec else None,
                json.dumps(chart_config) if chart_config else None,
                json.dumps(insights_payload) if insights_payload else None,
            )
            return saved_id

    async def list_saved_analyses(
        self,
        *,
        user_id: str,
        source_key: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, name, question, source_key, query_id, row_count,
                           chart_config IS NOT NULL AS has_chart,
                           insights_payload IS NOT NULL AS has_insights,
                           created_at, updated_at
                    FROM insights_saved_analyses
                    WHERE user_id = $1 AND source_key = $2 AND deleted_at IS NULL
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    user_id,
                    source_key,
                    limit,
                )
            return [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "question": r["question"],
                    "source_key": r["source_key"],
                    "query_id": str(r["query_id"]) if r["query_id"] else None,
                    "row_count": r["row_count"],
                    "has_chart": r["has_chart"],
                    "has_insights": r["has_insights"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to list saved analyses")
            return []

    async def get_saved_analysis(
        self,
        *,
        saved_id: UUID,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM insights_saved_analyses
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    """,
                    saved_id,
                    user_id,
                )
            if not row:
                return None
            data = dict(row)
            for key in ("columns", "result_snapshot", "chart_spec", "chart_config", "insights_payload"):
                val = data.get(key)
                if isinstance(val, str):
                    try:
                        data[key] = json.loads(val)
                    except Exception:
                        pass
            data["id"] = str(data["id"])
            if data.get("query_id"):
                data["query_id"] = str(data["query_id"])
            for key in ("created_at", "updated_at", "deleted_at"):
                if data.get(key):
                    data[key] = data[key].isoformat()
            return data
        except Exception:
            logger.exception("Failed to get saved analysis")
            return None

    async def update_saved_analysis(
        self,
        *,
        saved_id: UUID,
        user_id: str,
        name: Optional[str] = None,
        chart_spec: Optional[Dict[str, Any]] = None,
        chart_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_saved_analyses
                    SET name = COALESCE($1, name),
                        chart_spec = COALESCE($2::jsonb, chart_spec),
                        chart_config = COALESCE($3::jsonb, chart_config),
                        updated_at = NOW()
                    WHERE id = $4 AND user_id = $5 AND deleted_at IS NULL
                    """,
                    name.strip()[:180] if isinstance(name, str) and name.strip() else None,
                    json.dumps(chart_spec) if chart_spec is not None else None,
                    json.dumps(chart_config) if chart_config is not None else None,
                    saved_id,
                    user_id,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to update saved analysis")
            return False

    async def delete_saved_analysis(self, *, saved_id: UUID, user_id: str) -> bool:
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_saved_analyses
                    SET deleted_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    """,
                    saved_id,
                    user_id,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to delete saved analysis")
            return False

    async def pin_question(
        self,
        *,
        user_id: str,
        source_key: str,
        question: str,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO insights_pinned_questions (user_id, source_key, question)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, source_key, question) DO NOTHING
                    """,
                    user_id,
                    source_key,
                    question,
                )
                return True
        except Exception:
            logger.exception("Failed to pin question")
            return False

    async def unpin_question(
        self,
        *,
        user_id: str,
        source_key: str,
        question: str,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM insights_pinned_questions
                    WHERE user_id = $1 AND source_key = $2 AND question = $3
                    """,
                    user_id,
                    source_key,
                    question,
                )
                return True
        except Exception:
            logger.exception("Failed to unpin question")
            return False
