-- ============================================================================
-- Jeen Insights: widen insights_mcp_cache.cache_key for the structured datasets
-- ============================================================================
-- The L2 cache originally allowed only the six prompt-bundle sections
-- (connections, tables, columns, relationships, business_terms, knowledge_pairs).
-- The catalog client since added structured autocomplete datasets with new keys:
--   * tables_rich            (@ table picker / sidebar)
--   * knowledge_questions    (/ template autocomplete)
--   * columns_struct:<scope> (# column autocomplete; scope = table name or 'all')
--
-- Those writes were being rejected by the old CHECK constraint (and the
-- 50-char cap was too small for columns_struct:<long_table_name>), so the L2
-- cache silently never persisted them — which removed the stale-fallback that is
-- supposed to keep the catalog serving when the MCP server is unreachable.
--
-- This migration widens the column and replaces the constraint to allow the
-- fixed keys plus the columns_struct:* family. Idempotent.
-- ============================================================================

ALTER TABLE insights_mcp_cache ALTER COLUMN cache_key TYPE VARCHAR(160);

ALTER TABLE insights_mcp_cache
    DROP CONSTRAINT IF EXISTS insights_mcp_cache_cache_key_check;

-- Use starts_with() rather than LIKE 'columns_struct:%': in LIKE the underscore
-- is a single-char wildcard, so that pattern would also accept keys like
-- 'columnsXstruct:...'. starts_with() is a literal prefix match.
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
            'columns_struct'
        )
        OR starts_with(cache_key, 'columns_struct:')
    );
