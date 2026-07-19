-- Curated data classifications are enforced before query execution.  They are
-- intentionally separate from source ACLs: source access permits a user to use
-- a connection, while this table limits sensitive columns within that source.
CREATE TABLE IF NOT EXISTS insights_column_entitlements (
    source_key    TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'internal',
    allowed_roles TEXT[] NOT NULL DEFAULT ARRAY['admin']::TEXT[],
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_key, table_name, column_name)
);

CREATE INDEX IF NOT EXISTS idx_column_entitlements_source
    ON insights_column_entitlements(source_key);
