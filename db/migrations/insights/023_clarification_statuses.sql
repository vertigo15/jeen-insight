-- Pending clarification turns are durable conversation state, not failures.
-- Extend the original lifecycle constraint before application code can persist
-- these statuses.
ALTER TABLE insights_conversation_sessions
    DROP CONSTRAINT IF EXISTS chk_insights_execution_status;

ALTER TABLE insights_conversation_sessions
    ADD CONSTRAINT chk_insights_execution_status CHECK (
        execution_status IN (
            'success',
            'error',
            'timeout',
            'syntax_error',
            'pending',
            'clarification_pending',
            'clarification_resolved'
        )
    );

DROP INDEX IF EXISTS idx_insights_failed_queries;
CREATE INDEX IF NOT EXISTS idx_insights_failed_queries
    ON insights_conversation_sessions(execution_status)
    WHERE execution_status NOT IN ('success', 'clarification_pending', 'clarification_resolved');
