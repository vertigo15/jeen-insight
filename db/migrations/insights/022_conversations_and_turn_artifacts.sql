-- ============================================================================
-- Jeen Insights: user conversations + per-turn restore artifacts
-- ============================================================================
-- Introduces a conversation-level entity so the UI can reopen the user's last
-- conversation (per connection) with each turn's table and chart restored, and
-- browse previous conversations.
--
--   insights_conversations         one row per conversation; id == the
--                                  session_id already stored on every turn.
--   insights_turn_artifacts        answer / analytics / result snapshot /
--                                  chart baseline for one turn (keyed by the
--                                  turn row id, i.e. the query_id the UI holds).
--                                  Kept separate so the hot turn table stays
--                                  slim and dropping blobs is a plain UPDATE.
--   insights_conversation_prune_state
--                                  (user_id, source_key) -> last_pruned_at;
--                                  the atomic claim that throttles the
--                                  on-open retention prune across replicas.
--
-- Backfill: a legacy session_id was never connection-scoped, so one id can
-- hold threads for several source_keys. The thread with the most turns (tie:
-- earliest start) keeps the id; every other (session_id, user_id, source_key)
-- thread is re-keyed to a fresh UUID and gets its own conversation. Nothing is
-- dropped. parent_query_id links point at row ids, so they stay valid.
--
-- Order: tables -> remap -> conversations -> FK -> indexes. The migration
-- runner wraps the file in one transaction, so a failure leaves the schema
-- untouched.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_conversations (
    id               UUID PRIMARY KEY,
    user_id          VARCHAR(255) NOT NULL,
    source_key       VARCHAR(255) NOT NULL,
    source_label     TEXT NOT NULL,
    title            TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_insights_conversations_title_len CHECK (char_length(title) <= 200)
);

COMMENT ON TABLE insights_conversations IS
'Jeen Insights: one row per user conversation on one connection. id equals the session_id carried by every turn in insights_conversation_sessions.';

CREATE TABLE IF NOT EXISTS insights_turn_artifacts (
    turn_id          UUID PRIMARY KEY
                     REFERENCES insights_conversation_sessions(id) ON DELETE CASCADE,
    result_kind      VARCHAR(16) NOT NULL,
    answer           JSONB,
    error            TEXT,
    metrics          JSONB,
    findings         JSONB,
    suggestions      JSONB,
    followups        JSONB,
    result_snapshot  JSONB,
    snapshot_status  VARCHAR(16) NOT NULL,
    snapshot_bytes   INT,
    snapshot_at      TIMESTAMPTZ,
    chart_spec       JSONB,
    chart_config     JSONB,
    chart_bytes      INT,
    chart_updated_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_turn_artifacts_result_kind CHECK (
        result_kind IN ('table', 'text', 'error')
    ),
    CONSTRAINT chk_turn_artifacts_snapshot_status CHECK (
        snapshot_status IN ('stored', 'too_large', 'pruned', 'not_applicable')
    )
);

COMMENT ON TABLE insights_turn_artifacts IS
'Jeen Insights: per-turn restore payload (answer, inline analytics, full result snapshot, chart baseline). snapshot_status=pruned is terminal for normal writes; only an explicit rerun promotes it back to stored.';

CREATE TABLE IF NOT EXISTS insights_conversation_prune_state (
    user_id        VARCHAR(255) NOT NULL,
    source_key     VARCHAR(255) NOT NULL,
    last_pruned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, source_key)
);

COMMENT ON TABLE insights_conversation_prune_state IS
'Jeen Insights: per (user, connection) claim row used to throttle the on-open retention prune across API replicas.';

-- ── Backfill: re-key legacy cross-source threads ────────────────────────────
-- For each session_id that spans more than one (user_id, source_key), keep the
-- id on the largest thread and move every other thread to a fresh UUID.
DO $$
DECLARE
    remapped INT := 0;
BEGIN
    WITH threads AS (
        SELECT session_id, user_id, source_key,
               COUNT(*)        AS query_count,
               MIN(created_at) AS started_at
        FROM insights_conversation_sessions
        GROUP BY session_id, user_id, source_key
    ),
    ranked AS (
        SELECT t.*,
               ROW_NUMBER() OVER (
                   PARTITION BY session_id
                   ORDER BY query_count DESC, started_at ASC, user_id, source_key
               ) AS rn
        FROM threads t
    ),
    losers AS (
        SELECT session_id, user_id, source_key, gen_random_uuid() AS new_session_id
        FROM ranked
        WHERE rn > 1
    ),
    moved AS (
        UPDATE insights_conversation_sessions cs
        SET session_id = l.new_session_id
        FROM losers l
        WHERE cs.session_id = l.session_id
          AND cs.user_id    = l.user_id
          AND cs.source_key = l.source_key
        RETURNING 1
    )
    SELECT COUNT(*) INTO remapped FROM moved;

    IF remapped > 0 THEN
        RAISE NOTICE 'insights 022: re-keyed % turn row(s) belonging to cross-source legacy threads', remapped;
    END IF;
END $$;

-- ── Backfill: one conversation per (session_id, user_id, source_key) ────────
-- Title = the first question of the thread, capped to the column limit.
-- source_label falls back to source_key; the application refreshes it with the
-- connection's display name when it next touches the conversation.
INSERT INTO insights_conversations (id, user_id, source_key, source_label, title, created_at, last_activity_at)
SELECT
    t.session_id,
    t.user_id,
    t.source_key,
    t.source_key,
    LEFT(COALESCE(NULLIF(BTRIM(f.natural_language_query), ''), 'Conversation'), 200),
    t.started_at,
    t.last_activity_at
FROM (
    SELECT session_id, user_id, source_key,
           MIN(created_at) AS started_at,
           MAX(created_at) AS last_activity_at
    FROM insights_conversation_sessions
    GROUP BY session_id, user_id, source_key
) t
JOIN LATERAL (
    SELECT natural_language_query
    FROM insights_conversation_sessions cs
    WHERE cs.session_id = t.session_id
    ORDER BY cs.sequence_number ASC, cs.created_at ASC
    LIMIT 1
) f ON TRUE
ON CONFLICT (id) DO NOTHING;

-- ── Parent link enforced by the database ────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_insights_turn_conversation'
          AND conrelid = 'insights_conversation_sessions'::regclass
    ) THEN
        ALTER TABLE insights_conversation_sessions
            ADD CONSTRAINT fk_insights_turn_conversation
            FOREIGN KEY (session_id) REFERENCES insights_conversations(id)
            ON DELETE CASCADE;
    END IF;
END $$;

-- ── Indexes ─────────────────────────────────────────────────────────────────
-- Conversation list / "last conversation" per user + connection.
CREATE INDEX IF NOT EXISTS idx_insights_conversations_user_source_recent
    ON insights_conversations (user_id, source_key, last_activity_at DESC, id DESC);

-- Snapshot-ranking step of the retention prune (all turns of a user+connection
-- newest first). Detail paging keeps using (session_id, sequence_number).
CREATE INDEX IF NOT EXISTS idx_insights_turns_user_source_recent
    ON insights_conversation_sessions (user_id, source_key, created_at DESC, id DESC);

-- Blob-holding artifacts are the prune candidates; keep the scan cheap.
CREATE INDEX IF NOT EXISTS idx_insights_turn_artifacts_with_blobs
    ON insights_turn_artifacts (turn_id)
    WHERE result_snapshot IS NOT NULL OR chart_config IS NOT NULL;
