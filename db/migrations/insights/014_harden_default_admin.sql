-- ============================================================================
-- Jeen Insights: remove the seeded default-admin credential
-- ============================================================================
-- Migrations 008/010 shipped a publicly-documented default admin
-- (email 'admin', password 'ChangeMe123!'). That is a shared, known credential.
-- This migration deletes that seed ONLY when it was never customized (the stored
-- hash still equals the documented default). If an operator changed the password
-- the hash won't match and the row is preserved.
--
-- After removal, if no usable admin remains the Flask UI shows a first-run admin
-- setup screen (see /setup) instead of letting anyone log in with the shared
-- credential. Idempotent: re-runs affect 0 rows.
-- ============================================================================

DELETE FROM auth_users
 WHERE email = 'admin'
   AND password_hash = '$2b$12$bSvlOGpdYkN44TzBs6dWx.N7Ia9LaTosYRGVQOOOzSU81QfBJlAjC';
