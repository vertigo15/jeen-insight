"""Conversation history service for Jeen Insights.

Tracks the complete query lifecycle (input -> LLM -> execution -> insights ->
feedback) in the shared metadata DB. Every row is partitioned by `source_key`
(the active connection) so multiple connections can share the same DB.

Backed by:
  * insights_conversations              (one row per conversation; id == session_id)
  * insights_conversation_sessions      (one row per turn)
  * insights_turn_artifacts             (answer / snapshot / chart per turn)
  * insights_favorite_answers            (per-user saved turns)
  * insights_conversation_prune_state   (per user+connection retention claim)
  * insights_query_insights
  * insights_pinned_questions
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import asyncpg

from src.agent.conversation_artifacts import (
    RESULT_KIND_ERROR,
    RESULT_KIND_TABLE,
    RESULT_KIND_TEXT,
    SNAPSHOT_NOT_APPLICABLE,
    SNAPSHOT_PRUNED,
    SNAPSHOT_STORED,
    conversation_title_from_question,
)

logger = logging.getLogger(__name__)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if value is None or isinstance(value, str) else str(value)


def _jsonb(value: Any) -> Any:
    """asyncpg returns JSONB as str unless a codec is registered; decode it."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


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

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        persistence_enabled: bool = True,
        conversation_schema_ready: bool = True,
    ):
        self.pool = pool
        # Kill switch for per-turn artifact capture and retention (config, and
        # forced off by the lifespan when migration 022 is missing).
        self.persistence_enabled = persistence_enabled
        # Whether migration 022 (insights_conversations + parent FK) is applied.
        # Independent of the kill switch: once the FK exists a turn cannot be
        # inserted without its conversation row, and before it exists the
        # legacy (pre-conversation) SQL must be used so questions keep working.
        self.conversation_schema_ready = conversation_schema_ready
        # Whether migration 023 added ``analysis`` / ``low_confidence`` to the
        # turn artifact. Probed by the lifespan; off by default so a database
        # without the columns keeps persisting ordinary turns.
        self.analysis_schema_ready = False
        # Whether migration 030 added answer-level favorites. This stays
        # independent from migration 022 so rolling upgrades keep conversation
        # hydration working before the new table has been applied.
        self.favorite_schema_ready = False
        # Whether migration 035 added insights_answer_feedback (thumbs, star
        # rating, message). Probed by the lifespan; without it thumbs fall back
        # to the legacy user_feedback column on the turn row.
        self.answer_feedback_schema_ready = False

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
        source_label: Optional[str] = None,
    ) -> UUID:
        """Insert one turn, creating/bumping its conversation in the same transaction.

        The conversation row is locked (``FOR UPDATE``) before the sequence
        number is allocated, so two tabs writing to the same conversation can
        never collide on ``(session_id, sequence_number)``. A ``session_id``
        that already belongs to another user or another connection is refused.

        Before migration 022 is applied the conversation table does not exist,
        so the pre-conversation insert path is used instead.
        """
        if not self.conversation_schema_ready:
            return await self._log_query_legacy(
                user_id=user_id,
                source_key=source_key,
                session_id=session_id,
                natural_language_query=natural_language_query,
                dataset_id=dataset_id,
                schema_context=schema_context,
                rag_context=rag_context,
                parent_query_id=parent_query_id,
            )
        try:
            title = conversation_title_from_question(natural_language_query)
            label = (source_label or "").strip() or source_key
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO insights_conversations
                            (id, user_id, source_key, source_label, title)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (id) DO UPDATE
                            SET last_activity_at = NOW(),
                                source_label = COALESCE(NULLIF($4, ''), insights_conversations.source_label)
                        WHERE insights_conversations.user_id = $2
                          AND insights_conversations.source_key = $3
                        """,
                        session_id,
                        user_id,
                        source_key,
                        label,
                        title,
                    )
                    owned = await conn.fetchval(
                        """
                        SELECT 1
                        FROM insights_conversations
                        WHERE id = $1 AND user_id = $2 AND source_key = $3
                        FOR UPDATE
                        """,
                        session_id,
                        user_id,
                        source_key,
                    )
                    if not owned:
                        raise PermissionError(
                            "session_id belongs to another user or connection"
                        )
                    sequence_number = await conn.fetchval(
                        """
                        SELECT COALESCE(MAX(sequence_number), 0) + 1
                        FROM insights_conversation_sessions
                        WHERE session_id = $1
                        """,
                        session_id,
                    )
                    # Link each turn to the previous one in the same session so
                    # the conversation forms a chain the router can walk.
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
                        int(sequence_number or 1),
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

    async def _log_query_legacy(
        self,
        *,
        user_id: str,
        source_key: str,
        session_id: UUID,
        natural_language_query: str,
        dataset_id: Optional[str],
        schema_context: Optional[Dict[str, Any]],
        rag_context: Optional[Dict[str, Any]],
        parent_query_id: Optional[UUID],
    ) -> UUID:
        """Pre-022 insert (no conversation row, DB-side sequence helper)."""
        try:
            sequence_number = await self.get_next_sequence_number(session_id)
            async with self.pool.acquire() as conn:
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
                "📝 Logged query %s for session %s (seq %s, source=%s, legacy schema)",
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
        # COALESCE: a bare thumbs click must not erase a note or a corrected
        # query the user already attached to the same turn.
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_conversation_sessions
                    SET user_feedback = $1,
                        corrected_sql = COALESCE($2, corrected_sql),
                        feedback_notes = COALESCE($3, feedback_notes)
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

    async def record_answer_feedback(
        self,
        *,
        query_id: UUID,
        user_id: str,
        thumb: Optional[str] = None,
        rating: Optional[int] = None,
        feedback_type: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append one answer-feedback event (migration 035).

        Ownership is enforced in the INSERT: the row is only written when the
        turn belongs to ``user_id``; ``source_key`` is copied from the turn,
        never taken from the client. Returns ``{"id", "thumb", "source_key"}``
        where ``thumb`` is the turn's current thumb after this event, or
        ``None`` when the turn is not the caller's. Database errors propagate
        so the route can answer 5xx instead of a misleading 404.
        """
        if not self.answer_feedback_schema_ready:
            return None
        async with self.pool.acquire() as conn:
            # A data-modifying CTE's rows are invisible to sibling SELECTs in
            # the same statement, so the "current thumb" is derived from the
            # inserted row itself when it carries a thumb and from the prior
            # events otherwise (a dialog submission never changes the thumb).
            row = await conn.fetchrow(
                """
                WITH owned AS (
                    SELECT id, source_key
                    FROM insights_conversation_sessions
                    WHERE id = $1 AND user_id = $2
                ), inserted AS (
                    INSERT INTO insights_answer_feedback
                        (query_id, user_id, source_key, thumb, rating, feedback_type, message)
                    SELECT owned.id, $2, owned.source_key, $3, $4, $5, $6
                    FROM owned
                    RETURNING id, query_id, source_key, thumb
                )
                SELECT inserted.id, inserted.source_key,
                       COALESCE(inserted.thumb,
                                (SELECT f.thumb FROM insights_answer_feedback f
                                  WHERE f.query_id = inserted.query_id AND f.thumb IS NOT NULL
                                  ORDER BY f.event_seq DESC LIMIT 1)) AS thumb
                FROM inserted
                """,
                query_id,
                user_id,
                thumb,
                rating,
                feedback_type,
                message,
            )
        if not row:
            return None
        current = row["thumb"] if row["thumb"] in ("thumbs_up", "thumbs_down") else None
        return {"id": str(row["id"]), "thumb": current, "source_key": row["source_key"]}

    def _thumb_select(self, turn_alias: str = "cs") -> str:
        """Current thumb per turn: newest thumb event, 'cleared' reads as none.

        Falls back to the legacy column on the turn row for turns that have no
        event yet (thumbs posted by an older UI build to /api/feedback after
        the migration's backfill ran); a 'cleared' event never falls back.
        Before migration 035 only the legacy column is read.
        """
        if not self.answer_feedback_schema_ready:
            return f", {turn_alias}.user_feedback"
        return (
            ", NULLIF(COALESCE((SELECT f.thumb FROM insights_answer_feedback f "
            f"WHERE f.query_id = {turn_alias}.id AND f.thumb IS NOT NULL "
            "ORDER BY f.event_seq DESC LIMIT 1), "
            f"{turn_alias}.user_feedback), 'cleared') AS user_feedback"
        )

    # ------------------------------------------------------------------
    # Read APIs
    # ------------------------------------------------------------------
    async def get_conversation_context(
        self,
        *,
        session_id: UUID,
        user_id: str,
        limit: int = 5,
        source_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recent turns of one conversation, newest first.

        Joins the conversation row so context can never mix connections or
        continue a conversation that has been deleted. Before migration 022 the
        same scoping is applied on the turn rows themselves.

        In-flight turns (``execution_status = 'pending'``) are excluded: the
        agents fetch this context concurrently with inserting the current
        turn's pending row, so without the filter the question being answered
        could nondeterministically appear as its own "prior turn" and take one
        of the window's slots. ``IS DISTINCT FROM`` keeps legacy rows whose
        status is NULL.

        With the conversation schema each row also carries the turn's stored
        ``answer`` (plain text), ``result_kind`` and ``snapshot_status`` from
        ``insights_turn_artifacts``, so the memory ledger can present question,
        answer and data availability without a second query.
        """
        try:
            async with self.pool.acquire() as conn:
                if self.conversation_schema_ready:
                    rows = await conn.fetch(
                        """
                        SELECT cs.id, cs.parent_query_id, cs.sequence_number,
                               cs.natural_language_query, cs.generated_sql,
                               cs.execution_status, cs.row_count, cs.result_preview,
                               cs.result_artifact, cs.created_at,
                               a.answer AS turn_answer, a.result_kind, a.snapshot_status
                        FROM insights_conversation_sessions cs
                        JOIN insights_conversations c ON c.id = cs.session_id
                        LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id
                        WHERE cs.session_id = $1
                          AND cs.user_id = $2
                          AND c.user_id = $2
                          AND ($4::text IS NULL OR c.source_key = $4)
                          AND cs.execution_status IS DISTINCT FROM 'pending'
                        ORDER BY cs.sequence_number DESC
                        LIMIT $3
                        """,
                        session_id,
                        user_id,
                        limit,
                        source_key,
                    )
                    out = []
                    for r in rows:
                        d = dict(r)
                        d["answer"] = insight_text(_jsonb(d.pop("turn_answer", None)))
                        out.append(d)
                    return out
                else:
                    rows = await conn.fetch(
                        """
                        SELECT id, parent_query_id, sequence_number,
                               natural_language_query, generated_sql,
                               execution_status, row_count, result_preview,
                               result_artifact, created_at
                        FROM insights_conversation_sessions
                        WHERE session_id = $1
                          AND user_id = $2
                          AND ($4::text IS NULL OR source_key = $4)
                          AND execution_status IS DISTINCT FROM 'pending'
                        ORDER BY sequence_number DESC
                        LIMIT $3
                        """,
                        session_id,
                        user_id,
                        limit,
                        source_key,
                    )
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Failed to get conversation context")
            return []

    async def search_turns(
        self,
        *,
        user_id: str,
        source_key: Optional[str],
        keywords: List[str],
        since: datetime,
        until: datetime,
        limit: int = 10,
        exclude_query_id: Optional[UUID | str] = None,
    ) -> List[Dict[str, Any]]:
        """Entries of the application history log whose question mentions any of
        *keywords* in ``[since, until)``, newest first.

        Backs the ``history_lookup`` route ("did I ask about revenue last
        week?"). Reads exactly what :meth:`get_history_log` shows — every query
        of this user on this connection in ``insights_conversation_sessions`` —
        so the answer matches the History log drawer. Case-insensitive
        substring match; with no keywords the time window alone applies. The
        turn being answered right now is excluded so a question can never match
        itself. Never raises.

        Indexes: ``idx_insights_turns_user_source_recent`` (022) serves the
        user/source/window scan and the ordering; ``idx_insights_turns_question_trgm``
        (026, when ``pg_trgm`` is available) serves the ``ILIKE``.
        """
        words = [w.strip() for w in (keywords or []) if w and w.strip()]
        args: List[Any] = [user_id, source_key, since, until]
        clauses: List[str] = []
        predicates: List[str] = []
        for word in words:
            args.append(f"%{word}%")
            predicates.append(f"cs.natural_language_query ILIKE ${len(args)}")
        if predicates:
            clauses.append(f"AND ({' OR '.join(predicates)})")
        if exclude_query_id:
            args.append(str(exclude_query_id))
            clauses.append(f"AND cs.id::text <> ${len(args)}")
        args.append(max(1, int(limit)))
        limit_param = f"${len(args)}"
        extra = "\n                          ".join(clauses)
        # The stored answer lives in insights_turn_artifacts (migration 022+).
        answer_select = "a.answer AS turn_answer" if self.conversation_schema_ready else "NULL AS turn_answer"
        answer_join = "LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id" if self.conversation_schema_ready else ""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT cs.id, cs.session_id, cs.natural_language_query, cs.generated_sql,
                           cs.execution_status, cs.row_count, cs.created_at, {answer_select}
                    FROM insights_conversation_sessions cs
                    {answer_join}
                    WHERE cs.user_id = $1
                      AND ($2::text IS NULL OR cs.source_key = $2)
                      AND cs.created_at >= $3 AND cs.created_at < $4
                      {extra}
                    ORDER BY cs.created_at DESC
                    LIMIT {limit_param}
                    """,
                    *args,
                )
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["answer"] = insight_text(_jsonb(d.pop("turn_answer", None)))
                out.append(d)
            return out
        except Exception:
            logger.exception("Failed to search the history log")
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

    async def conversation_belongs_to_user(
        self,
        *,
        session_id: UUID | str,
        user_id: str,
        source_key: Optional[str] = None,
    ) -> bool:
        """True only when a conversation with this id exists, is owned by
        *user_id* and (when given) lives on *source_key*.

        Unknown ids are rejected: a client may only continue conversations the
        server handed out, which closes the "append to a guessed UUID" hole of
        the previous empty-session allowance.

        Before migration 022 there is no conversation table, so the check falls
        back to the turn rows: the first turn's owner (and connection) must
        match, and a session with no rows yet is allowed as before.
        """
        try:
            sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
        except (TypeError, ValueError):
            return False
        try:
            async with self.pool.acquire() as conn:
                if self.conversation_schema_ready:
                    found = await conn.fetchval(
                        """
                        SELECT 1
                        FROM insights_conversations
                        WHERE id = $1
                          AND user_id = $2
                          AND ($3::text IS NULL OR source_key = $3)
                        """,
                        sid,
                        user_id,
                        source_key,
                    )
                    return bool(found)
                first = await conn.fetchrow(
                    """
                    SELECT user_id, source_key
                    FROM insights_conversation_sessions
                    WHERE session_id = $1
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    sid,
                )
                if first is None:
                    return True
                if first["user_id"] != user_id:
                    return False
                return source_key is None or first["source_key"] == source_key
        except Exception:
            logger.exception("Failed to verify conversation ownership")
            return False

    async def session_belongs_to_user(
        self,
        *,
        session_id: UUID | str,
        user_id: str,
    ) -> bool:
        """Backward-compatible alias; delegates to the conversation check."""
        return await self.conversation_belongs_to_user(
            session_id=session_id, user_id=user_id
        )

    # ------------------------------------------------------------------
    # Conversations (restore / browse)
    # ------------------------------------------------------------------
    def _conversation_summary_sql(self) -> str:
        saved_count = (
            """
            (SELECT COUNT(*)
               FROM insights_favorite_answers f
               JOIN insights_conversation_sessions ft ON ft.id = f.turn_id
              WHERE f.user_id = c.user_id AND ft.session_id = c.id)
            """
            if self.favorite_schema_ready
            else "0::bigint"
        )
        return f"""
            SELECT c.id, c.title, c.source_key, c.source_label,
                   c.created_at, c.last_activity_at,
                   (SELECT COUNT(*) FROM insights_conversation_sessions t
                     WHERE t.session_id = c.id) AS turn_count,
                   (SELECT t.natural_language_query
                      FROM insights_conversation_sessions t
                     WHERE t.session_id = c.id
                     ORDER BY t.sequence_number DESC
                     LIMIT 1) AS last_question,
                   {saved_count} AS saved_answer_count
            FROM insights_conversations c
        """

    @staticmethod
    def _summary_row(row: Any) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "title": row["title"],
            "source_key": row["source_key"],
            "source_label": row["source_label"],
            "turn_count": int(row["turn_count"] or 0),
            "saved_answer_count": int(
                ConversationHistoryService._row_get(row, "saved_answer_count", 0) or 0
            ),
            "last_question": row["last_question"],
            "created_at": _iso(row["created_at"]),
            "last_activity_at": _iso(row["last_activity_at"]),
        }

    async def get_last_conversation(
        self, *, user_id: str, source_key: str
    ) -> Optional[Dict[str, Any]]:
        if not self.conversation_schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    self._conversation_summary_sql()
                    + """
                    WHERE c.user_id = $1 AND c.source_key = $2
                    ORDER BY c.last_activity_at DESC, c.id DESC
                    LIMIT 1
                    """,
                    user_id,
                    source_key,
                )
            return self._summary_row(row) if row else None
        except Exception:
            logger.exception("Failed to get last conversation")
            return None

    async def get_conversation(
        self, *, conversation_id: UUID, user_id: str
    ) -> Optional[Dict[str, Any]]:
        if not self.conversation_schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    self._conversation_summary_sql()
                    + " WHERE c.id = $1 AND c.user_id = $2",
                    conversation_id,
                    user_id,
                )
            return self._summary_row(row) if row else None
        except Exception:
            logger.exception("Failed to get conversation")
            return None

    async def list_conversations(
        self,
        *,
        user_id: str,
        source_key: Optional[str] = None,
        limit: int = 50,
        before: Optional[Tuple[datetime, UUID]] = None,
    ) -> List[Dict[str, Any]]:
        """Conversations newest first; ``before`` is the (last_activity_at, id)
        cursor of the last item of the previous page."""
        if not self.conversation_schema_ready:
            return []
        try:
            before_ts = before[0] if before else None
            before_id = before[1] if before else None
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    self._conversation_summary_sql()
                    + """
                    WHERE c.user_id = $1
                      AND ($2::text IS NULL OR c.source_key = $2)
                      AND ($3::timestamptz IS NULL
                           OR (c.last_activity_at, c.id) < ($3::timestamptz, $4::uuid))
                    ORDER BY c.last_activity_at DESC, c.id DESC
                    LIMIT $5
                    """,
                    user_id,
                    source_key,
                    before_ts,
                    before_id,
                    limit,
                )
            return [self._summary_row(r) for r in rows]
        except Exception:
            logger.exception("Failed to list conversations")
            return []

    @staticmethod
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        """asyncpg.Record raises on a missing column; older SELECTs omit the ML columns."""
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    @staticmethod
    def _turn_row(row: Any) -> Dict[str, Any]:
        status = row["execution_status"] or "pending"
        sql = row["generated_sql"]
        result_kind = row["result_kind"]
        snapshot_status = row["snapshot_status"]
        if result_kind is None:
            # Legacy turn (pre-artifact): derive what the UI needs.
            if row["error_message"] or status not in ("success", "pending"):
                result_kind = RESULT_KIND_ERROR
            elif sql:
                result_kind = RESULT_KIND_TABLE
            else:
                result_kind = RESULT_KIND_TEXT
        if snapshot_status is None:
            snapshot_status = (
                SNAPSHOT_PRUNED if result_kind == RESULT_KIND_TABLE else SNAPSHOT_NOT_APPLICABLE
            )
        has_chart = bool(row["has_chart"]) and snapshot_status == SNAPSHOT_STORED
        return {
            "turn_id": str(row["id"]),
            "sequence_number": int(row["sequence_number"]),
            "question": row["natural_language_query"],
            "sql": sql,
            "execution_status": status,
            "result_kind": result_kind,
            "answer": _jsonb(row["answer"]),
            "error": row["artifact_error"] or row["error_message"],
            "metrics": _jsonb(row["metrics"]),
            "findings": _jsonb(row["findings"]),
            "suggestions": _jsonb(row["suggestions"]),
            "followups": _jsonb(row["followups"]),
            "snapshot_status": snapshot_status,
            "row_count": row["row_count"],
            "has_chart": has_chart,
            "has_rerunnable_query": bool(sql) and result_kind == RESULT_KIND_TABLE,
            "created_at": _iso(row["created_at"]),
            "snapshot_at": _iso(row["snapshot_at"]),
            "analysis": _jsonb(ConversationHistoryService._row_get(row, "analysis")),
            "low_confidence": bool(ConversationHistoryService._row_get(row, "low_confidence", False)),
            "is_favorite": bool(ConversationHistoryService._row_get(row, "is_favorite", False)),
            # Only the two thumb values reach the client; the legacy column can
            # also hold 'edited' / 'catalog_gap', which are not pressed states.
            "user_feedback": (
                ConversationHistoryService._row_get(row, "user_feedback", None)
                if ConversationHistoryService._row_get(row, "user_feedback", None)
                in ("thumbs_up", "thumbs_down") else None
            ),
        }

    def _analysis_select(self, alias: str = "a") -> str:
        """Extra artifact columns, only when migration 023 is applied."""
        if not self.analysis_schema_ready:
            return ""
        return f", {alias}.analysis, {alias}.low_confidence"

    def _favorite_select(self, turn_alias: str = "cs", conversation_alias: str = "c") -> str:
        """Favorite state without touching migration 030 on older databases."""
        if not self.favorite_schema_ready:
            return ", FALSE AS is_favorite"
        return (
            ", EXISTS (SELECT 1 FROM insights_favorite_answers f "
            f"WHERE f.user_id = {conversation_alias}.user_id "
            f"AND f.turn_id = {turn_alias}.id) AS is_favorite"
        )

    async def get_conversation_turns(
        self,
        *,
        conversation_id: UUID,
        user_id: str,
        limit: int = 50,
        before_sequence: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Turn metadata (no rows, no chart config), newest first."""
        if not self.conversation_schema_ready:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT cs.id, cs.sequence_number, cs.natural_language_query,
                           cs.generated_sql, cs.execution_status, cs.row_count,
                           cs.error_message, cs.created_at,
                           a.result_kind, a.answer, a.error AS artifact_error,
                           a.metrics, a.findings, a.suggestions, a.followups,
                           a.snapshot_status, a.snapshot_at,
                           (a.chart_config IS NOT NULL) AS has_chart
                           {self._analysis_select()}{self._favorite_select()}{self._thumb_select()}
                    FROM insights_conversation_sessions cs
                    JOIN insights_conversations c ON c.id = cs.session_id
                    LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id
                    WHERE cs.session_id = $1
                      AND c.user_id = $2
                      AND ($3::int IS NULL OR cs.sequence_number < $3)
                    ORDER BY cs.sequence_number DESC
                    LIMIT $4
                    """,
                    conversation_id,
                    user_id,
                    before_sequence,
                    limit,
                )
            return [self._turn_row(r) for r in rows]
        except Exception:
            logger.exception("Failed to get conversation turns")
            return []

    async def get_conversation_turn(
        self, *, conversation_id: UUID, turn_id: UUID, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Metadata for one owned turn, including its favorite state."""
        if not self.conversation_schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT cs.id, cs.sequence_number, cs.natural_language_query,
                           cs.generated_sql, cs.execution_status, cs.row_count,
                           cs.error_message, cs.created_at,
                           a.result_kind, a.answer, a.error AS artifact_error,
                           a.metrics, a.findings, a.suggestions, a.followups,
                           a.snapshot_status, a.snapshot_at,
                           (a.chart_config IS NOT NULL) AS has_chart
                           {self._analysis_select()}{self._favorite_select()}{self._thumb_select()}
                    FROM insights_conversation_sessions cs
                    JOIN insights_conversations c ON c.id = cs.session_id
                    LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id
                    WHERE cs.id = $1 AND cs.session_id = $2 AND c.user_id = $3
                    """,
                    turn_id,
                    conversation_id,
                    user_id,
                )
            return self._turn_row(row) if row else None
        except Exception:
            logger.exception("Failed to get conversation turn")
            return None

    async def get_turn_artifact(
        self, *, conversation_id: UUID, turn_id: UUID, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Blobs for one turn. Chart fields are only returned alongside rows."""
        if not self.conversation_schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT a.result_snapshot, a.snapshot_status, a.snapshot_at,
                           a.chart_spec, a.chart_config{self._analysis_select()}
                    FROM insights_conversation_sessions cs
                    JOIN insights_conversations c ON c.id = cs.session_id
                    LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id
                    WHERE cs.id = $1 AND cs.session_id = $2 AND c.user_id = $3
                    """,
                    turn_id,
                    conversation_id,
                    user_id,
                )
            if row is None:
                return None
            results = _jsonb(row["result_snapshot"])
            status = row["snapshot_status"] or SNAPSHOT_PRUNED
            return {
                "turn_id": str(turn_id),
                "results": results,
                "chart_spec": _jsonb(row["chart_spec"]) if results is not None else None,
                "chart_config": _jsonb(row["chart_config"]) if results is not None else None,
                "snapshot_status": status,
                "snapshot_at": _iso(row["snapshot_at"]),
                "analysis": _jsonb(self._row_get(row, "analysis")),
                "low_confidence": bool(self._row_get(row, "low_confidence", False)),
            }
        except Exception:
            logger.exception("Failed to get turn artifact")
            return None

    async def get_turn_analysis(
        self, *, turn_id: UUID, user_id: str, source_key: str
    ) -> Optional[Dict[str, Any]]:
        """The persisted ML analysis of one turn (owner + connection bound), for re-runs."""
        if not (self.conversation_schema_ready and self.analysis_schema_ready):
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT cs.id, cs.session_id, cs.natural_language_query, a.analysis, a.low_confidence
                    FROM insights_conversation_sessions cs
                    JOIN insights_conversations c ON c.id = cs.session_id
                    LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id
                    WHERE cs.id = $1 AND c.user_id = $2 AND c.source_key = $3
                    """,
                    turn_id, user_id, source_key,
                )
            if row is None:
                return None
            return {
                "turn_id": str(row["id"]),
                "session_id": row["session_id"],
                "question": row["natural_language_query"],
                "analysis": _jsonb(row["analysis"]),
                "low_confidence": bool(row["low_confidence"]),
            }
        except Exception:
            logger.exception("Failed to load turn analysis")
            return None

    async def get_turn_for_rerun(
        self, *, conversation_id: UUID, turn_id: UUID, user_id: str
    ) -> Optional[Dict[str, Any]]:
        if not self.conversation_schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT cs.id, cs.generated_sql, cs.natural_language_query,
                           c.source_key, c.source_label
                    FROM insights_conversation_sessions cs
                    JOIN insights_conversations c ON c.id = cs.session_id
                    WHERE cs.id = $1 AND cs.session_id = $2 AND c.user_id = $3
                    """,
                    turn_id,
                    conversation_id,
                    user_id,
                )
            if row is None:
                return None
            return {
                "turn_id": str(row["id"]),
                "sql": row["generated_sql"],
                "question": row["natural_language_query"],
                "source_key": row["source_key"],
                "source_label": row["source_label"],
            }
        except Exception:
            logger.exception("Failed to load turn for rerun")
            return None

    async def upsert_turn_artifact(
        self,
        *,
        turn_id: UUID,
        result_kind: str,
        answer: Any = None,
        error: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        findings: Optional[List[str]] = None,
        suggestions: Optional[List[str]] = None,
        followups: Optional[List[str]] = None,
        result_snapshot: Optional[Dict[str, Any]] = None,
        snapshot_status: str = SNAPSHOT_NOT_APPLICABLE,
        snapshot_bytes: Optional[int] = None,
        analysis: Optional[Dict[str, Any]] = None,
        low_confidence: bool = False,
    ) -> bool:
        """Guarded write from ``save_to_memory``.

        No-op when the turn row is gone (hard delete) or when the artifact was
        already pruned: ``pruned`` is terminal for normal writes. The ML
        columns are written only when migration 023 is applied.
        """
        if not self.conversation_schema_ready:
            return False
        try:
            args: List[Any] = [
                turn_id,
                result_kind,
                json.dumps(answer) if answer is not None else None,
                error,
                json.dumps(metrics) if metrics else None,
                json.dumps(findings) if findings else None,
                json.dumps(suggestions) if suggestions else None,
                json.dumps(followups) if followups else None,
                json.dumps(result_snapshot) if result_snapshot is not None else None,
                snapshot_status,
                snapshot_bytes,
            ]
            if self.analysis_schema_ready:
                extra_cols = ", analysis, low_confidence"
                extra_vals = ", $12::jsonb, $13::boolean"
                extra_set = (
                    ", analysis = EXCLUDED.analysis, low_confidence = EXCLUDED.low_confidence"
                )
                args.extend([json.dumps(analysis, default=str) if analysis else None, bool(low_confidence)])
            else:
                extra_cols = extra_vals = extra_set = ""
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    f"""
                    INSERT INTO insights_turn_artifacts (
                        turn_id, result_kind, answer, error, metrics,
                        findings, suggestions, followups,
                        result_snapshot, snapshot_status, snapshot_bytes, snapshot_at{extra_cols}
                    )
                    SELECT $1::uuid, $2::text, $3::jsonb, $4::text, $5::jsonb,
                           $6::jsonb, $7::jsonb, $8::jsonb,
                           $9::jsonb, $10::text, $11::int,
                           CASE WHEN $10::text = 'stored' THEN NOW() END{extra_vals}
                    WHERE EXISTS (
                        SELECT 1 FROM insights_conversation_sessions WHERE id = $1::uuid
                    )
                    ON CONFLICT (turn_id) DO UPDATE
                        SET result_kind     = EXCLUDED.result_kind,
                            answer          = EXCLUDED.answer,
                            error           = EXCLUDED.error,
                            metrics         = EXCLUDED.metrics,
                            findings        = EXCLUDED.findings,
                            suggestions     = EXCLUDED.suggestions,
                            followups       = EXCLUDED.followups,
                            result_snapshot = EXCLUDED.result_snapshot,
                            snapshot_status = EXCLUDED.snapshot_status,
                            snapshot_bytes  = EXCLUDED.snapshot_bytes,
                            snapshot_at     = EXCLUDED.snapshot_at,
                            updated_at      = NOW(){extra_set}
                    WHERE insights_turn_artifacts.snapshot_status <> 'pruned'
                    """,
                    *args,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to upsert turn artifact for %s", turn_id)
            return False

    async def upsert_turn_chart(
        self,
        *,
        turn_id: UUID,
        user_id: str,
        chart_spec: Optional[Dict[str, Any]],
        chart_config: Optional[Dict[str, Any]],
        chart_bytes: int,
        request_started_at: Optional[datetime] = None,
    ) -> bool:
        """Persist the server-built chart baseline for a turn.

        Only turns whose snapshot is ``stored`` take a chart: a chart cannot be
        restored without its rows, and ``pruned`` stays terminal. When a
        request-start timestamp is supplied, ``chart_updated_at`` is a
        last-writer watermark: a slower request that started earlier cannot
        replace a chart produced by a newer request.
        """
        if not self.conversation_schema_ready:
            return False
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_turn_artifacts a
                    SET chart_spec       = $3::jsonb,
                        chart_config     = $4::jsonb,
                        chart_bytes      = $5,
                        chart_updated_at = COALESCE($6::timestamptz, NOW()),
                        updated_at       = NOW()
                    FROM insights_conversation_sessions cs
                    WHERE a.turn_id = $1
                      AND cs.id = a.turn_id
                      AND cs.user_id = $2
                      AND a.snapshot_status = 'stored'
                      AND (
                          $6::timestamptz IS NULL
                          OR a.chart_updated_at IS NULL
                          OR a.chart_updated_at <= $6::timestamptz
                      )
                    """,
                    turn_id,
                    user_id,
                    json.dumps(chart_spec) if chart_spec is not None else None,
                    json.dumps(chart_config) if chart_config is not None else None,
                    chart_bytes,
                    request_started_at,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to persist chart for turn %s", turn_id)
            return False

    async def get_chart_request_watermark(self) -> Optional[datetime]:
        """Return a database-clock watermark for ordering chart writes.

        Both request-start ordering and rerun invalidation then use PostgreSQL's
        clock, so API replica clock skew cannot let an obsolete chart win.
        """
        if not self.conversation_schema_ready:
            return None
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchval("SELECT clock_timestamp()")
        except Exception:
            logger.exception("Failed to obtain chart request watermark")
            return None

    async def clear_turn_chart(
        self,
        *,
        turn_id: UUID,
        user_id: str,
        request_started_at: Optional[datetime] = None,
    ) -> bool:
        """Drop a stored chart baseline (e.g. the new chart exceeded the byte cap
        and must not be restored from an obsolete config).

        A guarded clear retains its request timestamp as a watermark even
        though the chart columns become null. This prevents an older in-flight
        generation from repopulating the obsolete baseline afterward.
        """
        if not self.conversation_schema_ready:
            return False
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_turn_artifacts a
                    SET chart_spec = NULL, chart_config = NULL, chart_bytes = NULL,
                        chart_updated_at = CASE
                            WHEN $3::timestamptz IS NULL THEN NULL
                            ELSE $3::timestamptz
                        END,
                        updated_at = NOW()
                    FROM insights_conversation_sessions cs
                    WHERE a.turn_id = $1 AND cs.id = a.turn_id AND cs.user_id = $2
                      AND (
                          ($3::timestamptz IS NULL AND a.chart_config IS NOT NULL)
                          OR (
                              $3::timestamptz IS NOT NULL
                              AND (
                                  a.chart_updated_at IS NULL
                                  OR a.chart_updated_at <= $3::timestamptz
                              )
                          )
                      )
                    """,
                    turn_id,
                    user_id,
                    request_started_at,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to clear chart for turn %s", turn_id)
            return False

    async def store_rerun_snapshot(
        self,
        *,
        turn_id: UUID,
        user_id: str,
        result_snapshot: Optional[Dict[str, Any]],
        snapshot_status: str,
        snapshot_bytes: Optional[int],
    ) -> bool:
        """Rerun write: the one path allowed to promote a pruned/too_large turn
        back to ``stored``. Clears the chart baseline because it was built from
        the previous rows."""
        if not self.conversation_schema_ready:
            return False
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    INSERT INTO insights_turn_artifacts (
                        turn_id, result_kind, result_snapshot, snapshot_status,
                        snapshot_bytes, snapshot_at, chart_updated_at
                    )
                    SELECT $1::uuid, 'table', $3::jsonb, $4::text, $5::int,
                           CASE WHEN $4::text = 'stored' THEN NOW() END,
                           NOW()
                    WHERE EXISTS (
                        SELECT 1 FROM insights_conversation_sessions
                        WHERE id = $1::uuid AND user_id = $2::text
                    )
                    ON CONFLICT (turn_id) DO UPDATE
                        SET result_snapshot  = EXCLUDED.result_snapshot,
                            snapshot_status  = EXCLUDED.snapshot_status,
                            snapshot_bytes   = EXCLUDED.snapshot_bytes,
                            snapshot_at      = EXCLUDED.snapshot_at,
                            chart_spec       = NULL,
                            chart_config     = NULL,
                            chart_bytes      = NULL,
                            chart_updated_at = NOW(),
                            updated_at       = NOW()
                    """,
                    turn_id,
                    user_id,
                    json.dumps(result_snapshot) if result_snapshot is not None else None,
                    snapshot_status,
                    snapshot_bytes,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to store rerun snapshot for %s", turn_id)
            return False

    async def rename_conversation(
        self, *, conversation_id: UUID, user_id: str, title: str
    ) -> bool:
        if not self.conversation_schema_ready:
            return False
        clean = conversation_title_from_question(title)
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_conversations
                    SET title = $3
                    WHERE id = $1 AND user_id = $2
                    """,
                    conversation_id,
                    user_id,
                    clean,
                )
                return result.endswith(" 1")
        except Exception:
            logger.exception("Failed to rename conversation")
            return False

    async def delete_conversation(
        self,
        *,
        conversation_id: UUID,
        user_id: str,
        delete_saved: bool = False,
    ) -> Dict[str, Any]:
        """Hard delete an owned conversation after protecting saved answers.

        Favorite mutations, retention and deletion share one advisory lock per
        user + connection, so a concurrent Save cannot slip between the count
        and the cascading DELETE.
        """
        if not self.conversation_schema_ready:
            return {"status": "missing", "saved_answer_count": 0}
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    source_key = await conn.fetchval(
                        "SELECT source_key FROM insights_conversations WHERE id = $1 AND user_id = $2",
                        conversation_id,
                        user_id,
                    )
                    if not source_key:
                        return {"status": "missing", "saved_answer_count": 0}
                    if self.favorite_schema_ready:
                        await conn.fetchval(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1 || ':' || $2, 0))",
                            user_id,
                            source_key,
                        )
                        still_owned = await conn.fetchval(
                            "SELECT 1 FROM insights_conversations WHERE id = $1 AND user_id = $2",
                            conversation_id,
                            user_id,
                        )
                        if not still_owned:
                            return {"status": "missing", "saved_answer_count": 0}
                        saved_count = int(await conn.fetchval(
                            """
                            SELECT COUNT(*)
                            FROM insights_favorite_answers f
                            JOIN insights_conversation_sessions cs ON cs.id = f.turn_id
                            WHERE f.user_id = $1 AND cs.session_id = $2
                            """,
                            user_id,
                            conversation_id,
                        ) or 0)
                        if saved_count and not delete_saved:
                            return {
                                "status": "blocked",
                                "saved_answer_count": saved_count,
                            }
                    else:
                        saved_count = 0
                    result = await conn.execute(
                        "DELETE FROM insights_conversations WHERE id = $1 AND user_id = $2",
                        conversation_id,
                        user_id,
                    )
                    if not result.endswith(" 1"):
                        return {"status": "missing", "saved_answer_count": saved_count}
                    return {"status": "deleted", "saved_answer_count": saved_count}
        except Exception:
            logger.exception("Failed to delete conversation")
            return {"status": "error", "saved_answer_count": 0}

    # ------------------------------------------------------------------
    # Favorite answers
    # ------------------------------------------------------------------
    async def set_answer_favorite(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        user_id: str,
        favorite: bool,
    ) -> Optional[bool]:
        """Set one owned successful turn's favorite state.

        ``None`` means a favorite request targets a missing/not-owned turn.
        Removing a missing turn returns ``False`` because its cascaded favorite
        row is already gone, keeping DELETE idempotent for stale clients.
        """
        if not (self.conversation_schema_ready and self.favorite_schema_ready):
            return None
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    source_key = await conn.fetchval(
                        """
                        SELECT c.source_key
                        FROM insights_conversation_sessions cs
                        JOIN insights_conversations c ON c.id = cs.session_id
                        WHERE cs.id = $1
                          AND cs.session_id = $2
                          AND c.user_id = $3
                          AND cs.execution_status = 'success'
                        """,
                        turn_id,
                        conversation_id,
                        user_id,
                    )
                    if not source_key:
                        return None if favorite else False
                    # Serialize favorite mutations with retention for this user
                    # and connection. Re-check ownership after acquiring the
                    # lock because a prune may have won between the first lookup
                    # and this lock.
                    await conn.fetchval(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1 || ':' || $2, 0))",
                        user_id,
                        source_key,
                    )
                    owned = await conn.fetchval(
                        """
                        SELECT 1
                        FROM insights_conversation_sessions cs
                        JOIN insights_conversations c ON c.id = cs.session_id
                        WHERE cs.id = $1
                          AND cs.session_id = $2
                          AND c.user_id = $3
                          AND cs.execution_status = 'success'
                        """,
                        turn_id,
                        conversation_id,
                        user_id,
                    )
                    if not owned:
                        return None if favorite else False
                    if favorite:
                        await conn.execute(
                            """
                            INSERT INTO insights_favorite_answers (user_id, turn_id)
                            VALUES ($1, $2)
                            ON CONFLICT (user_id, turn_id) DO NOTHING
                            """,
                            user_id,
                            turn_id,
                        )
                    else:
                        await conn.execute(
                            """
                            DELETE FROM insights_favorite_answers
                            WHERE user_id = $1 AND turn_id = $2
                            """,
                            user_id,
                            turn_id,
                        )
            return favorite
        except Exception:
            logger.exception("Failed to update answer favorite for %s", turn_id)
            return None

    @staticmethod
    def _favorite_row(row: Any) -> Dict[str, Any]:
        return {
            "conversation_id": str(row["conversation_id"]),
            "turn_id": str(row["turn_id"]),
            "sequence_number": int(row["sequence_number"]),
            "conversation_title": row["conversation_title"],
            "question": row["question"],
            "answer": _jsonb(row["answer"]),
            "result_kind": row["result_kind"] or RESULT_KIND_TEXT,
            "snapshot_status": row["snapshot_status"] or SNAPSHOT_NOT_APPLICABLE,
            "source_key": row["source_key"],
            "source_label": row["source_label"],
            "created_at": _iso(row["created_at"]),
            "favorited_at": _iso(row["favorited_at"]),
        }

    async def list_favorite_answers(
        self,
        *,
        user_id: str,
        source_key: Optional[str] = None,
        limit: int = 100,
        before: Optional[Tuple[datetime, UUID]] = None,
    ) -> List[Dict[str, Any]]:
        """Favorited answers, newest favorite first, with enough metadata to reopen."""
        if not (self.conversation_schema_ready and self.favorite_schema_ready):
            return []
        try:
            before_ts = before[0] if before else None
            before_id = before[1] if before else None
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT cs.session_id AS conversation_id, cs.id AS turn_id,
                           cs.sequence_number, cs.natural_language_query AS question,
                           cs.created_at, c.title AS conversation_title,
                           c.source_key, c.source_label,
                           a.answer, a.result_kind, a.snapshot_status,
                           f.created_at AS favorited_at
                    FROM insights_favorite_answers f
                    JOIN insights_conversation_sessions cs ON cs.id = f.turn_id
                    JOIN insights_conversations c ON c.id = cs.session_id
                    LEFT JOIN insights_turn_artifacts a ON a.turn_id = cs.id
                    WHERE f.user_id = $1
                      AND c.user_id = $1
                      AND ($2::text IS NULL OR c.source_key = $2)
                      AND ($3::timestamptz IS NULL
                           OR (f.created_at, f.turn_id) < ($3::timestamptz, $4::uuid))
                    ORDER BY f.created_at DESC, f.turn_id DESC
                    LIMIT $5
                    """,
                    user_id,
                    source_key,
                    before_ts,
                    before_id,
                    max(1, min(int(limit), 200)),
                )
            return [self._favorite_row(row) for row in rows]
        except Exception:
            logger.exception("Failed to list favorite answers")
            return []

    async def prune_user_conversations(
        self,
        *,
        user_id: str,
        source_key: str,
        keep_last: int,
        keep_last_turns: int,
        min_interval_seconds: int,
        protect_conversation_id: Optional[UUID] = None,
    ) -> Dict[str, Any]:
        """Count-based retention for one user + connection, in one transaction.

        1. Claim the ``(user_id, source_key)`` prune-state row; if another
           replica pruned within *min_interval_seconds* the whole run is skipped.
        2. Delete conversations ranked beyond *keep_last* (never the protected
           one or a conversation containing a favorite). FK cascades remove
           their turns, insights and artifacts.
        3. Drop the blobs of non-favorite turns ranked beyond *keep_last_turns*
           across all of the user's turns on this connection, marking them
           ``pruned``. Favorites do not consume the ordinary retention budget.
        """
        if not self.conversation_schema_ready:
            return {"skipped": "schema_missing"}
        started = time.monotonic()
        favorite_conversation_guard = ""
        favorite_turn_guard = ""
        if self.favorite_schema_ready:
            favorite_conversation_guard = """
                          AND NOT EXISTS (
                              SELECT 1
                              FROM insights_conversation_sessions ft
                              JOIN insights_favorite_answers f ON f.turn_id = ft.id
                              WHERE ft.session_id = c.id AND f.user_id = $1
                          )
            """
            favorite_turn_guard = """
                              AND NOT EXISTS (
                                  SELECT 1 FROM insights_favorite_answers f
                                  WHERE f.user_id = $1 AND f.turn_id = cs.id
                              )
            """
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.fetchval(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1 || ':' || $2, 0))",
                        user_id,
                        source_key,
                    )
                    claimed = await conn.fetchval(
                        """
                        INSERT INTO insights_conversation_prune_state
                            (user_id, source_key, last_pruned_at)
                        VALUES ($1, $2, NOW())
                        ON CONFLICT (user_id, source_key) DO UPDATE
                            SET last_pruned_at = NOW()
                        WHERE insights_conversation_prune_state.last_pruned_at
                              < NOW() - make_interval(secs => $3)
                        RETURNING 1
                        """,
                        user_id,
                        source_key,
                        float(max(0, min_interval_seconds)),
                    )
                    if not claimed:
                        return {"skipped": "recently_pruned"}

                    deleted = await conn.fetch(
                        f"""
                        WITH ranked AS (
                            SELECT id,
                                   ROW_NUMBER() OVER (
                                       ORDER BY last_activity_at DESC, id DESC
                                   ) AS rn
                            FROM insights_conversations
                            WHERE user_id = $1 AND source_key = $2
                        )
                        DELETE FROM insights_conversations c
                        USING ranked r
                        WHERE c.id = r.id
                          AND r.rn > $3
                          AND c.id IS DISTINCT FROM $4::uuid
                          {favorite_conversation_guard}
                        RETURNING c.id
                        """,
                        user_id,
                        source_key,
                        max(1, keep_last),
                        protect_conversation_id,
                    )
                    pruned = await conn.fetch(
                        f"""
                        WITH ranked AS (
                            SELECT a.turn_id,
                                   ROW_NUMBER() OVER (
                                       ORDER BY cs.created_at DESC, cs.id DESC
                                   ) AS rn
                            FROM insights_turn_artifacts a
                            JOIN insights_conversation_sessions cs ON cs.id = a.turn_id
                            WHERE cs.user_id = $1
                              AND cs.source_key = $2
                              AND (a.result_snapshot IS NOT NULL
                                   OR a.chart_config IS NOT NULL)
                              {favorite_turn_guard}
                        )
                        UPDATE insights_turn_artifacts a
                        SET result_snapshot  = NULL,
                            snapshot_bytes   = NULL,
                            chart_spec       = NULL,
                            chart_config     = NULL,
                            chart_bytes      = NULL,
                            snapshot_status  = 'pruned',
                            updated_at       = NOW()
                        FROM ranked r
                        WHERE a.turn_id = r.turn_id
                          AND r.rn > $3
                        RETURNING a.turn_id
                        """,
                        user_id,
                        source_key,
                        max(1, keep_last_turns),
                    )
            stats = {
                "deleted_conversations": len(deleted),
                "pruned_turns": len(pruned),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            logger.info(
                "conversation_prune user=%s source=%s deleted=%d pruned=%d duration_ms=%d",
                user_id,
                source_key,
                stats["deleted_conversations"],
                stats["pruned_turns"],
                stats["duration_ms"],
                extra={"event": "conversation_prune", **stats},
            )
            return stats
        except Exception:
            logger.exception(
                "conversation_prune failed user=%s source=%s", user_id, source_key
            )
            return {"skipped": "error"}

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
        chart_state: Optional[Dict[str, Any]] = None,
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
                    chart_spec, chart_config, chart_state, insights_payload
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
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
                json.dumps(chart_state) if chart_state else None,
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
            for key in (
                "columns", "result_snapshot", "chart_spec", "chart_config",
                "chart_state", "insights_payload",
            ):
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
        chart_state: Optional[Dict[str, Any]] = None,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE insights_saved_analyses
                    SET name = COALESCE($1, name),
                        chart_spec = COALESCE($2::jsonb, chart_spec),
                        chart_config = COALESCE($3::jsonb, chart_config),
                        chart_state = COALESCE($4::jsonb, chart_state),
                        updated_at = NOW()
                    WHERE id = $5 AND user_id = $6 AND deleted_at IS NULL
                    """,
                    name.strip()[:180] if isinstance(name, str) and name.strip() else None,
                    json.dumps(chart_spec) if chart_spec is not None else None,
                    json.dumps(chart_config) if chart_config is not None else None,
                    json.dumps(chart_state) if chart_state is not None else None,
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
