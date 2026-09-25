-- Answer-quality feedback, append-only.
--
-- Every feedback event a user gives on an answer is one row: a thumbs up /
-- down click from the answer card, or a submission of the "Give feedback"
-- dialog (star rating, feedback type, free-text message). Rows are never
-- updated; the CURRENT thumb for a turn is the row with the highest
-- event_seq that carries a thumb value (thumb = 'cleared' means withdrawn).
-- event_seq, not created_at, is the order: NOW() can tie inside one
-- transaction and UUIDs sort randomly.
--
-- This replaces writing thumbs into insights_conversation_sessions.user_feedback.
-- Historical thumbs are copied in below so nothing disappears on upgrade. The
-- column stays for the two values that are not answer-quality signals:
-- 'edited' (set when a turn's question is edited and rerun) and 'catalog_gap'
-- (the "Report catalog gap" action on a failed turn) — see migration 033.
--
-- Rows are owned by the turn: deleting a conversation cascades here.
-- Insights-owned objects only (the metadata DB is shared with the Schema
-- Modeler); no Schema Modeler table is touched.

CREATE TABLE IF NOT EXISTS insights_answer_feedback (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_seq      BIGINT       GENERATED ALWAYS AS IDENTITY,
    query_id       UUID         NOT NULL
                                REFERENCES insights_conversation_sessions(id) ON DELETE CASCADE,
    user_id        VARCHAR(255) NOT NULL,
    source_key     VARCHAR(255),
    thumb          VARCHAR(12),
    rating         SMALLINT,
    feedback_type  VARCHAR(20),
    message        TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_insights_answer_feedback_thumb CHECK (
        thumb IS NULL OR thumb IN ('thumbs_up', 'thumbs_down', 'cleared')
    ),
    CONSTRAINT chk_insights_answer_feedback_rating CHECK (
        rating IS NULL OR rating BETWEEN 1 AND 5
    ),
    CONSTRAINT chk_insights_answer_feedback_type CHECK (
        feedback_type IS NULL
        OR feedback_type IN ('general', 'report_bug', 'ui_bug', 'other')
    ),
    CONSTRAINT chk_insights_answer_feedback_message_len CHECK (
        message IS NULL OR char_length(message) <= 4000
    ),
    -- An event must say something: a thumb, a rating or a message.
    CONSTRAINT chk_insights_answer_feedback_not_empty CHECK (
        thumb IS NOT NULL OR rating IS NOT NULL OR (message IS NOT NULL AND message <> '')
    )
);

-- "Latest thumb for this turn" and "all feedback for this turn", newest first.
CREATE INDEX IF NOT EXISTS idx_insights_answer_feedback_turn
    ON insights_answer_feedback (query_id, event_seq DESC);

-- Reporting: thumbs-down rate / average rating per user and period.
CREATE INDEX IF NOT EXISTS idx_insights_answer_feedback_owner
    ON insights_answer_feedback (user_id, created_at DESC);

-- Carry over thumbs recorded before this table existed, so an upgrade does not
-- un-press anything. The feedback time is unknown; the turn's created_at is
-- used. Idempotent: skipped for turns that already have an event. The legacy
-- partial index idx_insights_user_feedback (001) makes the scan cheap.
INSERT INTO insights_answer_feedback (query_id, user_id, source_key, thumb, created_at)
SELECT cs.id, cs.user_id, cs.source_key, cs.user_feedback, cs.created_at
FROM insights_conversation_sessions cs
WHERE cs.user_feedback IN ('thumbs_up', 'thumbs_down')
  AND NOT EXISTS (
      SELECT 1 FROM insights_answer_feedback f WHERE f.query_id = cs.id
  );

COMMENT ON TABLE insights_answer_feedback IS
'Jeen Insights: append-only log of user feedback on answers. One row per event: a thumbs click on the answer card, or a submission of the Give feedback dialog. The current thumb of a turn is its row with the highest event_seq and a non-NULL thumb (cleared = withdrawn). Answer-quality feedback lives here; insights_conversation_sessions.user_feedback keeps only edited / catalog_gap.';
COMMENT ON COLUMN insights_answer_feedback.event_seq IS
'Monotonic event order (identity). Use this, not created_at, to find the newest event for a turn.';
COMMENT ON COLUMN insights_answer_feedback.query_id IS
'The turn (insights_conversation_sessions.id) the feedback is about. Cascades on turn delete.';
COMMENT ON COLUMN insights_answer_feedback.user_id IS
'The signed-in user who gave the feedback (always the turn owner; the API refuses other users).';
COMMENT ON COLUMN insights_answer_feedback.source_key IS
'Connection the answer was produced on, copied from the turn row at feedback time (never taken from the client) so per-connection quality can be reported without joining the turn.';
COMMENT ON COLUMN insights_answer_feedback.thumb IS
'Quick signal from the answer card: thumbs_up | thumbs_down | cleared (the user clicked the pressed thumb again to withdraw it). NULL when the event is a dialog submission without a thumb.';
COMMENT ON COLUMN insights_answer_feedback.rating IS
'Star rating 1-5 from the Give feedback dialog ("Rate this answer"). NULL for thumb-only events.';
COMMENT ON COLUMN insights_answer_feedback.feedback_type IS
'Category chosen in the Give feedback dialog: general | report_bug | ui_bug | other. NULL for thumb-only events.';
COMMENT ON COLUMN insights_answer_feedback.message IS
'Free-text message from the Give feedback dialog, at most 4000 characters. NULL when the user left it empty.';
COMMENT ON COLUMN insights_answer_feedback.created_at IS
'When the feedback was given (server clock).';
