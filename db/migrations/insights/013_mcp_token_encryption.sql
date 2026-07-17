-- ============================================================================
-- Jeen Insights: encrypt the catalog MCP bearer token at rest
-- ============================================================================
-- Adds envelope-encryption columns to insights_mcp_servers so the catalog MCP
-- bearer token is no longer stored in plaintext. McpServerService writes the
-- encrypted blob (and nulls bearer_token) whenever a KEK is configured, and
-- reads transparently prefer the encrypted value. A one-time Python backfill in
-- scripts/run_insights_migrations.py migrates any existing plaintext tokens.
--
-- Backward compatible: when no KEK is configured the plaintext column is still
-- used so the catalog feature keeps working.
-- ============================================================================

ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS token_algo        TEXT;
ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS token_kek_id      TEXT;
ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS token_ciphertext  TEXT;
ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS token_nonce       TEXT;
ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS token_wrapped_dek TEXT;
ALTER TABLE insights_mcp_servers ADD COLUMN IF NOT EXISTS token_dek_nonce   TEXT;
