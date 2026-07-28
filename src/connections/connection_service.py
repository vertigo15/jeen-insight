"""Lookup of available data-source connections, sourced from `public.settings_services`.

Schema-modeler is responsible for CRUD on `settings_services`; Jeen Insights is a
read-only consumer. Each row carries the connection details inside a
`connection_config` JSONB column. We treat the row's `name` as the
`source_key` — the same value that appears in
`metadata_tables.source` / `metadata_columns.source` / `knowledge_pairs.source` /
`metadata_business_terms.source` / `metadata_relationships.source`.

This module also caches one `SqlRunner` per `source_key` so we don't
reopen pools per request.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import asyncpg

from src.connectors import SqlRunner, UnsupportedConnectionType
from src.connectors.factory import (
    build_sql_runner,
    coerce_bool,
    coerce_int,
    coerce_str,
    decode_config,
    is_power_bi_service_type,
    power_bi_connection_fields,
    public_connection_fields,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPORARY: hardcoded Power BI workspace + dataset coordinates
# ─────────────────────────────────────────────────────────────────────────────
# The Power BI sources in ``settings_services`` are registered as offline
# ``.pbit`` uploads (category='dashboard', service_type='PowerBISemanticFile').
# Their ``connection_config`` carries only model metadata (fileName / modelName)
# — NOT the live workspace + dataset id needed to call the executeQueries REST
# API. Until schema-modeler captures those live coordinates (the user will set
# them in Jeen schema later), map each ``source_key`` (= settings_services.name)
# to its published dataset here so the text-to-DAX agent can run live queries.
#
# Workspace: https://app.powerbi.com/groups/79fe9a53-6ef1-4dae-b477-39429f19009d
# Dataset ids were resolved from GET /v1.0/myorg/groups/{ws}/datasets.
# REMOVE this block once connection_config carries workspaceId + datasetId.
_TEMP_PBI_WORKSPACE_ID = "79fe9a53-6ef1-4dae-b477-39429f19009d"
_TEMP_PBI_DATASETS: Dict[str, str] = {
    "test3": "adb541a7-8133-4f01-8fb5-3b92e4c10e60",  # Sales & Returns Sample v201912
    "Awesome_Chocolates": "2fb1253a-c352-403c-88d4-4fe6981bc3f8",
    "sakila powerbi final": "c99b06cd-6c8d-4e24-8be9-982ea9f30a86",
}

# Rows selectable as data sources: real SQL databases plus the Power BI ``.pbit``
# datasets we bridge above. Kept as a single predicate so list/get stay in sync.
_SELECTABLE_ROWS_WHERE = (
    "(category = 'database' "
    "OR (category = 'dashboard' AND lower(service_type) = 'powerbisemanticfile'))"
)


class ConnectionNotFound(Exception):
    """Raised when a requested `source_key` is missing or inactive."""


@dataclass
class Connection:
    """A row from `public.settings_services` (without secrets)."""

    id: int
    source_key: str  # = settings_services.name; matches metadata_*.source
    display_name: str
    description: Optional[str]
    service_type: str  # e.g. 'Postgres', 'Mysql', 'Snowflake'
    database_type: str  # normalized lower-case alias of service_type for the prompt
    connection_host: Optional[str]
    connection_port: Optional[int]
    connection_database: Optional[str]
    connection_catalog: Optional[str]
    connection_http_path: Optional[str]
    db_schema: Optional[str]
    enable_ssl: bool
    is_active: bool
    # ── Power BI (text-to-DAX) — non-secret dataset identifiers ─────────────
    # Populated only for `service_type='powerbi'` connections; None otherwise.
    is_power_bi: bool = False
    workspace_id: Optional[str] = None
    dataset_id: Optional[str] = None
    model_version: Optional[str] = None

    def to_public_dict(self) -> dict:
        """Return a dict safe to send to the UI (no secrets)."""
        return {
            "id": self.id,
            "source_key": self.source_key,
            "display_name": self.display_name,
            "description": self.description,
            "service_type": self.service_type,
            "database_type": self.database_type,
            "connection_host": self.connection_host,
            "connection_port": self.connection_port,
            "connection_database": self.connection_database,
            "connection_catalog": self.connection_catalog,
            "connection_http_path": self.connection_http_path,
            "db_schema": self.db_schema,
            "enable_ssl": self.enable_ssl,
            "is_active": self.is_active,
            "is_power_bi": self.is_power_bi,
            "workspace_id": self.workspace_id,
            "dataset_id": self.dataset_id,
            "model_version": self.model_version,
        }


# ----------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------
class ConnectionService:
    """Reads `settings_services` and lazily builds per-connection SQL runners."""

    def __init__(self, metadata_pool: asyncpg.Pool):
        self.pool = metadata_pool
        self._runners: Dict[str, SqlRunner] = {}
        self._runner_locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def list_connections(self) -> List[Connection]:
        """List active database connections from `settings_services`."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, description, service_type, connection_config, is_active
                FROM public.settings_services
                WHERE {_SELECTABLE_ROWS_WHERE} AND is_active = TRUE
                ORDER BY name
                """
            )
        return [self._row_to_connection(r) for r in rows]

    async def get_connection(self, source_key: str) -> Connection:
        """Fetch one active connection by `name`. Raises `ConnectionNotFound`."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT id, name, description, service_type, connection_config, is_active
                FROM public.settings_services
                WHERE {_SELECTABLE_ROWS_WHERE} AND name = $1
                """,
                source_key,
            )
        if row is None:
            raise ConnectionNotFound(
                f"No connection found for source_key={source_key!r} (category=database)"
            )
        return self._row_to_connection(row)

    async def get_runner(self, source_key: str) -> SqlRunner:
        """Return a lazily-initialized `SqlRunner` for `source_key`.

        Power BI connections have no ``SqlRunner`` (they are served by the
        text-to-DAX agent), so this raises ``UnsupportedConnectionType``. In
        practice ``resolve_agent`` dispatches Power BI before ever calling here.
        """
        if source_key in self._runners:
            return self._runners[source_key]

        # Per-source lock so concurrent first-touches don't double-build the runner.
        lock = self._runner_locks.setdefault(source_key, asyncio.Lock())
        async with lock:
            if source_key in self._runners:
                return self._runners[source_key]
            row = await self._fetch_full_row(source_key)
            if is_power_bi_service_type(row.get("service_type")):
                raise UnsupportedConnectionType(
                    f"Connection {source_key!r} is a Power BI dataset (text-to-DAX); "
                    "it has no SqlRunner."
                )
            runner = await self._build_runner(row)
            self._runners[source_key] = runner
            return runner

    async def close(self) -> None:
        """Close all cached runners."""
        for runner in self._runners.values():
            try:
                await runner.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing data-source runner")
        self._runners.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _fetch_full_row(self, source_key: str) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT id, name, description, service_type, connection_config, is_active
                FROM public.settings_services
                WHERE {_SELECTABLE_ROWS_WHERE} AND name = $1
                """,
                source_key,
            )
        if row is None:
            raise ConnectionNotFound(
                f"No connection found for source_key={source_key!r} (category=database)"
            )
        return dict(row)

    def _row_to_connection(self, row) -> Connection:
        cfg = decode_config(row["connection_config"])
        service_type = row["service_type"] or ""

        # Power BI datasets are not SqlRunners — surface their delegated-OAuth
        # dataset identifiers and skip the SQL connection-field parsing entirely.
        if is_power_bi_service_type(service_type):
            pbi = power_bi_connection_fields(cfg)
            # TEMPORARY: offline .pbit sources have no live workspace/dataset id
            # in connection_config, so fall back to the hardcoded map keyed by
            # source_key (settings_services.name). Real 'powerbi' connections
            # that already carry the ids keep using them. Remove with _TEMP_PBI_*.
            workspace_id = pbi["workspace_id"] or _TEMP_PBI_WORKSPACE_ID
            dataset_id = pbi["dataset_id"] or _TEMP_PBI_DATASETS.get(row["name"])
            if not pbi["dataset_id"] and dataset_id:
                logger.info(
                    "Power BI source_key=%s using hardcoded workspace/dataset "
                    "(workspace=%s dataset=%s) — TEMPORARY until stored in schema",
                    row["name"], workspace_id, dataset_id,
                )
            return Connection(
                id=row["id"],
                source_key=row["name"],
                display_name=row["name"],
                description=row["description"],
                service_type=service_type,
                database_type="powerbi",
                connection_host=None,
                connection_port=None,
                connection_database=None,
                connection_catalog=None,
                connection_http_path=None,
                db_schema=None,
                enable_ssl=True,
                is_active=row["is_active"],
                is_power_bi=True,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                model_version=pbi["model_version"],
            )
        try:
            fields = public_connection_fields(cfg, service_type)
            database_type = fields["database_type"]
        except UnsupportedConnectionType:
            # Listing connections should remain best-effort even if an inactive
            # future connector type is present. Selecting it still returns 501
            # when the runner is built.
            fields = {
                "host": coerce_str(cfg.get("host")),
                "port": coerce_int(cfg.get("port")),
                "database": coerce_str(cfg.get("database")),
                "catalog": coerce_str(cfg.get("catalog")),
                "schema": coerce_str(cfg.get("databaseSchema") or cfg.get("schema")),
                "http_path": coerce_str(cfg.get("httpPath") or cfg.get("http_path")),
                "enable_ssl": coerce_bool(cfg.get("ssl"), default=True),
            }
            database_type = service_type.lower()
        return Connection(
            id=row["id"],
            source_key=row["name"],
            display_name=row["name"],
            description=row["description"],
            service_type=service_type,
            database_type=database_type,
            connection_host=fields["host"],
            connection_port=fields["port"],
            connection_database=fields["database"],
            connection_catalog=fields["catalog"],
            connection_http_path=fields["http_path"],
            db_schema=fields["schema"],
            enable_ssl=fields["enable_ssl"],
            is_active=row["is_active"],
        )

    async def _build_runner(self, row: dict) -> SqlRunner:
        """Build a `SqlRunner` from a `settings_services` row."""
        runner = await build_sql_runner(row)
        cfg = decode_config(row.get("connection_config"))
        fields = public_connection_fields(cfg, row.get("service_type"))
        logger.info(
            "Built data-source runner source_key=%s database_type=%s host=%s port=%s catalog=%s schema=%s",
            row["name"],
            getattr(runner, "database_type", row.get("service_type")),
            fields.get("host"),
            fields.get("port"),
            fields.get("catalog"),
            fields.get("schema"),
        )
        return runner
