"""Pluggable data-source connectors + the per-user connector/integration platform.

This package hosts two distinct concerns that happen to share a namespace:

Data-source connectors (read-only SQL): ``base``, ``factory``, ``postgres``,
``databricks``, ``trino``, ``dialects`` — the text-to-SQL execution layer.

Per-user connector / integration platform (outbound actions): ``catalog``,
``identity_service``, ``registry_service``, ``grant_service``,
``snapshot_service``, ``action_gate``, ``audit_service``, ``providers`` — the
admin-registered, group-gated, per-user-consented destination connectors.

Only the data-source connector API is re-exported at package level (many modules
do ``from src.connectors import SqlRunner``). Platform modules are imported by
their submodule path (e.g. ``from src.connectors.catalog import list_catalog``)
to keep the two concerns clearly separated and avoid heavy import chains.
"""

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
