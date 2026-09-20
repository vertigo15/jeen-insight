-- ============================================================================
-- Jeen Insights: per-account interface language
-- ============================================================================
-- `locale` is a BCP 47 language tag ('en', 'he', later 'es', 'ar', ...). NULL
-- means the user has not chosen one yet, so the UI falls back to the `locale`
-- cookie, then Accept-Language, then DEFAULT_LOCALE (see src/i18n).
--
-- Validation happens against the shipped locale registry in application code
-- (src/i18n/__init__.py LOCALES), not a CHECK constraint, so adding a language
-- needs no migration. Mirrored into a readable `locale` cookie by the Flask UI
-- so a fresh tab renders the right language/direction on first paint.
-- Idempotent.
-- ============================================================================

ALTER TABLE auth_users
    ADD COLUMN IF NOT EXISTS locale TEXT;
