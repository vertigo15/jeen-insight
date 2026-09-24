-- Preserve page-local smart-chart state in explicitly saved analyses.
-- Conversation chart baselines remain semantic and session edits remain
-- non-durable unless the user explicitly saves the analysis.

ALTER TABLE insights_saved_analyses
    ADD COLUMN IF NOT EXISTS chart_state JSONB;

COMMENT ON COLUMN insights_saved_analyses.chart_state IS
'Versioned smart-chart session snapshot (baseline, working state, toggles, and derived overlays) for explicit saved-analysis restore.';
