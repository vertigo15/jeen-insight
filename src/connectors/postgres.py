"""PostgreSQL data-source runner."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorPermissionError,
    ConnectorSyntaxError,
    QueryTimeout,
    ReadOnlyViolation,
    SqlRunner,
)


class PostgresSqlRunner(SqlRunner):
    """Async PostgreSQL runner backed by an ``asyncpg`` connection pool."""

    database_type = "postgres"
    sqlglot_dialect = "postgres"

    def __init__(
        self,
        *,
        connection_string: Optional[str] = None,
        source_key: Optional[str] = None,
        host: Optional[str] = None,
        port: int = 5432,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        enable_ssl: bool = True,
        schema: Optional[str] = None,
        min_size: int = 2,
        max_size: int = 10,
    ) -> None:
        self.connection_string = connection_string
        self.source_key = source_key
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.enable_ssl = enable_ssl
        self.schema = schema or "public"
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Optional[asyncpg.Pool] = None

    async def initialize(self) -> None:
        if self.connection_string:
            self.pool = await asyncpg.create_pool(
                self.connection_string,
                min_size=self.min_size,
                max_size=self.max_size,
            )
            return

        self.pool = await asyncpg.create_pool(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.username,
            password=self.password,
            ssl="require" if self.enable_ssl else None,
            min_size=self.min_size,
            max_size=self.max_size,
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _execute(
        self, sql: str, statement_timeout_ms: int
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        if not self.pool:
            raise ConnectorConnectionError("PostgreSQL runner is not initialized.")

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    if statement_timeout_ms and statement_timeout_ms > 0:
                        await conn.execute(
                            f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"
                        )
                    rows = await conn.fetch(sql)
        except asyncpg.exceptions.QueryCanceledError as exc:
            raise QueryTimeout(str(exc)) from exc
        except asyncpg.exceptions.ReadOnlySQLTransactionError as exc:
            raise ReadOnlyViolation(str(exc)) from exc
        except asyncpg.exceptions.InsufficientPrivilegeError as exc:
            raise ConnectorPermissionError(str(exc)) from exc
        except asyncpg.exceptions.InvalidPasswordError as exc:
            raise ConnectorAuthError("PostgreSQL authentication failed.") from exc
        except asyncpg.exceptions.SyntaxOrAccessError as exc:
            raise ConnectorSyntaxError(str(exc)) from exc
        except OSError as exc:
            raise ConnectorConnectionError(str(exc)) from exc

        if not rows:
            return [], []
        columns = list(rows[0].keys())
        return columns, [dict(row) for row in rows]

    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a table from ``information_schema``."""
        if not self.pool:
            return []

        schema, table = self._split_table_name(table_name)
        sql = """
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql, schema, table)
                return [dict(row) for row in rows]
        except Exception:
            return []

    async def list_tables(self) -> List[str]:
        """List visible tables in the configured schema."""
        if not self.pool:
            return []

        sql = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = $1
              AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_name
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql, self.schema)
                return [row["table_name"] for row in rows]
        except Exception:
            return []

    def _split_table_name(self, table_name: str) -> tuple[str, str]:
        cleaned = (table_name or "").strip().strip('"')
        if "." in cleaned:
            schema, _, table = cleaned.rpartition(".")
            return schema.strip('"') or self.schema, table.strip('"')
        return self.schema, cleaned
