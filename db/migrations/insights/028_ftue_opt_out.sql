-- ============================================================================
-- Jeen Insights: permanent opt-out of the whole onboarding (FTUE) layer
-- ============================================================================
-- Set when the user ticks "Don't show this again" on the welcome dialog. It
-- suppresses EVERY FTUE surface (welcome dialog, guided tour, getting-started
-- checklist, quick-start cards, post-answer nudge) for that account.
--
-- This is deliberately its own column rather than being inferred from
-- welcome_seen_at + checklist_dismissed_at + nudge_dismissed_at: a user can reach
-- that combination through ordinary, independent dismissals, which must not
-- silently turn into a global preference. "Skip for now" is session-scoped and
-- lives client-side only; it never touches this table.
-- Idempotent.
-- ============================================================================

ALTER TABLE insights_user_onboarding
    ADD COLUMN IF NOT EXISTS ftue_opted_out_at TIMESTAMPTZ;

COMMENT ON COLUMN insights_user_onboarding.ftue_opted_out_at IS
'Set when the user permanently opts out of all onboarding surfaces ("Don''t show this again").';
