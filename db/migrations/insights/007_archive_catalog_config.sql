-- ============================================================================
-- Jeen Insights: archive insights_catalog_config
-- ============================================================================
-- The application now uses a SINGLE GLOBAL catalog source, stored in
-- app_settings.catalog_source ('db' | 'mcp'), for every connection. The cache
-- TTL is MCP-only and stored on the active MCP server
-- (insights_mcp_servers.cache_ttl_seconds).
--
-- The old per-connection table insights_catalog_config is therefore no longer
-- consulted at runtime. We RENAME (rather than drop) it so historical rows are
-- retained and the change is trivially reversible.
--
-- Rollback:
--   ALTER TABLE insights_catalog_config_archive
--       RENAME TO insights_catalog_config;
-- ============================================================================

ALTER TABLE IF EXISTS insights_catalog_config
    RENAME TO insights_catalog_config_archive;
