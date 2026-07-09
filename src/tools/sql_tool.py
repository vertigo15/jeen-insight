"""SQL tool compatibility exports.

Query execution now lives in ``src.connectors`` so multiple data sources can
share one safety contract. This module remains as the stable import location
for existing code/tests that import ``RunSqlTool``, ``PostgresSqlRunner`` or
``is_read_only_sql``.
"""

from typing import Any, Dict, Optional

from src.connectors import PostgresSqlRunner, SqlRunner, is_read_only_sql


class RunSqlTool:
    """
    Tool wrapper for SQL execution, for the LLM's function-calling interface.

    Wraps a `SqlRunner` bound to a specific active connection. The tool
    description sent to the LLM is built from the connection's display
    name + database type so the model knows which database it is querying.
    """

    def __init__(
        self,
        sql_runner: Optional[SqlRunner],
        connection_display_name: Optional[str] = None,
        database_type: Optional[str] = None,
        source_key: Optional[str] = None,
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
    ):
        self.sql_runner = sql_runner
        self.name = "run_sql"
        self.database_type = (database_type or "sql").strip() or "sql"
        self.source_key = (source_key or "").strip()
        self.catalog = (catalog or "").strip()
        self.schema = (schema or "").strip()
        # Build a connection-aware description; fall back to a neutral one.
        if connection_display_name:
            db_type = self.database_type
            self.description = (
                f"Execute a read-only SQL query against the "
                f"{connection_display_name} ({db_type}) database."
            )
        else:
            self.description = (
                "Execute a read-only SQL query against the active database."
            )

    def get_schema(self) -> Dict[str, Any]:
        """Return tool schema for LLM function calling."""
        target_parts = []
        if self.source_key:
            target_parts.append(f"source_key={self.source_key}")
        if self.catalog:
            target_parts.append(f"catalog={self.catalog}")
        if self.schema:
            target_parts.append(f"schema={self.schema}")
        target_hint = f" Target: {', '.join(target_parts)}." if target_parts else ""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": (
                                f"A single read-only SELECT or WITH statement in "
                                f"{self.database_type} dialect. Do not include a "
                                f"trailing semicolon, markdown fences, comments "
                                f"outside the SQL, or multiple statements.{target_hint}"
                            ),
                        }
                    },
                    "required": ["sql"],
                },
            },
        }

    async def execute(self, sql: str, **kwargs) -> Dict[str, Any]:
        """Execute the SQL query."""
        if self.sql_runner is None:
            raise RuntimeError("RunSqlTool has no sql_runner bound for execution.")
        return await self.sql_runner.run_sql(sql)


__all__ = ["PostgresSqlRunner", "RunSqlTool", "SqlRunner", "is_read_only_sql"]
