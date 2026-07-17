-- ============================================================================
-- Jeen Insights: auth_users (Flask UI login)
-- ============================================================================
-- Stores UI login accounts. The Flask UI reads/writes this table directly via
-- psycopg (see src/auth_db.py). The FastAPI API does not use it.
--
-- No default admin is seeded. On first run (no usable admin account) the Flask
-- UI shows a one-time admin setup screen (/setup) so the operator creates the
-- first admin with their own password. See migration 014, which removes any
-- legacy seeded default-admin credential that was never customized.
--
-- Idempotent. Safe to run on the shared metadata DB.
-- ============================================================================

CREATE TABLE IF NOT EXISTS auth_users (
    id              SERIAL PRIMARY KEY,
    name            TEXT        NOT NULL,
    email           TEXT        NOT NULL,
    password_hash   TEXT        NOT NULL,
    role            VARCHAR(20) NOT NULL DEFAULT 'viewer'
                        CHECK (role IN ('admin', 'editor', 'viewer')),
    status          VARCHAR(20) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'disabled')),
    avatar_hue      INT         NOT NULL DEFAULT 0
                        CHECK (avatar_hue >= 0 AND avatar_hue <= 359),
    last_active_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT auth_users_email_unique UNIQUE (email)
);

CREATE INDEX IF NOT EXISTS idx_auth_users_email
    ON auth_users (email);
