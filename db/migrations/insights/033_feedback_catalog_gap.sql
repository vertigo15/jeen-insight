-- Admit 'catalog_gap' as a feedback value.
--
-- The "Report catalog gap" action on a failed turn has always posted
-- feedback='catalog_gap', which chk_insights_user_feedback (001) rejected, so
-- the report was silently dropped (record_feedback swallows the error and
-- the route answered 404). Widening the CHECK makes it persist.
--
-- Shared-DB safety: the metadata DB is shared with the live Schema Modeler
-- and insights_conversation_sessions is a hot table. NOT VALID skips the full
-- table scan (every existing value is already in the old, narrower set, so
-- nothing can be in violation); the runner's session lock_timeout bounds the
-- brief ACCESS EXCLUSIVE lock the two ALTERs take.

ALTER TABLE insights_conversation_sessions
    DROP CONSTRAINT IF EXISTS chk_insights_user_feedback;

ALTER TABLE insights_conversation_sessions
    ADD CONSTRAINT chk_insights_user_feedback CHECK (
        user_feedback IS NULL
        OR user_feedback IN ('thumbs_up', 'thumbs_down', 'edited', 'catalog_gap')
    ) NOT VALID;

COMMENT ON COLUMN insights_conversation_sessions.user_feedback IS
'thumbs_up / thumbs_down / edited: result-quality feedback; catalog_gap: the user reported a failed turn as a catalog gap.';
