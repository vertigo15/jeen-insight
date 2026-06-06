-- ============================================================================
-- Jeen Insights: insights_mcp_cache
-- ============================================================================
-- L2 persistent cache for MCP catalog responses.
--
-- Cache key design:
--   source_key = '__global__'   → connection list (tool_list_sources)
--   source_key = <source_key>   → per-connection catalog data
--
--   cache_key  = 'connections'      → global connection list
--              = 'tables'           → table catalog for source_key
--              = 'columns'          → column catalog for source_key
--              = 'relationships'    → join relationships for source_key
--              = 'business_terms'   → business glossary for source_key
--              = 'knowledge_pairs'  → RAG Q→SQL examples for source_key
--
-- Rows are only written when cache_ttl_seconds > 0. When TTL = 0 (no cache),
-- the application bypasses this table entirely and calls MCP on every query.
--
-- Lifecycle:
--   - Written after every fresh MCP call (upsert on unique key).
--   - is_stale = true marks rows for refresh without deleting them,
--     so the stale payload can still be served if MCP is unreachable.
--   - Cascade-deleted when the parent insights_mcp_config row is deleted.
--   - "Refresh metadata" button sets is_stale = true for the target source_key
--     (or all rows when doing a global refresh).
--
-- Idempotent. Safe to run on the shared metadata DB.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_mcp_cache (
    id              SERIAL PRIMARY KEY,

    -- FK to the server that produced this cache entry.
    -- Cascade delete keeps the cache clean when a server is removed.
    mcp_server_id   INT         NOT NULL
                        REFERENCES insights_mcp_servers(id) ON DELETE CASCADE,

    -- '__global__' for the connection list; source_key for catalog entries.
    source_key      VARCHAR(255) NOT NULL,

    -- Type of cached data.
    cache_key       VARCHAR(50)  NOT NULL
                        CHECK (cache_key IN (
                            'connections',
                            'tables',
                            'columns',
                            'relationships',
                            'business_terms',
                            'knowledge_pairs'
                        )),

    -- The raw MCP tool response, normalised into a consistent JSONB array.
    payload         JSONB        NOT NULL,

    -- Timing
    fetched_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ  NOT NULL,   -- fetched_at + cache_ttl_seconds

    -- Set to true by "Refresh metadata". Stale rows are returned only as a
    -- fallback when the MCP server is unreachable.
    is_stale        BOOLEAN      NOT NULL DEFAULT false,

    -- One cache entry per (server, source, type).
    CONSTRAINT uq_mcp_cache_entry
        UNIQUE (mcp_server_id, source_key, cache_key)
);

-- Primary lookup path: valid (non-expired, non-stale) entries.
CREATE INDEX IF NOT EXISTS idx_mcp_cache_valid
    ON insights_mcp_cache(mcp_server_id, source_key, cache_key)
    WHERE is_stale = false;

-- Secondary path: stale fallback lookup (MCP unreachable).
CREATE INDEX IF NOT EXISTS idx_mcp_cache_stale_fallback
    ON insights_mcp_cache(mcp_server_id, source_key, cache_key, fetched_at DESC)
    WHERE is_stale = true;

COMMENT ON TABLE insights_mcp_cache IS
'Jeen Insights: L2 persistent cache for MCP catalog responses. '
'Survives restarts. Serves as stale fallback when the MCP server is unreachable. '
'Only populated when insights_mcp_config.cache_ttl_seconds > 0.';

COMMENT ON COLUMN insights_mcp_cache.source_key IS
'__global__ for the connection list; the actual source_key for per-connection '
'catalog data (tables, columns, relationships, etc.).';

COMMENT ON COLUMN insights_mcp_cache.payload IS
'Normalised JSONB array of the MCP tool response. Shape matches the '
'MetadataLoader bundle format so both DB and MCP paths share the same '
'downstream consumers.';

COMMENT ON COLUMN insights_mcp_cache.is_stale IS
'True after a manual Refresh or when expires_at has passed but the row has '
'not been replaced yet. Stale rows are served only as a last-resort fallback.';
