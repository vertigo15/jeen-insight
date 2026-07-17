-- ============================================================================
-- Jeen Insights: Per-User Connector / Integration Platform (Phase 0 + 1 + core)
-- ============================================================================
-- Introduces the destination-connector domain: canonical Entra identities,
-- group authorization, curated native connectors with IMMUTABLE version rows,
-- envelope-encrypted secret stores (connector client secret vs grant-bound
-- user token material), per-user grants + OAuth sessions, durable result
-- snapshots, the server-authorized action gate, and an append-only audit log.
--
-- The whole feature ships OFF: app_settings.connectors_enabled defaults false.
--
-- Requires PostgreSQL 13+ (gen_random_uuid() in core). Applied once by the
-- tracked migration runner (scripts/run_insights_migrations.py).
-- ============================================================================

-- ── Global master switch (default OFF) ──────────────────────────────────────
INSERT INTO app_settings (key, value, updated_at)
     VALUES ('connectors_enabled', 'false', NOW())
ON CONFLICT (key) DO NOTHING;

-- ── Canonical immutable identity (Phase 1) ──────────────────────────────────
-- Bound to (tenant_id, object_id) from Entra — never to a mutable email.
CREATE TABLE IF NOT EXISTS connector_identities (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     TEXT        NOT NULL,
    object_id     TEXT        NOT NULL,
    upn           TEXT,
    display_name  TEXT,
    -- Safe account-linking: at most one identity per local auth_users row.
    auth_user_id  INTEGER     UNIQUE REFERENCES auth_users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_identity_tenant_object UNIQUE (tenant_id, object_id)
);
CREATE INDEX IF NOT EXISTS idx_connector_identities_auth_user
    ON connector_identities(auth_user_id);

-- ── Group directory (read-only cache of Entra groups) ───────────────────────
CREATE TABLE IF NOT EXISTS connector_group_dir (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     TEXT        NOT NULL,
    object_id     TEXT        NOT NULL,  -- Entra group object id
    display_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_group_tenant_object UNIQUE (tenant_id, object_id)
);

-- ── Membership cache (identity -> group). Never manually mutated. ───────────
CREATE TABLE IF NOT EXISTS connector_identity_groups (
    identity_id   UUID        NOT NULL REFERENCES connector_identities(id) ON DELETE CASCADE,
    group_id      UUID        NOT NULL REFERENCES connector_group_dir(id) ON DELETE CASCADE,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (identity_id, group_id)
);
-- Freshness marker per identity so authz can fail-closed on stale/unknown data.
CREATE TABLE IF NOT EXISTS connector_membership_sync (
    identity_id   UUID PRIMARY KEY REFERENCES connector_identities(id) ON DELETE CASCADE,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source        TEXT NOT NULL DEFAULT 'graph',   -- 'graph' | 'token' | 'overage'
    complete      BOOLEAN NOT NULL DEFAULT TRUE     -- false when truncated/overage
);

-- ── group -> role mapping (authorization product; uses group object ids) ────
CREATE TABLE IF NOT EXISTS connector_group_roles (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT        NOT NULL,
    group_object_id  TEXT        NOT NULL,
    role             VARCHAR(20) NOT NULL CHECK (role IN ('admin','editor','viewer')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by       TEXT,
    CONSTRAINT uq_group_role UNIQUE (tenant_id, group_object_id)
);

-- ── Connectors (curated native destinations; v1 = fixed providers) ──────────
-- NOTE: no remote URL / arbitrary manifest columns. The provider adapter owns
-- endpoints, scopes and action schemas server-side; admins only enable + scope.
CREATE TABLE IF NOT EXISTS connectors (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key           TEXT        NOT NULL UNIQUE,   -- e.g. 'microsoft-graph-mail'
    provider      TEXT        NOT NULL,          -- native adapter id, e.g. 'microsoft_graph'
    display_name  TEXT        NOT NULL,
    is_enabled    BOOLEAN     NOT NULL DEFAULT FALSE,
    current_version_id UUID,                     -- FK added after connector_versions
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT
);

-- ── Immutable connector version rows ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS connector_versions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id  UUID        NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    version       INTEGER     NOT NULL,
    -- Server-owned manifest: provider, oauth scopes, actions, endpoints.
    manifest      JSONB       NOT NULL,
    -- Admin-set config snapshot (e.g. recipient domain allowlist) — immutable.
    config        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT,
    CONSTRAINT uq_connector_version UNIQUE (connector_id, version)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_connectors_current_version'
    ) THEN
        ALTER TABLE connectors
            ADD CONSTRAINT fk_connectors_current_version
            FOREIGN KEY (current_version_id)
            REFERENCES connector_versions(id) ON DELETE SET NULL;
    END IF;
END$$;

-- ── Local policy exceptions (separate source; explicit precedence) ──────────
-- Precedence at evaluation time: deny wins > group role > local allow exception.
CREATE TABLE IF NOT EXISTS connector_local_exceptions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id   UUID        NOT NULL REFERENCES connector_identities(id) ON DELETE CASCADE,
    effect        VARCHAR(10) NOT NULL CHECK (effect IN ('allow','deny')),
    -- what the exception applies to: a role grant or a specific connector.
    scope         VARCHAR(20) NOT NULL CHECK (scope IN ('role','connector')),
    role          VARCHAR(20) CHECK (role IN ('admin','editor','viewer')),
    connector_id  UUID        REFERENCES connectors(id) ON DELETE CASCADE,
    reason        TEXT,
    expires_at    TIMESTAMPTZ,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_local_exceptions_identity
    ON connector_local_exceptions(identity_id);

-- ── Connector-level client secrets (exist before any grant) ─────────────────
-- Envelope-encrypted (see src/security/crypto.py). Never returned by any API.
CREATE TABLE IF NOT EXISTS connector_client_secrets (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id  UUID        NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    purpose       TEXT        NOT NULL DEFAULT 'oauth_client_secret',
    version       INTEGER     NOT NULL DEFAULT 1,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    -- EncryptedBlob fields
    algo          TEXT        NOT NULL,
    kek_id        TEXT        NOT NULL,
    ciphertext    TEXT        NOT NULL,
    nonce         TEXT        NOT NULL,
    wrapped_dek   TEXT        NOT NULL,
    dek_nonce     TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT,
    CONSTRAINT uq_client_secret UNIQUE (connector_id, purpose, version)
);

-- ── group -> connector gating (which Entra groups may use a connector) ──────
CREATE TABLE IF NOT EXISTS connector_group_grants (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id     UUID        NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    tenant_id        TEXT        NOT NULL,
    group_object_id  TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by       TEXT,
    CONSTRAINT uq_connector_group UNIQUE (connector_id, group_object_id)
);

-- ── Per-user grants (consent) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS connector_user_grants (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id          UUID        NOT NULL REFERENCES connector_identities(id) ON DELETE CASCADE,
    connector_id         UUID        NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    connector_version_id UUID        NOT NULL REFERENCES connector_versions(id) ON DELETE RESTRICT,
    status               VARCHAR(20) NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active','revoked','expired','error')),
    external_account     TEXT,       -- bound mailbox identity (UPN) from provider
    scopes               TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at         TIMESTAMPTZ,
    CONSTRAINT uq_user_grant UNIQUE (identity_id, connector_id)
);
CREATE INDEX IF NOT EXISTS idx_user_grants_identity
    ON connector_user_grants(identity_id);

-- ── Grant-bound token material (refresh/access tokens), encrypted ───────────
CREATE TABLE IF NOT EXISTS connector_grant_secrets (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grant_id      UUID        NOT NULL REFERENCES connector_user_grants(id) ON DELETE CASCADE,
    kind          VARCHAR(20) NOT NULL CHECK (kind IN ('refresh_token','access_token')),
    -- EncryptedBlob fields
    algo          TEXT        NOT NULL,
    kek_id        TEXT        NOT NULL,
    ciphertext    TEXT        NOT NULL,
    nonce         TEXT        NOT NULL,
    wrapped_dek   TEXT        NOT NULL,
    dek_nonce     TEXT        NOT NULL,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_grant_secret UNIQUE (grant_id, kind)
);

-- ── OAuth authorize sessions (PKCE), short-lived ────────────────────────────
CREATE TABLE IF NOT EXISTS connector_oauth_sessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state                TEXT        NOT NULL UNIQUE,
    identity_id          UUID        NOT NULL REFERENCES connector_identities(id) ON DELETE CASCADE,
    connector_id         UUID        NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    connector_version_id UUID        NOT NULL REFERENCES connector_versions(id) ON DELETE RESTRICT,
    redirect_uri         TEXT        NOT NULL,
    -- PKCE verifier is a secret -> encrypted at rest.
    algo          TEXT        NOT NULL,
    kek_id        TEXT        NOT NULL,
    ciphertext    TEXT        NOT NULL,
    nonce         TEXT        NOT NULL,
    wrapped_dek   TEXT        NOT NULL,
    dek_nonce     TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    consumed_at   TIMESTAMPTZ
);

-- ── Durable result snapshots (export authorization source) ──────────────────
CREATE TABLE IF NOT EXISTS connector_result_snapshots (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id     TEXT        NOT NULL,     -- principal.user_id (never trusted rows)
    identity_id       UUID        REFERENCES connector_identities(id) ON DELETE SET NULL,
    connection        TEXT        NOT NULL,
    query_id          TEXT,
    source_query_hash TEXT        NOT NULL,
    policy_version    TEXT        NOT NULL,
    row_count         INTEGER     NOT NULL DEFAULT 0,
    columns           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    classification    TEXT        NOT NULL DEFAULT 'internal',
    payload_hash      TEXT        NOT NULL,     -- sha256 of canonical payload
    -- EncryptedBlob fields (encrypted rows)
    algo          TEXT        NOT NULL,
    kek_id        TEXT        NOT NULL,
    ciphertext    TEXT        NOT NULL,
    nonce         TEXT        NOT NULL,
    wrapped_dek   TEXT        NOT NULL,
    dek_nonce     TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_owner ON connector_result_snapshots(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_expiry ON connector_result_snapshots(expires_at);

-- ── Action gate: typed proposals with DB-enforced single execution ──────────
CREATE TABLE IF NOT EXISTS connector_action_proposals (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nonce                TEXT        NOT NULL UNIQUE,   -- one-time execution token
    owner_user_id        TEXT        NOT NULL,
    identity_id          UUID        REFERENCES connector_identities(id) ON DELETE SET NULL,
    connector_id         UUID        NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    connector_version_id UUID        NOT NULL REFERENCES connector_versions(id) ON DELETE RESTRICT,
    grant_id             UUID        REFERENCES connector_user_grants(id) ON DELETE SET NULL,
    snapshot_id          UUID        REFERENCES connector_result_snapshots(id) ON DELETE SET NULL,
    action               TEXT        NOT NULL,          -- e.g. 'send_email'
    params               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    confirmation_hash    TEXT,                          -- hash of exact confirmed payload
    status               VARCHAR(20) NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending','confirmed','attempted',
                                               'succeeded','failed','cancelled','expired')),
    row_version          INTEGER     NOT NULL DEFAULT 0,
    nonce_used_at        TIMESTAMPTZ,
    attempted_at         TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    provider_result      JSONB,                         -- status code / message id only
    error                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_owner ON connector_action_proposals(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON connector_action_proposals(status);

-- ── Append-only audit log ───────────────────────────────────────────────────
-- Event-type rows (not just current state). No tokens/rows/bodies; recipient
-- PII redacted upstream to domain/hash. FKs never cascade-delete audit rows.
CREATE TABLE IF NOT EXISTS connector_audit (
    id            BIGSERIAL PRIMARY KEY,
    event_time    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type    TEXT        NOT NULL,
    actor_user_id TEXT,
    actor_email   TEXT,
    identity_id   UUID        REFERENCES connector_identities(id) ON DELETE SET NULL,
    connector_id  UUID        REFERENCES connectors(id) ON DELETE SET NULL,
    grant_id      UUID        REFERENCES connector_user_grants(id) ON DELETE SET NULL,
    proposal_id   UUID        REFERENCES connector_action_proposals(id) ON DELETE SET NULL,
    snapshot_id   UUID        REFERENCES connector_result_snapshots(id) ON DELETE SET NULL,
    outcome       TEXT,
    detail        JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_connector_audit_time ON connector_audit(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_connector_audit_actor ON connector_audit(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_connector_audit_type ON connector_audit(event_type);

-- Enforce append-only: block UPDATE and DELETE at the DB level.
--
-- NOTE (operational hardening): this trigger is a backstop. A role that owns the
-- table (or is superuser) can DROP the trigger and then mutate history. For a
-- tamper-evident audit trail, run the APPLICATION with a least-privilege DB role
-- that has only INSERT/SELECT on connector_audit and cannot ALTER/DROP it, e.g.:
--     REVOKE UPDATE, DELETE, TRUNCATE ON connector_audit FROM app_role;
--     GRANT  INSERT, SELECT           ON connector_audit TO   app_role;
-- Keep table ownership/DDL under a separate migration-only role.
CREATE OR REPLACE FUNCTION connector_audit_block_mutate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'connector_audit is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_connector_audit_block ON connector_audit;
CREATE TRIGGER trg_connector_audit_block
    BEFORE UPDATE OR DELETE ON connector_audit
    FOR EACH ROW EXECUTE FUNCTION connector_audit_block_mutate();
