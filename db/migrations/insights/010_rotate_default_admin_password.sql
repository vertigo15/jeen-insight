-- ============================================================================
-- Jeen Insights: rotate the legacy default admin password
-- ============================================================================
-- Migration 008 originally seeded the admin account with the 4-char password
-- "admin", which violates the current >=8 character policy. This migration
-- upgrades that seed to the new documented default "ChangeMe123!" ONLY when the
-- admin row still carries the exact old default hash — i.e. the password was
-- never changed. If an operator has already set a custom password, the hash
-- won't match and this UPDATE is a no-op.
--
-- Safe and idempotent: after it runs once the stored hash differs from the old
-- default, so re-runs affect 0 rows. It never overwrites a real password.
--
-- Operators should STILL change "ChangeMe123!" after login — it is a shared,
-- publicly-documented bootstrap credential, not a secret.
-- ============================================================================

UPDATE auth_users
SET password_hash = '$2b$12$bSvlOGpdYkN44TzBs6dWx.N7Ia9LaTosYRGVQOOOzSU81QfBJlAjC'
WHERE email = 'admin'
  AND password_hash = '$2b$12$04PLmZ5VyTctiR6QODu5seF8uRExKLZkS/euG1Xg9cWEwXHwtbHYK';
