-- ============================================================================
-- Jeen Insights: encrypted, TTL-bound tool-result artifacts (Phase 5)
-- ============================================================================
-- Read/data tools (e.g. Tavily web search) return DATA that must be fed back to
-- the model to compose the final answer. That data is UNTRUSTED (external web
-- content) and must never be trusted as authorization. It is captured here as a
-- durable, envelope-encrypted, integrity-hashed artifact and later consumed
-- exactly once by a RESPONSE-ONLY continuation that re-enters the model with the
-- data fenced + size-capped and TOOLS DISABLED (no autonomous chaining).
--
-- Binding: owner_user_id + identity_id + proposal_id + session_id + connector
-- version. Single-consume (consumed_at) and TTL-bound (expires_at). The payload
-- hash detects tampering at decrypt time.
-- ============================================================================

CREATE TABLE IF NOT EXISTS connector_tool_results (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id          UUID        NOT NULL REFERENCES connector_action_proposals(id) ON DELETE CASCADE,
    owner_user_id        TEXT        NOT NULL,
    identity_id          UUID        REFERENCES connector_identities(id) ON DELETE SET NULL,
    session_id           TEXT,
    connector_version_id UUID        REFERENCES connector_versions(id) ON DELETE SET NULL,
    classification       TEXT        NOT NULL DEFAULT 'external',   -- untrusted web data
    integrity_hash       TEXT        NOT NULL,                      -- sha256 of canonical payload
    -- EncryptedBlob fields (encrypted tool-result payload)
    algo          TEXT        NOT NULL,
    kek_id        TEXT        NOT NULL,
    ciphertext    TEXT        NOT NULL,
    nonce         TEXT        NOT NULL,
    wrapped_dek   TEXT        NOT NULL,
    dek_nonce     TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    consumed_at   TIMESTAMPTZ,
    CONSTRAINT uq_tool_result_proposal UNIQUE (proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_results_owner  ON connector_tool_results(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_tool_results_expiry ON connector_tool_results(expires_at);
