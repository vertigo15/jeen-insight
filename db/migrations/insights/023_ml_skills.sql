-- ============================================================================
-- Jeen Insights: ML skills — proposals, per-skill consent, analysis artifacts
-- ============================================================================
-- Adds the persistence the ML-skills feature needs so a confirm card, a
-- clarification or a guard refusal can be resumed server-side, a user's
-- first-run consent is remembered per skill and connection, and an ML turn
-- restores with its method details and low-confidence flag intact.
--
--   insights_analysis_proposals   one row per pending proposal (confirm /
--                                 clarify / guard). Owner-bound, expiring,
--                                 consumed at most once. The browser never
--                                 supplies authoritative {skill, params}; it
--                                 posts the proposal id plus an allowlisted
--                                 parameter patch.
--   insights_user_skill_prefs     (user, connection, skill, contract_version)
--                                 -> confirmed_at. "Don't ask again" consent,
--                                 scoped to the contract version so a changed
--                                 contract re-prompts.
--   insights_turn_artifacts       + analysis JSONB (method, params, validation,
--                                 guard results, provenance — never rows) and
--                                 + low_confidence so the flag survives restore,
--                                 pinning and export.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_analysis_proposals (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           VARCHAR(255) NOT NULL,
    source_key        VARCHAR(255) NOT NULL,
    session_id        UUID,
    parent_query_id   UUID,
    kind              VARCHAR(16)  NOT NULL DEFAULT 'confirm',
    skill             VARCHAR(64)  NOT NULL,
    params            JSONB        NOT NULL,
    contract_version  VARCHAR(16)  NOT NULL,
    question          TEXT,
    proposal          JSONB,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ  NOT NULL,
    consumed_at       TIMESTAMPTZ,
    consumed_query_id UUID,
    idempotency_key   TEXT,
    CONSTRAINT chk_analysis_proposal_kind CHECK (
        kind IN ('confirm', 'clarify', 'guard', 'rerun')
    )
);

COMMENT ON TABLE insights_analysis_proposals IS
'Jeen Insights: pending ML-skill proposals (confirm card / clarification / guard refusal). Owner-bound, expiring, consumed once; the only server-trusted source of {skill, params} for /api/analysis/run.';

CREATE INDEX IF NOT EXISTS idx_insights_analysis_proposals_owner_recent
    ON insights_analysis_proposals (user_id, source_key, created_at DESC);

-- One execution per (user, connection, idempotency key): a retried /run returns
-- the first turn. Scoped so one user's key can never block another's request.
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_analysis_proposals_idempotency
    ON insights_analysis_proposals (user_id, source_key, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS insights_user_skill_prefs (
    user_id          VARCHAR(255) NOT NULL,
    source_key       VARCHAR(255) NOT NULL,
    skill            VARCHAR(64)  NOT NULL,
    contract_version VARCHAR(16)  NOT NULL,
    confirmed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, source_key, skill, contract_version)
);

COMMENT ON TABLE insights_user_skill_prefs IS
'Jeen Insights: per (user, connection, skill, contract_version) first-run consent for ML skills ("don''t ask again").';

-- Additive columns on the turn artifact. Guarded so the migration is valid on
-- a database where 022 has not been applied yet.
DO $$
BEGIN
    IF to_regclass('insights_turn_artifacts') IS NOT NULL THEN
        ALTER TABLE insights_turn_artifacts
            ADD COLUMN IF NOT EXISTS analysis JSONB;
        ALTER TABLE insights_turn_artifacts
            ADD COLUMN IF NOT EXISTS low_confidence BOOLEAN NOT NULL DEFAULT false;
    END IF;
END $$;
