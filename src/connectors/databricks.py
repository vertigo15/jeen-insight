"""Databricks SQL data-source runner."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorSyntaxError,
    QueryTimeout,
    SqlRunner,
)


class DatabricksSqlRunner(SqlRunner):
    """Databricks SQL runner backed by ``databricks-sql-connector``.

    The connector is synchronous, so this runner opens a short-lived connection
    per operation in a bounded executor. That avoids cross-thread connection
    sharing and keeps lifecycle management simple.
    """

    database_type = "databricks"
    sqlglot_dialect = "databricks"

    def __init__(
        self,
        *,
        source_key: Optional[str],
        host: str,
        http_path: str,
        access_token: str,
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
        timeout_seconds: float = 30.0,
        max_workers: int = 4,
    ) -> None:
        self.source_key = source_key
        self.host = _normalize_host(host)
        self.http_path = http_path
        self.access_token = access_token
        self.catalog = catalog
        self.schema = schema
        self.timeout_seconds = timeout_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"jeen-dbx-{source_key or 'source'}",
        )

    async def initialize(self) -> None:
        # Connections are opened per query to avoid sharing sync DB-API objects
        # across threads.
        return None

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _execute(
        self, sql: str, statement_timeout_ms: int
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        timeout = _timeout_seconds(statement_timeout_ms, self.timeout_seconds)
        try:
            return await asyncio.wait_for(
                self._run_blocking(lambda cur: _fetch_rows(cur, sql)),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise QueryTimeout(
                "Databricks SQL query exceeded the configured timeout."
            ) from exc

    async def list_tables(self) -> List[str]:
        if not self.schema:
            return []
        sql = (
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = {_sql_literal(self.schema)} "
            "AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_name"
        )
        try:
            _, rows = await self._run_blocking(lambda cur: _fetch_rows(cur, sql))
            return [str(row["table_name"]) for row in rows]
        except Exception:
            return []

    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        schema, table = self._split_table_name(table_name)
        if not schema or not table:
            return []
        sql = (
            "SELECT column_name, data_type, is_nullable, NULL AS column_default "
            "FROM information_schema.columns "
            f"WHERE table_schema = {_sql_literal(schema)} "
            f"AND table_name = {_sql_literal(table)} "
            "ORDER BY ordinal_position"
        )
        try:
            _, rows = await self._run_blocking(lambda cur: _fetch_rows(cur, sql))
            return rows
        except Exception:
            return []

    async def _run_blocking(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._with_cursor, fn)

    def _with_cursor(self, fn):
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                try:
                    return fn(cur)
                finally:
                    cur.close()
            finally:
                conn.close()
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _classify_databricks_error(exc) from exc

    def _connect(self):
        try:
            from databricks import sql
        except ImportError as exc:
            raise ConnectorConnectionError(
                "The 'databricks-sql-connector' package is not installed."
            ) from exc

        kwargs: Dict[str, Any] = {
            "server_hostname": self.host,
            "http_path": self.http_path,
            "access_token": self.access_token,
            "catalog": self.catalog,
            "schema": self.schema,
        }
        return sql.connect(**{k: v for k, v in kwargs.items() if v is not None})

    def _split_table_name(self, table_name: str) -> tuple[Optional[str], str]:
        cleaned = (table_name or "").strip().strip("`")
        parts = [p.strip("`") for p in cleaned.split(".") if p.strip("`")]
        if len(parts) >= 3:
            return parts[-2], parts[-1]
        if len(parts) == 2:
            return parts[0], parts[1]
        return self.schema, parts[0] if parts else ""


def _fetch_rows(cur, sql: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    cur.execute(sql)
    columns = [col[0] for col in (cur.description or [])]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return columns, rows


def _timeout_seconds(statement_timeout_ms: int, default_timeout: float) -> float:
    if statement_timeout_ms and statement_timeout_ms > 0:
        return max(1.0, statement_timeout_ms / 1000.0)
    return max(1.0, default_timeout)


def _normalize_host(host: str) -> str:
    cleaned = (host or "").strip()
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    return cleaned.rstrip("/")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _classify_databricks_error(exc: Exception) -> ConnectorError:
    msg = str(exc)
    lower = msg.lower()
    if "authentication" in lower or "unauthorized" in lower or "401" in lower:
        return ConnectorAuthError("Databricks authentication failed.")
    if "permission" in lower or "access denied" in lower or "403" in lower:
        return ConnectorPermissionError(msg)
    if "syntax" in lower or "parse" in lower or "analysisexception" in lower:
        return ConnectorSyntaxError(msg)
    if "timed out" in lower or "timeout" in lower:
        return QueryTimeout(msg)
    if "connection" in lower or "host" in lower or "network" in lower:
        return ConnectorConnectionError(msg)
    return ConnectorError(msg)
