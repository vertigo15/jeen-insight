-- Clarification lifecycle states are neither successful data queries nor
-- execution failures. Keep operational dashboards aligned with 023.
CREATE OR REPLACE VIEW v_insights_conversation_threads AS
SELECT
    session_id,
    user_id,
    source_key,
    COUNT(*) AS query_count,
    MIN(created_at) AS started_at,
    MAX(created_at) AS last_activity_at,
    SUM(CASE WHEN execution_status = 'success' THEN 1 ELSE 0 END) AS successful_queries,
    SUM(
        CASE
            WHEN execution_status NOT IN (
                'success', 'clarification_pending', 'clarification_resolved'
            )
            THEN 1
            ELSE 0
        END
    ) AS failed_queries
FROM insights_conversation_sessions
GROUP BY session_id, user_id, source_key;

CREATE OR REPLACE VIEW v_insights_query_performance AS
SELECT
    source_key,
    llm_model,
    DATE_TRUNC('day', created_at) AS query_date,
    COUNT(*) AS query_count,
    AVG(llm_latency_ms) AS avg_llm_latency_ms,
    AVG(execution_time_ms) AS avg_execution_time_ms,
    AVG(tokens_used) AS avg_tokens_used,
    SUM(tokens_used) AS total_tokens_used,
    COUNT(*) FILTER (WHERE execution_status = 'success') AS success_count,
    COUNT(*) FILTER (
        WHERE execution_status NOT IN (
            'success', 'clarification_pending', 'clarification_resolved'
        )
    ) AS error_count
FROM insights_conversation_sessions
WHERE llm_latency_ms IS NOT NULL
GROUP BY source_key, llm_model, DATE_TRUNC('day', created_at);
