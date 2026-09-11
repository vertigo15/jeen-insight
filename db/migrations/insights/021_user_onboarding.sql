-- ============================================================================
-- Jeen Insights: per-user onboarding / first-time-user-experience state
-- ============================================================================
-- Tracks the once-per-user welcome dialog, guided tour, getting-started
-- checklist, and the post-first-answer nudge. Persisted server-side (per
-- account, not per browser) so "show once" holds across browsers/devices.
--
-- One row per user_id. `checklist` is a flat jsonb map of item -> bool
-- (e.g. {"pick_connection": true, "ask_first_question": true}). The checklist
-- card and the nudge are independently dismissible, so each has its own
-- *_dismissed_at column.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_user_onboarding (
    user_id                VARCHAR(255) PRIMARY KEY,
    welcome_seen_at        TIMESTAMPTZ,
    tour_completed_at      TIMESTAMPTZ,
    checklist              JSONB NOT NULL DEFAULT '{}'::jsonb,
    checklist_dismissed_at TIMESTAMPTZ,
    nudge_dismissed_at     TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE insights_user_onboarding IS
'Jeen Insights: per-user FTUE state (welcome dialog, guided tour, getting-started checklist, post-answer nudge). One row per user_id.';
