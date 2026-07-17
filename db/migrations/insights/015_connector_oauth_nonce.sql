-- ============================================================================
-- Jeen Insights: bind OAuth callbacks with an OIDC nonce
-- ============================================================================
-- Adds a plaintext `oidc_nonce` column to connector_oauth_sessions. The nonce is
-- generated at /authorize time, sent to the IdP in the authorize request, and
-- re-checked against the returned id_token `nonce` claim in the callback. This
-- defeats id_token replay / authorization-code injection. The nonce is NOT a
-- secret (it travels in the front-channel authorize URL), so it is stored in the
-- clear alongside the encrypted PKCE verifier.
-- ============================================================================

ALTER TABLE connector_oauth_sessions ADD COLUMN IF NOT EXISTS oidc_nonce TEXT;
