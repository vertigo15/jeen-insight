"""DB catalog mode must never hand the model a system-schema, hidden or dropped table.

Schema Modeler harvests whole databases, so `metadata_tables` can hold the
PostgreSQL information_schema views next to the real tables. The loader
filters them in SQL (when the columns exist) and by name as a fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.metadata.metadata_loader import MetadataLoader, _VISIBLE_TABLE_CLAUSES


def _row(**kv):
    row = MagicMock()
    row.__getitem__ = lambda self, key: kv[key]
    return row


def _loader(fetch_results):
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fetch_results)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return MetadataLoader(pool=pool), conn


@pytest.mark.asyncio
async def test_predicate_uses_only_the_columns_this_schema_modeler_has():
    loader, conn = _loader([[_row(column_name="schema_name"), _row(column_name="is_hidden")]])
    predicate = await loader._visible_tables_predicate(conn)
    assert _VISIBLE_TABLE_CLAUSES["schema_name"] in predicate
    assert _VISIBLE_TABLE_CLAUSES["is_hidden"] in predicate
    assert "is_deleted" not in predicate
    # Probed once, then cached.
    assert await loader._visible_tables_predicate(conn) == predicate
    assert conn.fetch.await_count == 1


@pytest.mark.asyncio
async def test_predicate_degrades_to_true_when_probe_fails():
    loader, conn = _loader([RuntimeError("no information_schema access")])
    assert await loader._visible_tables_predicate(conn) == "TRUE"


@pytest.mark.asyncio
async def test_load_tables_drops_system_objects_by_name_as_a_fallback():
    probe = [_row(column_name="schema_name")]
    tables = [
        _row(table_name="dimcustomer", table_description="Customers"),
        _row(table_name="tables", table_description=None),          # information_schema view
        _row(table_name="_pg_foreign_servers", table_description=None),
        _row(table_name="pg_stat_statements", table_description=None),
        _row(table_name="factinternetsales", table_description=" "),
    ]
    loader, conn = _loader([probe, tables])
    lines = await loader._load_tables("AdventureWorksDW")
    assert lines == ["dimcustomer - Customers", "factinternetsales"]
    sql = conn.fetch.await_args_list[1].args[0]
    assert "information_schema" in sql and "pg_catalog" in sql


@pytest.mark.asyncio
async def test_load_columns_drops_columns_of_system_tables():
    probe = [_row(column_name="schema_name"), _row(column_name="is_hidden"), _row(column_name="is_deleted")]
    columns = [
        _row(table_name="dimcustomer", line="dimcustomer.customerkey - Type: integer, PK: true"),
        _row(table_name="columns", line="columns.table_name - Type: name"),
        _row(table_name="_pg_user_mappings", line="_pg_user_mappings.oid - Type: oid"),
    ]
    loader, conn = _loader([probe, columns])
    lines = await loader._load_columns("AdventureWorksDW")
    assert lines == ["dimcustomer.customerkey - Type: integer, PK: true"]
    sql = conn.fetch.await_args_list[1].args[0]
    # Columns of hidden / dropped / system tables are excluded in SQL as well.
    assert "NOT EXISTS" in sql and "is_hidden" in sql and "is_deleted" in sql
