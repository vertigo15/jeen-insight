-- ============================================================================
-- Jeen Insights: immutable prompt-governance audit trail
-- ============================================================================
-- Prompt content and model overrides are versioned in insights_prompts. This
-- table records who performed each operator action so an administrator can
-- investigate, roll back, and demonstrate change control.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_prompt_audit (
    id              SERIAL PRIMARY KEY,
    prompt_place    VARCHAR(100) NOT NULL,
    action          VARCHAR(32)  NOT NULL,
    actor_user_id   TEXT,
    actor_email     TEXT,
    version         INTEGER      NOT NULL,
    details         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_insights_prompt_audit_place_created
    ON insights_prompt_audit(prompt_place, created_at DESC);

CREATE OR REPLACE FUNCTION prevent_insights_prompt_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'insights_prompt_audit is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_insights_prompt_audit_append_only
    ON insights_prompt_audit;
CREATE TRIGGER trg_insights_prompt_audit_append_only
    BEFORE UPDATE OR DELETE ON insights_prompt_audit
    FOR EACH ROW
    EXECUTE FUNCTION prevent_insights_prompt_audit_mutation();
