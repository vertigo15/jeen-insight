"""Tests for the generic data-source connector layer."""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent.nodes.catalog import make_prompt_builder
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.nodes.validation import make_sqlglot_validate
from src.connectors.base import SqlRunner
from src.connectors.dialects import dialect_rules_for, sqlglot_dialect_for
from src.connectors.base import ConnectorConnectionError, UnsupportedConnectionType
from src.connectors.factory import (
    _build_databricks,
    _build_trino,
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


def test_sql_string_literal_is_safe_for_probe_operands():
    from src.connectors.base import sql_string_literal
    from src.connectors.databricks import DatabricksSqlRunner
    from src.connectors.postgres import PostgresSqlRunner
    from src.connectors.trino import TrinoSqlRunner

    assert sql_string_literal("O'Brien", "postgres") == "'O''Brien'"
    assert sql_string_literal("x' OR '1'='1", "postgres") == "'x'' OR ''1''=''1'"
    # Control characters and NULs never reach the engine; length is capped.
    assert sql_string_literal("a\x00b\x01c\n", "postgres") == "'abc\n'".replace("\n", "")
    assert len(sql_string_literal("x" * 10_000, "postgres")) == 512 + 2
    # Backslash is an escape character on Spark-family engines only.
    assert sql_string_literal("a\\b", "databricks") == "'a\\\\b'"
    assert sql_string_literal("a\\b", "postgres") == "'a\\b'"
    assert sql_string_literal("a\\b", "trino") == "'a\\b'"
    # Only runners that stop a statement server-side may be probed.
    assert PostgresSqlRunner.supports_server_side_timeout is True
    assert TrinoSqlRunner.supports_server_side_timeout is True
    assert DatabricksSqlRunner.supports_server_side_timeout is True
    assert SqlRunner.supports_server_side_timeout is False


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


def test_trino_builder_keeps_password_auth_when_mode_is_absent():
    runner = _build_trino(
        source_key="sales",
        cfg={
            "host": "trino.internal",
            "username": "analyst",
            "password": "secret",
            "catalog": "hive",
            "databaseSchema": "mart",
            "clientCertPath": "/mnt/trino-mtls/client.crt",
            "clientKeyPath": "/mnt/trino-mtls/client.key",
        },
    )

    assert runner.auth == "basic"
    assert runner.password == "secret"
    assert runner.client_cert_path is None
    assert runner.verify is True


def test_trino_builder_selects_mtls_and_ignores_a_leftover_password():
    runner = _build_trino(
        source_key="sales",
        cfg={
            "authenticationMode": "mtls",
            "host": "trino.internal",
            "username": "analyst",
            "password": "leftover",
            "catalog": "hive",
            "databaseSchema": "mart",
            "verify": "false",
            "clientCertPath": "/mnt/trino-mtls/client.crt",
            "clientKeyPath": "/mnt/trino-mtls/client.key",
            "caCertPath": "/mnt/trino-mtls/ca.crt",
        },
    )

    assert runner.auth == "mtls"
    assert runner.password is None
    assert runner.access_token is None
    assert runner.http_scheme == "https"
    assert runner.client_cert_path == "/mnt/trino-mtls/client.crt"
    assert runner.client_key_path == "/mnt/trino-mtls/client.key"
    # An explicit verify:false disables server checks even when a CA path is set.
    assert runner.verify is False


def test_trino_builder_uses_a_ca_path_for_server_verification():
    runner = _build_trino(
        source_key="sales",
        cfg={
            "authenticationMode": "basic",
            "host": "trino.internal",
            "username": "analyst",
            "password": "secret",
            "verify": "/etc/jeen/trino-ca.pem",
        },
    )

    assert runner.auth == "basic"
    assert runner.verify == "/etc/jeen/trino-ca.pem"


def test_trino_builder_infers_mtls_from_mounted_paths_without_a_password():
    runner = _build_trino(
        source_key="sales",
        cfg={
            "host": "trino.internal",
            "username": "analyst",
            "client_cert_path": "/mnt/trino-mtls/client.crt",
            "client_key_path": "/mnt/trino-mtls/client.key",
            "ca_cert_path": "/mnt/trino-mtls/ca.crt",
        },
    )

    assert runner.auth == "mtls"
    assert runner.verify == "/mnt/trino-mtls/ca.crt"


def test_trino_builder_rejects_mtls_without_certificate_paths():
    with pytest.raises(UnsupportedConnectionType, match="clientCertPath"):
        _build_trino(
            source_key="sales",
            cfg={
                "authenticationMode": "mtls",
                "host": "trino.internal",
                "username": "analyst",
            },
        )


def test_trino_builder_rejects_mtls_over_http():
    with pytest.raises(UnsupportedConnectionType, match="https"):
        _build_trino(
            source_key="sales",
            cfg={
                "authenticationMode": "mtls",
                "host": "trino.internal",
                "username": "analyst",
                "httpScheme": "http",
                "clientCertPath": "/mnt/trino-mtls/client.crt",
                "clientKeyPath": "/mnt/trino-mtls/client.key",
            },
        )


def test_trino_mtls_connect_presents_the_client_certificate(monkeypatch, tmp_path):
    from src.connectors.trino import TrinoSqlRunner

    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca.crt"
    cert.write_text("cert")
    key.write_text("key")
    ca.write_text("ca")
    captured = {}

    class FakeCertificateAuth:
        def __init__(self, cert_path, key_path):
            captured["cert"] = cert_path
            captured["key"] = key_path

    def fake_connect(**kwargs):
        captured["kwargs"] = kwargs
        return object()

    import trino.auth
    import trino.dbapi

    monkeypatch.setenv("TRINO_MTLS_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(trino.auth, "CertificateAuthentication", FakeCertificateAuth)
    monkeypatch.setattr(trino.dbapi, "connect", fake_connect)

    runner = TrinoSqlRunner(
        source_key="sales",
        host="trino.internal",
        username="analyst",
        password="leftover",
        auth="mtls",
        client_cert_path=str(cert),
        client_key_path=str(key),
        verify=str(ca),
    )
    runner._connect()

    assert captured["cert"] == str(cert)
    assert captured["key"] == str(key)
    assert isinstance(captured["kwargs"]["auth"], FakeCertificateAuth)
    assert captured["kwargs"]["verify"] == str(ca)
    assert captured["kwargs"]["http_scheme"] == "https"
    assert "password" not in captured["kwargs"]


def test_trino_mtls_connect_refuses_a_missing_key(monkeypatch, tmp_path):
    from src.connectors.trino import TrinoSqlRunner

    monkeypatch.setenv("TRINO_MTLS_MOUNT_PATH", str(tmp_path))
    cert = tmp_path / "client.crt"
    cert.write_text("cert")
    runner = TrinoSqlRunner(
        source_key="sales",
        host="trino.internal",
        username="analyst",
        auth="mtls",
        client_cert_path=str(cert),
        client_key_path=str(tmp_path / "missing.key"),
    )

    with pytest.raises(ConnectorConnectionError, match="client private key"):
        runner._connect()


def test_trino_mtls_connect_refuses_a_file_outside_the_mount(monkeypatch, tmp_path):
    from src.connectors.trino import TrinoSqlRunner

    mount = tmp_path / "mnt"
    mount.mkdir()
    outside = tmp_path / "client.crt"
    outside.write_text("cert")
    key = mount / "client.key"
    key.write_text("key")
    monkeypatch.setenv("TRINO_MTLS_MOUNT_PATH", str(mount))
    runner = TrinoSqlRunner(
        source_key="sales",
        host="trino.internal",
        username="analyst",
        auth="mtls",
        client_cert_path=str(outside),
        client_key_path=str(key),
    )

    with pytest.raises(ConnectorConnectionError, match="inside"):
        runner._connect()


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
