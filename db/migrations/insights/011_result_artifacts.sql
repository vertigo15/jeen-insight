-- ============================================================================
-- Jeen Insights: durable result artifacts + follow-up linkage
-- ============================================================================
-- Adds a compact, durable per-query result artifact (columns, types, row_count,
-- lightweight stats, freshness) so follow-up questions can be answered against a
-- known prior result instead of guessing from a tiny text preview. Complements
-- `result_preview` (a few sample rows) with structured metadata the router can
-- reason over.
--
-- `parent_query_id` already exists (001) but was never populated; the service
-- now links each turn to the previous one so a conversation forms a chain.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

ALTER TABLE insights_conversation_sessions
    ADD COLUMN IF NOT EXISTS result_artifact JSONB;

COMMENT ON COLUMN insights_conversation_sessions.result_artifact IS
'Compact structured summary of the executed result set: {columns, column_types, row_count, stats, sql, created_at}. Used to detect and answer follow-up questions about a prior result.';

-- Speeds up "most recent artifact in this session" lookups used by the router
-- manifest / follow-up detection.
CREATE INDEX IF NOT EXISTS idx_insights_session_artifact
    ON insights_conversation_sessions(session_id, sequence_number DESC)
    WHERE result_artifact IS NOT NULL;
