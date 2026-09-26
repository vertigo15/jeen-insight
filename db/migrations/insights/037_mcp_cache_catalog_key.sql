-- ============================================================================
-- Jeen Insights: allow the whole-catalog entry in the MCP L2 cache
-- ============================================================================
-- The catalog client now caches the full MCP catalog as ONE entry
-- (cache_key 'catalog': every prompt section plus the source description), so
-- a reader can never combine sections from two different fetches and a cold
-- read is one row instead of eight. The CHECK constraint from migration 024
-- does not list that key, so the write is rejected (logged as a best-effort
-- failure) and the catalog lives only in the process-local L1 cache — lost on
-- every restart and never available as the stale fallback the L2 cache exists
-- to provide.
--
-- Expand only: every key allowed before stays allowed, so pods that still
-- write the per-section rows keep working. Idempotent.
-- ============================================================================

ALTER TABLE insights_mcp_cache
    DROP CONSTRAINT IF EXISTS insights_mcp_cache_cache_key_check;

ALTER TABLE insights_mcp_cache
    ADD CONSTRAINT insights_mcp_cache_cache_key_check
    CHECK (
        cache_key IN (
            'connections',
            'tables',
            'columns',
            'relationships',
            'business_terms',
            'knowledge_pairs',
            'tables_rich',
            'knowledge_questions',
            'columns_struct',
            'column_statistics',
            'column_samples',
            'catalog'
        )
        OR starts_with(cache_key, 'columns_struct:')
    );
