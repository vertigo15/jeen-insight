-- ============================================================================
-- Jeen Insights: per-account result date format
-- ============================================================================
-- `date_format` controls calendar-date presentation in result tables:
--   auto  - derive day/month order from the interface formatting locale
--   dmy   - DD/MM/YYYY
--   mdy   - MM/DD/YYYY
--   iso   - YYYY-MM-DD
--
-- NULL means `auto`. Validation remains in application code so new display
-- formats can be introduced without changing the database constraint.
-- Idempotent.
-- ============================================================================

ALTER TABLE auth_users
    ADD COLUMN IF NOT EXISTS date_format TEXT;
