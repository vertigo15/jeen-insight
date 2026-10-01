-- Durable, admin-only execution details for Analytics run inspection.
--
-- This table deliberately has no foreign key to insights_conversation_sessions:
-- conversation retention may remove a turn before the shorter analytics
-- retention window expires. The writer accepts only the final user-visible
-- question/answer, generated SQL or DAX, bounded error text, scalar metrics,
-- and the slim timing trace. Prompts and result rows are never written here.

CREATE TABLE IF NOT EXISTS insights_execution_run_details (
    id              BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_id        UUID         NOT NULL,
    occurred_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    user_id         VARCHAR(255) NOT NULL,
    source_key      VARCHAR(255),
    session_id      UUID,
    outcome         VARCHAR(16)  NOT NULL,
    route           VARCHAR(40),
    skill           VARCHAR(64),
    query_language  VARCHAR(8),
    question        TEXT         NOT NULL,
    answer          JSONB,
    generated_query TEXT,
    error           TEXT,
    metrics         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    trace           JSONB        NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT uq_insights_execution_run_details_query UNIQUE (query_id),
    CONSTRAINT chk_insights_execution_run_details_outcome CHECK (
        outcome IN ('success', 'error', 'refused')
    ),
    CONSTRAINT chk_insights_execution_run_details_language CHECK (
        query_language IS NULL OR query_language IN ('sql', 'dax')
    ),
    CONSTRAINT chk_insights_execution_run_details_error_len CHECK (
        error IS NULL OR char_length(error) <= 4000
    ),
    CONSTRAINT chk_insights_execution_run_details_metrics_object CHECK (
        jsonb_typeof(metrics) = 'object'
    ),
    CONSTRAINT chk_insights_execution_run_details_trace_array CHECK (
        jsonb_typeof(trace) = 'array'
    )
);

-- Newest-first keyset feed, with selective filters applied before paging.
CREATE INDEX IF NOT EXISTS idx_insights_execution_run_details_feed
    ON insights_execution_run_details (occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_insights_execution_run_details_outcome_feed
    ON insights_execution_run_details (outcome, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_insights_execution_run_details_source_feed
    ON insights_execution_run_details (source_key, occurred_at DESC, id DESC);

-- Seed details for retained turns so the Analytics view is useful immediately
-- after rollout. Build metrics explicitly instead of copying the artifact's
-- metrics blob: older artifacts may contain nested memory/filter diagnostics.
INSERT INTO insights_execution_run_details (
    query_id, occurred_at, user_id, source_key, session_id, outcome, route,
    skill, query_language, question, answer, generated_query, error, metrics,
    trace
)
SELECT
    cs.id,
    cs.created_at AT TIME ZONE 'UTC',
    cs.user_id,
    cs.source_key,
    cs.session_id,
    COALESCE(
        ue.outcome,
        CASE WHEN cs.execution_status = 'success' THEN 'success' ELSE 'error' END
    ),
    ue.route,
    ue.skill,
    CASE
        WHEN LOWER(COALESCE(ta.metrics->>'database_type', '')) IN ('powerbi', 'power-bi', 'dax')
             OR cs.generated_sql ~* '^[[:space:]]*EVALUATE([[:space:]]|$)'
            THEN 'dax'
        WHEN cs.generated_sql IS NOT NULL AND cs.generated_sql <> ''
            THEN 'sql'
        ELSE NULL
    END,
    cs.natural_language_query,
    ta.answer,
    cs.generated_sql,
    LEFT(COALESCE(ta.error, cs.error_message), 4000),
    jsonb_strip_nulls(jsonb_build_object(
        'total_tokens', cs.tokens_used,
        'llm_latency_ms', cs.llm_latency_ms,
        'execution_time_ms', cs.execution_time_ms,
        'graph_time_ms', cs.graph_time_ms,
        'row_count', cs.row_count
    )),
    COALESCE(cs.node_trace, '[]'::jsonb)
FROM insights_conversation_sessions cs
LEFT JOIN insights_turn_artifacts ta ON ta.turn_id = cs.id
LEFT JOIN insights_usage_events ue
    ON ue.event_type = 'query' AND ue.query_id = cs.id
WHERE cs.execution_status IS DISTINCT FROM 'pending'
ON CONFLICT (query_id) DO NOTHING;

COMMENT ON TABLE insights_execution_run_details IS
'Jeen Insights: retained final execution details for the admin Analytics runs view. No FK to conversation turns. Contains no prompts or result rows.';
COMMENT ON COLUMN insights_execution_run_details.query_id IS
'Original turn id; unique for idempotent final-detail writes and intentionally not a foreign key.';
COMMENT ON COLUMN insights_execution_run_details.question IS
'Full user question. Unlike the aggregate usage ledger excerpt, this is not truncated.';
COMMENT ON COLUMN insights_execution_run_details.answer IS
'Final user-visible answer as JSON (string or fragment array); never the result row payload.';
COMMENT ON COLUMN insights_execution_run_details.generated_query IS
'Final generated SQL or DAX, identified by query_language.';
COMMENT ON COLUMN insights_execution_run_details.error IS
'Final user-visible execution error, bounded to 4000 characters.';
COMMENT ON COLUMN insights_execution_run_details.metrics IS
'Allowlisted scalar execution metrics only; no memory payloads, prompts, or result values.';
COMMENT ON COLUMN insights_execution_run_details.trace IS
'Complete slim trace: ordered node/type/elapsed_ms entries only, with prompts and detail prose removed.';
