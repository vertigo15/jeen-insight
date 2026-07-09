"""Connector factory for ``settings_services`` database rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Type

from src.connectors.base import SqlRunner, UnsupportedConnectionType
from src.connectors.databricks import DatabricksSqlRunner
from src.connectors.dialects import dialect_rules_for, sqlglot_dialect_for
from src.connectors.postgres import PostgresSqlRunner
from src.connectors.trino import TrinoSqlRunner


@dataclass(frozen=True)
class ConnectorDefinition:
    canonical_type: str
    runner_cls: Type[SqlRunner]
    default_port: Optional[int]

    @property
    def sqlglot_dialect(self) -> Optional[str]:
        return sqlglot_dialect_for(self.canonical_type)

    @property
    def dialect_rules(self) -> str:
        return dialect_rules_for(self.canonical_type)


CONNECTOR_REGISTRY: dict[str, ConnectorDefinition] = {
    "postgres": ConnectorDefinition("postgres", PostgresSqlRunner, 5432),
    "postgresql": ConnectorDefinition("postgres", PostgresSqlRunner, 5432),
    "trino": ConnectorDefinition("trino", TrinoSqlRunner, 443),
    "presto": ConnectorDefinition("trino", TrinoSqlRunner, 443),
    "databricks": ConnectorDefinition("databricks", DatabricksSqlRunner, None),
    "spark": ConnectorDefinition("databricks", DatabricksSqlRunner, None),
    "spark-sql": ConnectorDefinition("databricks", DatabricksSqlRunner, None),
}


def get_connector_definition(service_type: str | None) -> ConnectorDefinition:
    key = (service_type or "").strip().lower()
    definition = CONNECTOR_REGISTRY.get(key)
    if not definition:
        supported = ", ".join(sorted(CONNECTOR_REGISTRY))
        raise UnsupportedConnectionType(
            f"service_type={service_type!r} is not supported. Supported types: {supported}."
        )
    return definition


def normalize_database_type(service_type: str | None) -> str:
    """Return the canonical database type used by prompts, validation and logs."""
    return get_connector_definition(service_type).canonical_type


async def build_sql_runner(row: dict) -> SqlRunner:
    """Build and initialize a ``SqlRunner`` from a ``settings_services`` row."""
    service_type = row.get("service_type")
    definition = get_connector_definition(service_type)
    cfg = decode_config(row.get("connection_config"))
    source_key = row.get("name")

    if definition.canonical_type == "postgres":
        runner = _build_postgres(source_key=source_key, cfg=cfg)
    elif definition.canonical_type == "trino":
        runner = _build_trino(source_key=source_key, cfg=cfg)
    elif definition.canonical_type == "databricks":
        runner = _build_databricks(source_key=source_key, cfg=cfg)
    else:  # pragma: no cover - defensive; registry controls canonical values.
        raise UnsupportedConnectionType(f"Unsupported connector {service_type!r}.")

    await runner.initialize()
    return runner


def public_connection_fields(cfg: Dict[str, Any], service_type: str | None) -> Dict[str, Any]:
    """Return sanitized connection metadata for the API/UI."""
    definition = get_connector_definition(service_type)
    host, port = _host_port(cfg, default_port=definition.default_port)
    catalog = coerce_str(cfg.get("catalog") or cfg.get("database"))
    schema = coerce_str(
        cfg.get("databaseSchema")
        or cfg.get("schema")
        or cfg.get("db_schema")
    )
    return {
        "database_type": definition.canonical_type,
        "host": host,
        "port": port,
        "database": coerce_str(cfg.get("database") or catalog),
        "catalog": catalog,
        "schema": schema,
        "http_path": coerce_str(cfg.get("httpPath") or cfg.get("http_path")),
        "enable_ssl": coerce_bool(cfg.get("ssl"), default=True),
    }


def _build_postgres(*, source_key: Optional[str], cfg: Dict[str, Any]) -> PostgresSqlRunner:
    host, port = _host_port(cfg, default_port=5432)
    database = coerce_str(cfg.get("database"))
    if not host:
        raise UnsupportedConnectionType("Postgres connection_config is missing 'host'.")
    if not database:
        raise UnsupportedConnectionType("Postgres connection_config is missing 'database'.")
    return PostgresSqlRunner(
        source_key=source_key,
        host=host,
        port=port or 5432,
        database=database,
        username=coerce_str(cfg.get("username")) or "",
        password=coerce_str(cfg.get("password")) or "",
        enable_ssl=coerce_bool(cfg.get("ssl"), default=True),
        schema=coerce_str(cfg.get("databaseSchema") or cfg.get("schema")) or "public",
    )


def _build_trino(*, source_key: Optional[str], cfg: Dict[str, Any]) -> TrinoSqlRunner:
    host, port = _host_port(cfg, default_port=443)
    username = coerce_str(cfg.get("username") or cfg.get("user"))
    if not host:
        raise UnsupportedConnectionType("Trino connection_config is missing 'host'.")
    if not username:
        raise UnsupportedConnectionType("Trino connection_config is missing 'username'.")
    catalog = coerce_str(cfg.get("catalog") or cfg.get("database"))
    schema = coerce_str(cfg.get("databaseSchema") or cfg.get("schema"))
    request_timeout = coerce_float(cfg.get("requestTimeout") or cfg.get("request_timeout")) or 30.0
    return TrinoSqlRunner(
        source_key=source_key,
        host=host,
        port=port or 443,
        username=username,
        password=coerce_str(cfg.get("password")),
        access_token=coerce_str(cfg.get("accessToken") or cfg.get("access_token") or cfg.get("token")),
        catalog=catalog,
        schema=schema,
        http_scheme=coerce_str(cfg.get("httpScheme") or cfg.get("http_scheme")) or "https",
        auth=coerce_str(cfg.get("auth") or cfg.get("authType") or cfg.get("auth_type")),
        request_timeout=request_timeout,
        max_workers=coerce_int(cfg.get("maxWorkers") or cfg.get("max_workers")) or 4,
    )


def _build_databricks(*, source_key: Optional[str], cfg: Dict[str, Any]) -> DatabricksSqlRunner:
    host, _ = _host_port(cfg, default_port=None)
    host = host or coerce_str(
        cfg.get("serverHostname")
        or cfg.get("server_hostname")
    )
    http_path = coerce_str(cfg.get("httpPath") or cfg.get("http_path"))
    access_token = coerce_str(
        cfg.get("accessToken")
        or cfg.get("access_token")
        or cfg.get("token")
        or cfg.get("password")
    )
    if not host:
        raise UnsupportedConnectionType("Databricks connection_config is missing 'host'.")
    if not http_path:
        raise UnsupportedConnectionType("Databricks connection_config is missing 'httpPath'.")
    if not access_token:
        raise UnsupportedConnectionType(
            "Databricks connection_config is missing 'accessToken' (or token/password)."
        )
    timeout = coerce_float(cfg.get("timeoutSeconds") or cfg.get("timeout_seconds")) or 30.0
    return DatabricksSqlRunner(
        source_key=source_key,
        host=host,
        http_path=http_path,
        access_token=access_token,
        catalog=coerce_str(cfg.get("catalog") or cfg.get("database")),
        schema=coerce_str(cfg.get("databaseSchema") or cfg.get("schema")),
        timeout_seconds=timeout,
        max_workers=coerce_int(cfg.get("maxWorkers") or cfg.get("max_workers")) or 4,
    )


def decode_config(value: Any) -> Dict[str, Any]:
    """Convert a JSONB connection_config payload to a Python dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def coerce_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on", "t"):
            return True
        if v in ("false", "0", "no", "off", "f", ""):
            return False
    return default


def _host_port(cfg: Dict[str, Any], *, default_port: Optional[int]) -> tuple[Optional[str], Optional[int]]:
    host = coerce_str(cfg.get("host"))
    port = coerce_int(cfg.get("port")) or default_port
    if (host is None or port is None) and cfg.get("hostPort"):
        host_port_str = str(cfg["hostPort"])
        if ":" in host_port_str:
            parsed_host, parsed_port = host_port_str.rsplit(":", 1)
            host = host or parsed_host
            port = port or coerce_int(parsed_port)
        else:
            host = host or host_port_str
    return host, port
