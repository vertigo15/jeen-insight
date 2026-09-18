-- ============================================================================
-- Jeen Insights: allow the MCP statistics / sample-value sections in the L2 cache
-- ============================================================================
-- The catalog client caches two optional prompt sections from the MCP catalog
-- (`column_statistics`, `column_samples`) alongside the six bundle sections.
-- Migration 016 replaced the CHECK constraint without listing them, so those
-- L2 writes were rejected (logged as best-effort failures) and the sections
-- only ever lived in the process-local L1 cache — lost on every restart and
-- never available as the stale fallback the L2 cache exists to provide.
--
-- This migration replaces the constraint with the full key set. Idempotent.
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
            'column_samples'
        )
        OR starts_with(cache_key, 'columns_struct:')
    );
