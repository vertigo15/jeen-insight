-- ============================================================================
-- Jeen Insights: trigram index for the history_lookup route
-- ============================================================================
-- ConversationHistoryService.search_turns answers "did I ask about X last
-- week?" with
--
--     WHERE user_id = $1 AND source_key = $2
--       AND created_at >= $3 AND created_at < $4
--       AND natural_language_query ILIKE '%X%'
--     ORDER BY created_at DESC
--
-- The scoping + window + ordering is already served by
-- idx_insights_turns_user_source_recent (022). This revision makes the ILIKE
-- itself indexable for users with very large histories or open-ended windows.
--
-- pg_trgm is a *trusted* extension (PostgreSQL 13+), so a database owner can
-- create it without superuser. Where the role still cannot (managed offerings
-- that allow-list extensions), the index is skipped with a NOTICE and the query
-- stays correct — just a sequential filter over the user's windowed rows.
-- Idempotent.
-- ============================================================================

DO $$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE '026: pg_trgm unavailable (%); skipping idx_insights_turns_question_trgm',
                     SQLERRM;
        RETURN;
    END;

    CREATE INDEX IF NOT EXISTS idx_insights_turns_question_trgm
        ON insights_conversation_sessions
        USING gin (natural_language_query gin_trgm_ops);
END
$$;
