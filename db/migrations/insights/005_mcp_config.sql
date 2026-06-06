-- ============================================================================
-- Jeen Insights: insights_mcp_servers
-- ============================================================================
-- Saved MCP server configurations. At most one row with is_active = true.
--
-- One active server serves ALL connections (source_keys). Each connection
-- passes its source_key as a parameter to MCP tools.
-- Switching to DB mode: UPDATE insights_mcp_servers SET is_active = false
--   + UPDATE app_settings SET value='db' WHERE key='catalog_source'.
--
-- The health column stores the full last health-check result as JSONB,
-- including tool list, protocol version, SDK, capabilities and latency.
-- bearer_token is stored plain text in v1; encryption flagged for v2.
--
-- Idempotent. Safe to run on the shared metadata DB.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_mcp_servers (
    id          SERIAL PRIMARY KEY,

    -- Mode toggle. Partial unique index enforces at-most-one active row.
    is_active   BOOLEAN     NOT NULL DEFAULT false,

    -- Server identity
    server_name TEXT        NOT NULL,
    endpoint    TEXT        NOT NULL,
    transport   VARCHAR(10) NOT NULL DEFAULT 'http'
                    CHECK (transport IN ('stdio', 'sse', 'http')),

    -- Authentication
    auth_type       VARCHAR(20) NOT NULL DEFAULT 'none'
                        CHECK (auth_type IN ('none', 'bearer', 'oauth')),
    bearer_token    TEXT,       -- plain text v1; encrypt in v2

    -- Cache TTL (seconds). 0 = no cache (always call MCP).
    -- UI options: 0 | 300 (5 min) | 900 (15 min) | 3600 (1 hr) | 86400 (24 hr)
    cache_ttl_seconds   INT NOT NULL DEFAULT 900
                            CHECK (cache_ttl_seconds IN (0, 300, 900, 3600, 86400)),

    -- ── Health report ──────────────────────────────────────────────────────
    -- Full result of the last "Test & health check" run, stored as a JSONB
    -- blob so the UI can render the health card without re-running the check.
    --
    -- Shape:
    --   {
    --     "status":         "healthy" | "degraded" | "down",
    --     "latency_ms":     412,
    --     "ping_ms":        38,
    --     "protocol":       "2025-06-18",
    --     "sdk":            "mcp-python 1.9.2",
    --     "server_version": "1.4.0",
    --     "uptime":         "14d 6h",
    --     "tls":            "TLS 1.3",
    --     "capabilities":   ["tools", "resources", "prompts", "logging"],
    --     "resources":      12,
    --     "prompts":        3,
    --     "tools": [
    --       {"name": "catalog.list_tables", "description": "…",
    --        "need": "list_tables"},
    --       {"name": "catalog.describe_table", "description": "…",
    --        "need": "describe_table"},
    --       ...
    --     ],
    --     "checked_at": "2026-06-05T12:00:00Z"
    --   }
    --
    -- NULL = server has never been tested.
    health          JSONB,
    last_checked_at TIMESTAMPTZ,

    -- Audit
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enforce at most one active MCP server at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_mcp_servers_active
    ON insights_mcp_servers(is_active)
    WHERE is_active = true;

COMMENT ON TABLE insights_mcp_servers IS
'Jeen Insights: saved MCP server configs. At most one is_active = true at a time. '
'When active (and app_settings catalog_source = mcp), all catalog data for every '
'connection is sourced from this server instead of the metadata DB tables.';

COMMENT ON COLUMN insights_mcp_servers.health IS
'Full health-check result JSONB. Persisted so the UI can render the health card '
'on load without re-running the check. NULL until first Test & health check.';

COMMENT ON COLUMN insights_mcp_servers.bearer_token IS
'Bearer token for MCP server auth. Stored plain text in v1. '
'Flagged for app-layer encryption in v2.';
