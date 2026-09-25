-- Durable usage ledger for the admin Analytics page.
--
-- Conversation retention (CONVERSATION_KEEP_LAST, per user + connection) deletes
-- old conversations and cascades into insights_conversation_sessions and
-- insights_answer_feedback, so those tables cannot answer "how many active
-- users did we have last month" or "what did people complain about in March".
-- This table is the durable copy: one row per event, NO foreign key to the turn,
-- so it survives retention. Rows are immutable until they expire
-- (USAGE_EVENTS_RETENTION_DAYS, pruned in batches by the API at startup and
-- then daily).
--
-- Event types
--   login     a successful sign-in (written by the Flask UI process)
--   query     one completed turn (written once by save_to_memory; outcome,
--             route, skill, latency, tokens, a 500-char question excerpt)
--   feedback  one insights_answer_feedback event copied here (thumb / rating /
--             type / message) together with the question it was about
--   analysis  one ML-skill run (ok | guard_failed | error)
--
-- Idempotency: a turn produces exactly one query event and at most one
-- analysis event; a feedback row is copied at most once. The partial UNIQUE
-- indexes below back the writers' ON CONFLICT DO NOTHING, so a retried graph
-- run or a re-applied backfill never double-counts.
--
-- Privacy: question excerpts and free-text feedback messages live here for the
-- ledger's retention window, which is longer than conversation retention.
-- `detail` carries only allowlisted, non-sensitive keys (method names, guard
-- names, error categories) — never row values, SQL or message bodies. Reads
-- are admin-only and audited (connector_audit event_type='analytics.read').
--
-- Insights-owned objects only (the metadata DB is shared with the Schema
-- Modeler); no Schema Modeler table is touched.

CREATE TABLE IF NOT EXISTS insights_usage_events (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type        VARCHAR(16)  NOT NULL,
    occurred_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    user_id           VARCHAR(255) NOT NULL,
    source_key        VARCHAR(255),
    query_id          UUID,
    session_id        UUID,

    -- query / analysis
    outcome           VARCHAR(16),
    error_type        VARCHAR(64),
    route             VARCHAR(40),
    skill             VARCHAR(64),
    llm_model         VARCHAR(128),
    total_tokens      INT,
    input_tokens      INT,
    llm_latency_ms    INT,
    execution_time_ms INT,
    graph_time_ms     INT,
    row_count         INT,

    -- feedback
    feedback_id       UUID,
    thumb             VARCHAR(12),
    rating            SMALLINT,
    feedback_type     VARCHAR(20),
    message           TEXT,

    question          TEXT,
    detail            JSONB,

    CONSTRAINT chk_insights_usage_events_type CHECK (
        event_type IN ('login', 'query', 'feedback', 'analysis')
    ),
    CONSTRAINT chk_insights_usage_events_outcome CHECK (
        outcome IS NULL OR outcome IN ('success', 'error', 'refused')
    ),
    CONSTRAINT chk_insights_usage_events_thumb CHECK (
        thumb IS NULL OR thumb IN ('thumbs_up', 'thumbs_down', 'cleared')
    ),
    CONSTRAINT chk_insights_usage_events_rating CHECK (
        rating IS NULL OR rating BETWEEN 1 AND 5
    ),
    CONSTRAINT chk_insights_usage_events_feedback_type CHECK (
        feedback_type IS NULL
        OR feedback_type IN ('general', 'report_bug', 'ui_bug', 'other')
    ),
    CONSTRAINT chk_insights_usage_events_message_len CHECK (
        message IS NULL OR char_length(message) <= 4000
    ),
    CONSTRAINT chk_insights_usage_events_question_len CHECK (
        question IS NULL OR char_length(question) <= 500
    )
);

-- Period scans (KPIs, time series, prune).
CREATE INDEX IF NOT EXISTS idx_insights_usage_events_time
    ON insights_usage_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_insights_usage_events_type_time
    ON insights_usage_events (event_type, occurred_at DESC);
-- Top users / top connections.
CREATE INDEX IF NOT EXISTS idx_insights_usage_events_user_time
    ON insights_usage_events (user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_insights_usage_events_source_time
    ON insights_usage_events (source_key, occurred_at DESC);
-- Feedback feed keyset pagination (ORDER BY id DESC ... WHERE id < $before).
CREATE INDEX IF NOT EXISTS idx_insights_usage_events_feedback_feed
    ON insights_usage_events (id DESC) WHERE event_type = 'feedback';
-- Current-thumb derivation and "feedback on this turn" joins.
CREATE INDEX IF NOT EXISTS idx_insights_usage_events_feedback_turn
    ON insights_usage_events (query_id, id DESC) WHERE event_type = 'feedback';

-- Idempotency guards (see header).
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_usage_events_query
    ON insights_usage_events (query_id) WHERE event_type = 'query';
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_usage_events_analysis
    ON insights_usage_events (query_id) WHERE event_type = 'analysis';
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_usage_events_feedback
    ON insights_usage_events (feedback_id) WHERE event_type = 'feedback';

-- One-time backfill from what retention has not deleted yet, so the page is
-- not empty on the day it ships. insights_conversation_sessions.created_at is
-- a naive TIMESTAMP written with NOW() by an API running in UTC; interpret it
-- as UTC explicitly instead of trusting the migration session's time zone.
-- Idempotent through the unique guards.
--
-- graph_time_ms is a baseline column (src/metadata/insights_schema.py) rather
-- than a numbered migration; on a database bootstrapped from the SQL files
-- alone it may not exist yet, so make sure it does before selecting it.
ALTER TABLE insights_conversation_sessions ADD COLUMN IF NOT EXISTS graph_time_ms INT;

INSERT INTO insights_usage_events (
    event_type, occurred_at, user_id, source_key, query_id, session_id,
    outcome, error_type, route, llm_model, total_tokens, llm_latency_ms,
    execution_time_ms, graph_time_ms, row_count, question
)
SELECT
    'query',
    cs.created_at AT TIME ZONE 'UTC',
    cs.user_id,
    cs.source_key,
    cs.id,
    cs.session_id,
    CASE WHEN cs.execution_status = 'success' THEN 'success' ELSE 'error' END,
    CASE WHEN cs.execution_status = 'success' THEN NULL ELSE cs.execution_status END,
    CASE WHEN cs.generated_sql IS NULL OR cs.generated_sql = '' THEN 'text' ELSE 'sql' END,
    cs.llm_model,
    cs.tokens_used,
    cs.llm_latency_ms,
    cs.execution_time_ms,
    cs.graph_time_ms,
    cs.row_count,
    LEFT(cs.natural_language_query, 500)
FROM insights_conversation_sessions cs
WHERE cs.execution_status IS DISTINCT FROM 'pending'
ON CONFLICT DO NOTHING;

INSERT INTO insights_usage_events (
    event_type, occurred_at, user_id, source_key, query_id, session_id,
    feedback_id, thumb, rating, feedback_type, message, question
)
SELECT
    'feedback',
    f.created_at,
    f.user_id,
    COALESCE(f.source_key, cs.source_key),
    f.query_id,
    cs.session_id,
    f.id,
    f.thumb,
    f.rating,
    f.feedback_type,
    f.message,
    LEFT(cs.natural_language_query, 500)
FROM insights_answer_feedback f
LEFT JOIN insights_conversation_sessions cs ON cs.id = f.query_id
ON CONFLICT DO NOTHING;

COMMENT ON TABLE insights_usage_events IS
'Jeen Insights: durable, append-only usage ledger for the admin Analytics page (logins, completed turns, feedback events, ML runs). No FK to turns so it survives conversation retention; rows expire after USAGE_EVENTS_RETENTION_DAYS. Question excerpts and feedback messages are kept for that window; detail holds allowlisted keys only.';
COMMENT ON COLUMN insights_usage_events.event_type IS
'login | query | feedback | analysis.';
COMMENT ON COLUMN insights_usage_events.user_id IS
'The signed-in principal id as a string (auth_users.id::text for local accounts). Join to auth_users is best-effort; deleted or external ids stay as raw strings.';
COMMENT ON COLUMN insights_usage_events.query_id IS
'The turn the event belongs to (insights_conversation_sessions.id at the time). Intentionally NOT a foreign key.';
COMMENT ON COLUMN insights_usage_events.outcome IS
'query/analysis: success | error | refused (an ML guard declined to run).';
COMMENT ON COLUMN insights_usage_events.route IS
'query: the planner route (sql, needs_analysis, greeting, capability, clarification, memory, ...). Text-only routes are not counted as questions.';
COMMENT ON COLUMN insights_usage_events.feedback_id IS
'feedback: insights_answer_feedback.id this row mirrors (unique per feedback event).';
COMMENT ON COLUMN insights_usage_events.thumb IS
'feedback: thumbs_up | thumbs_down | cleared. The CURRENT thumb of a turn is its newest feedback event with a non-NULL thumb; cleared means no thumb.';
COMMENT ON COLUMN insights_usage_events.question IS
'First 500 characters of the natural-language question, copied so feedback stays readable after the turn is pruned.';
COMMENT ON COLUMN insights_usage_events.detail IS
'Allowlisted, non-sensitive extras only: method, refused_by (guard names), connector_error_type, low_confidence.';
