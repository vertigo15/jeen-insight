-- ============================================================================
-- Jeen Insights: saved analyses
-- ============================================================================
-- Durable user-owned analysis snapshots. These are state-oriented records:
-- they restore table data, SQL, insights, and chart configuration without
-- requiring the original question to be rerun.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_saved_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    source_key VARCHAR(255) NOT NULL,
    connection_id VARCHAR(255),
    query_id UUID REFERENCES insights_conversation_sessions(id) ON DELETE SET NULL,

    name TEXT NOT NULL,
    question TEXT NOT NULL,
    generated_sql TEXT,

    columns JSONB NOT NULL DEFAULT '[]'::jsonb,
    row_count INT NOT NULL DEFAULT 0,
    result_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,

    chart_spec JSONB,
    chart_config JSONB,
    insights_payload JSONB,

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    deleted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_saved_analyses_user_source_recent
    ON insights_saved_analyses(user_id, source_key, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_saved_analyses_query
    ON insights_saved_analyses(query_id)
    WHERE query_id IS NOT NULL;

COMMENT ON TABLE insights_saved_analyses IS
'Jeen Insights: user-owned saved analysis snapshots for restoring table data, charts, SQL, and insights.';
