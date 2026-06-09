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
                        graph_time_ms = $6
                    WHERE id = $7
                    """,
                    execution_status,
                    execution_time_ms,
                    row_count,
                    json.dumps(result_preview) if result_preview else None,
                    error_message,
                    graph_time_ms,
                    query_id,
                )
        except Exception:
            logger.exception("Failed to update execution")

    # ------------------------------------------------------------------
    # Insights
    # ------------------------------------------------------------------
    async def add_insight(
        self,
        *,
        query_id: UUID,
        insight_type: str,
        content: str,
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
                    content,
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
        user_feedback: str,
        corrected_sql: Optional[str] = None,
        feedback_notes: Optional[str] = None,
    ) -> None:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE insights_conversation_sessions
                    SET user_feedback = $1,
                        corrected_sql = $2,
                        feedback_notes = $3
                    WHERE id = $4
                    """,
                    user_feedback,
                    corrected_sql,
                    feedback_notes,
                    query_id,
                )
        except Exception:
            logger.exception("Failed to record feedback")

    # ------------------------------------------------------------------
    # Read APIs
    # ------------------------------------------------------------------
    async def get_conversation_context(
        self,
        *,
        session_id: UUID,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT sequence_number, natural_language_query, generated_sql,
                           execution_status, created_at
                    FROM insights_conversation_sessions
                    WHERE session_id = $1
                    ORDER BY sequence_number DESC
                    LIMIT $2
                    """,
                    session_id,
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
        include_insights: bool = True,
    ) -> Dict[str, Any]:
        try:
            async with self.pool.acquire() as conn:
                queries = await conn.fetch(
                    """
                    SELECT *
                    FROM insights_conversation_sessions
                    WHERE session_id = $1
                    ORDER BY sequence_number ASC
                    """,
                    session_id,
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
