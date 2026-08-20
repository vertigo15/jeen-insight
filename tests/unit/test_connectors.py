"""Tests for the generic data-source connector layer."""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent.nodes.catalog import make_prompt_builder
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.nodes.validation import make_sqlglot_validate
from src.connectors.base import SqlRunner
from src.connectors.dialects import dialect_rules_for, sqlglot_dialect_for
from src.connectors.factory import (
    _build_databricks,
    get_connector_definition,
    normalize_database_type,
    public_connection_fields,
)
from src.tools.sql_tool import RunSqlTool


class FakeRunner(SqlRunner):
    database_type = "fake"

    def __init__(self):
        self.executed_sql = None

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def _execute(self, sql: str, statement_timeout_ms: int):
        self.executed_sql = sql
        return ["x"], [{"x": 1}]

    async def list_tables(self):
        return ["demo"]

    async def get_table_schema(self, table_name: str):
        return [{"column_name": "x", "data_type": "integer"}]


def test_factory_aliases_resolve_to_canonical_types():
    assert normalize_database_type("PostgreSQL") == "postgres"
    assert normalize_database_type("presto") == "trino"
    assert normalize_database_type("spark-sql") == "databricks"
    assert get_connector_definition("trino").canonical_type == "trino"


def test_public_connection_fields_are_sanitized():
    fields = public_connection_fields(
        {
            "host": "adb-123.azuredatabricks.net",
            "httpPath": "/sql/1.0/warehouses/abc",
            "accessToken": "secret-token",
            "catalog": "main",
            "schema": "sales",
        },
        "databricks",
    )

    assert fields["database_type"] == "databricks"
    assert fields["host"] == "adb-123.azuredatabricks.net"
    assert fields["http_path"] == "/sql/1.0/warehouses/abc"
    assert "accessToken" not in fields


def test_databricks_builder_accepts_host_port_config():
    runner = _build_databricks(
        source_key="databricks_demo",
        cfg={
            "hostPort": "adb-123.azuredatabricks.net:443",
            "httpPath": "/sql/1.0/warehouses/abc",
            "accessToken": "secret-token",
            "catalog": "main",
            "schema": "sales",
        },
    )

    assert runner.host == "adb-123.azuredatabricks.net"
    assert runner.http_path == "/sql/1.0/warehouses/abc"
    assert runner.catalog == "main"
    assert runner.schema == "sales"


@pytest.mark.asyncio
async def test_sql_runner_enforces_read_only_and_row_cap():
    runner = FakeRunner()

    blocked = await runner.run_sql("DELETE FROM demo")
    assert blocked["error_type"] == "read_only_blocked"
    assert runner.executed_sql is None

    result = await runner.run_sql("SELECT x FROM demo", limit=5, max_rows=10)
    assert result["row_count"] == 1
    # One sentinel row beyond the visible cap proves whether truncation occurred.
    assert "LIMIT 6" in runner.executed_sql


@pytest.mark.asyncio
async def test_sql_runner_uses_sentinel_row_for_truthful_truncation():
    runner = FakeRunner()

    async def execute(sql: str, statement_timeout_ms: int):
        runner.executed_sql = sql
        return ["x"], [{"x": i} for i in range(6)]

    runner._execute = execute
    result = await runner.run_sql("SELECT x FROM demo", limit=5, max_rows=10)

    assert result["rows"] == [{"x": i} for i in range(5)]
    assert result["row_count"] == 5
    assert result["truncated"] is True
    assert result["cap"] == 5


def test_dialect_metadata_for_supported_connectors():
    assert sqlglot_dialect_for("postgresql") == "postgres"
    assert sqlglot_dialect_for("trino") == "trino"
    assert sqlglot_dialect_for("databricks") == "databricks"
    assert "Postgres ::" in dialect_rules_for("trino")
    assert "backticks" in dialect_rules_for("databricks")


def test_sql_validation_uses_selected_dialect(monkeypatch):
    seen = {}

    def fake_parse(sql, *, dialect=None, error_level=None):
        seen["dialect"] = dialect
        return []

    import sqlglot

    monkeypatch.setattr(sqlglot, "parse", fake_parse)
    validate = make_sqlglot_validate(enabled=True)

    result = validate({"generated_sql": "SELECT 1", "database_type": "trino"})

    assert result["sqlglot_error"] == "SQL could not be parsed — empty statement."
    assert seen["dialect"] == "trino"


async def test_prompt_builder_includes_active_connection_context():
    prompt_builder = make_prompt_builder(PromptLoader())

    result = await prompt_builder(
        {
            "source_key": "sales_trino",
            "connection_display_name": "Sales Lake",
            "database_type": "trino",
            "connection_database": "hive",
            "connection_catalog": "hive",
            "connection_schema": "mart",
            "metadata_bundle": {},
            "question": "top products",
            "conversation_history": [],
        }
    )

    prompt = result["system_prompt"]
    assert "Source key: sales_trino" in prompt
    assert "Catalog: hive" in prompt
    assert "Schema: mart" in prompt
    assert "exactly one `run_sql` tool call" in prompt
    assert result["structured_prompt"]["connection"]["catalog"] == "hive"


def test_run_sql_tool_schema_describes_target_and_format():
    tool = RunSqlTool(
        None,
        connection_display_name="Sales Lake",
        database_type="databricks",
        source_key="sales_databricks",
        catalog="main",
        schema="gold",
    )

    sql_description = tool.get_schema()["function"]["parameters"]["properties"]["sql"][
        "description"
    ]
    assert "single read-only SELECT or WITH statement" in sql_description
    assert "databricks dialect" in sql_description
    assert "source_key=sales_databricks" in sql_description
    assert "catalog=main" in sql_description
    assert "schema=gold" in sql_description
