"""Every fixed cache key the MCP client writes must be allowed by the L2 table.

The ``insights_mcp_cache.cache_key`` CHECK constraint is an allowlist. Twice a
new key shipped without it (migrations 016 and 024 exist to repair that), and
each time the L2 write was rejected quietly: the data lived only in the
process-local L1, was lost on restart and never served as the stale fallback.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.lifespan import _probe_mcp_cache_catalog_key
from src.metadata import mcp_cache_service as keys

ROOT = Path(__file__).resolve().parents[2]
WRITTEN_KEYS = {
    keys.KEY_CATALOG, keys.KEY_CONNECTIONS, keys.KEY_TABLES_RICH, keys.KEY_KNOWLEDGE_QUESTIONS,
    keys.KEY_COLUMNS_STRUCT,
    # Written by earlier releases; still allowed so their pods keep working.
    keys.KEY_TABLES, keys.KEY_COLUMNS, keys.KEY_RELATIONSHIPS, keys.KEY_BUSINESS_TERMS,
    keys.KEY_KNOWLEDGE_PAIRS, keys.KEY_COLUMN_STATISTICS, keys.KEY_COLUMN_SAMPLES,
}


def _allowed(sql: str) -> set[str]:
    listed = re.search(r"cache_key\s+IN\s*\((?P<keys>[^)]*)\)", sql, re.IGNORECASE)
    assert listed, "no cache_key IN (...) list found"
    return set(re.findall(r"'([^']+)'", listed.group("keys")))


def _latest_migration() -> str:
    files = sorted((ROOT / "db/migrations/insights").glob("*.sql"))
    touching = [f for f in files if "insights_mcp_cache_cache_key_check" in f.read_text(encoding="utf-8")]
    return touching[-1].read_text(encoding="utf-8")


def test_the_latest_migration_allows_every_key_the_client_writes():
    assert WRITTEN_KEYS <= _allowed(_latest_migration())


def test_fresh_bootstrap_allows_the_same_keys_as_the_migrations():
    bootstrap = (ROOT / "src/metadata/insights_schema.py").read_text(encoding="utf-8")
    table = bootstrap[bootstrap.index("CREATE TABLE IF NOT EXISTS insights_mcp_cache"):]
    assert _allowed(table) == _allowed(_latest_migration())


@pytest.mark.asyncio
@pytest.mark.parametrize(("definition", "ready"), [
    ("CHECK (((cache_key)::text = ANY (ARRAY['connections'::text, 'catalog'::text])))", True),
    ("CHECK (((cache_key)::text = ANY (ARRAY['connections'::text, 'tables'::text])))", False),
    (None, True),  # no cache table (or no constraint): nothing is rejected
])
async def test_startup_says_when_the_catalog_key_is_not_allowed_yet(definition, ready, caplog):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=definition)

    assert await _probe_mcp_cache_catalog_key(conn) is ready
    assert ("037_mcp_cache_catalog_key" in caplog.text) is (not ready)
