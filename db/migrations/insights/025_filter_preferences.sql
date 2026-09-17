-- ============================================================================
-- Jeen Insights: remembered filter-grounding choices
-- ============================================================================
-- When the grounder has to ask which field (or which exact value) a word of a
-- question meant, the user's answer is remembered per user and connection so
-- the same question is never asked twice:
--
--   * literal_norm + role_column   "mosco" → dim_dealer.city (or a value)
--   * ''           + role_column   role-level: for the "city" role this user
--                                  means dim_dealer.city — reused for "paris"
--   * literal_norm + ''            "any of these fields" for that literal
--
-- Preferences are advisory: the grounder re-checks governance (sensitive,
-- hidden, denylisted columns) before honouring one. Idempotent.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_filter_preferences (
    id            SERIAL PRIMARY KEY,
    user_id       VARCHAR(255) NOT NULL,
    source_key    VARCHAR(255) NOT NULL,
    literal_norm  VARCHAR(255) NOT NULL DEFAULT '',
    role_column   VARCHAR(255) NOT NULL DEFAULT '',
    table_name    VARCHAR(255),
    column_name   VARCHAR(255),
    any_of        BOOLEAN      NOT NULL DEFAULT FALSE,
    chosen_value  TEXT,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_insights_filter_pref UNIQUE (user_id, source_key, literal_norm, role_column)
);

CREATE INDEX IF NOT EXISTS idx_insights_filter_pref_user_source
    ON insights_filter_preferences(user_id, source_key);
