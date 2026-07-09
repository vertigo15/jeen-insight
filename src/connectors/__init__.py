"""Pluggable data-source connectors for Jeen Insights."""

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorSyntaxError,
    QueryTimeout,
    ReadOnlyViolation,
    SqlRunner,
    UnsupportedConnectionType,
    is_read_only_sql,
)
from src.connectors.factory import (
    CONNECTOR_REGISTRY,
    build_sql_runner,
    get_connector_definition,
    normalize_database_type,
    public_connection_fields,
)
from src.connectors.postgres import PostgresSqlRunner

__all__ = [
    "CONNECTOR_REGISTRY",
    "ConnectorAuthError",
    "ConnectorConnectionError",
    "ConnectorError",
    "ConnectorPermissionError",
    "ConnectorSyntaxError",
    "PostgresSqlRunner",
    "QueryTimeout",
    "ReadOnlyViolation",
    "SqlRunner",
    "UnsupportedConnectionType",
    "build_sql_runner",
    "get_connector_definition",
    "is_read_only_sql",
    "normalize_database_type",
    "public_connection_fields",
]
