-- ============================================================================
-- Jeen Insights: auth_users (Flask UI login)
-- ============================================================================
-- Stores UI login accounts. The Flask UI reads/writes this table directly via
-- psycopg (see src/auth_db.py). The FastAPI API does not use it.
--
-- Seeds a default admin account when missing:
--   email: admin
--   password: admin   (change after first login)
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

-- Default admin (password: admin). Skip if email already exists.
INSERT INTO auth_users (name, email, password_hash, role, status, avatar_hue)
SELECT
    'Admin',
    'admin',
    '$2b$12$04PLmZ5VyTctiR6QODu5seF8uRExKLZkS/euG1Xg9cWEwXHwtbHYK',
    'admin',
    'active',
    210
WHERE NOT EXISTS (
    SELECT 1 FROM auth_users WHERE email = 'admin'
);
