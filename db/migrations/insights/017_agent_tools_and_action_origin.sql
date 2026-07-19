-- ============================================================================
-- Jeen Insights: agent-tools master switch + action-proposal origin
-- ============================================================================
-- Phase 1 of the agent tool-calling work (security contract + identity + flag):
--
--   * app_settings.agent_tools_enabled — an INDEPENDENT master switch controlling
--     whether the AGENT may autonomously propose/execute connector tools. It is
--     distinct from connectors_enabled (which governs manual user actions); agent
--     tool-calling requires BOTH to be true. Ships OFF.
--
--   * connector_action_proposals.origin — records whether a proposal was initiated
--     by a 'user' (the manual Integrations flow) or the 'agent'. The action gate
--     enforces the agent-tools switch (read fresh) for agent-origin proposals and
--     records the origin in the audit trail.
--
-- Server-enforced approval reuses existing columns (params, confirmation_hash) and
-- the existing 'confirmed' status from migration 012 — no schema change needed for
-- the preview->confirm->execute transition. Result snapshots are already nullable
-- (migration 012), so actions that don't act on a result need no change.
-- ============================================================================

-- ── Independent agent-tools switch (default OFF) ────────────────────────────
INSERT INTO app_settings (key, value, updated_at)
     VALUES ('agent_tools_enabled', 'false', NOW())
ON CONFLICT (key) DO NOTHING;

-- ── Action proposal origin (user | agent) ───────────────────────────────────
ALTER TABLE connector_action_proposals
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'user';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'connector_action_proposals_origin_check'
    ) THEN
        ALTER TABLE connector_action_proposals
            ADD CONSTRAINT connector_action_proposals_origin_check
            CHECK (origin IN ('user', 'agent'));
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_proposals_origin
    ON connector_action_proposals(origin);
