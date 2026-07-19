-- Per-source authorization grants for analytics connections.
--
-- Non-admin callers require an explicit user, role, or Entra group grant.
-- Administrators retain access to every registered source.

CREATE TABLE IF NOT EXISTS insights_source_access (
    source_key   VARCHAR(255) NOT NULL,
    subject_type VARCHAR(16)  NOT NULL
        CHECK (subject_type IN ('user', 'role', 'group')),
    subject_id   TEXT         NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_key, subject_type, subject_id)
);

CREATE INDEX IF NOT EXISTS idx_insights_source_access_subject
    ON insights_source_access(subject_type, subject_id);
