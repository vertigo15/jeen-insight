-- ============================================================================
-- Jeen Insights: answer-level favorites
-- ============================================================================
-- A favorite points at one persisted conversation turn. The answer, findings,
-- result snapshot and chart remain in their existing tables; retention excludes
-- favorited turns and their conversations while this row exists.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_favorite_answers (
    user_id       VARCHAR(255) NOT NULL,
    turn_id       UUID NOT NULL
                  REFERENCES insights_conversation_sessions(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, turn_id)
);

COMMENT ON TABLE insights_favorite_answers IS
'Jeen Insights: per-user saved answers. Favorites protect their conversation and result artifact from automatic retention until removed.';

CREATE INDEX IF NOT EXISTS idx_insights_favorite_answers_user_recent
    ON insights_favorite_answers (user_id, created_at DESC, turn_id DESC);

CREATE INDEX IF NOT EXISTS idx_insights_favorite_answers_turn
    ON insights_favorite_answers (turn_id);
