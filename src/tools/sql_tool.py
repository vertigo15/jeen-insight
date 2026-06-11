"""SQL execution tool for PostgreSQL."""

import logging
import re
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)

# Statements the agent is allowed to send to a user data source.
# Anything else — INSERT, UPDATE, DELETE, DDL, COPY, GRANT, etc. — is rejected
# before we ever reach the database. The read-only transaction wrapper inside
# `run_sql` is a second line of defence that catches anything this regex misses
# (e.g. a function call that mutates state).
_ALLOWED_LEAD_KEYWORDS = re.compile(
    r"^(SELECT|WITH)\b",
    re.IGNORECASE,
)
# Strip leading SQL comments + whitespace so a query that starts with
# "-- comment\nSELECT ..." or "/* foo */ SELECT ..." still passes the
# leading-keyword check.
_LEADING_COMMENTS = re.compile(
    r"\s*(?:--[^\n]*\n|/\*.*?\*/)\s*",
    re.DOTALL,
)


def _strip_leading_noise(sql: str) -> str:
    """Drop leading whitespace and any chained leading SQL comments."""
    text = sql or ""
    while True:
        new_text = _LEADING_COMMENTS.sub("", text, count=1)
        if new_text == text:
            break
        text = new_text
    return text.lstrip()


def is_read_only_sql(sql: str) -> bool:
    """Return True iff `sql` starts with SELECT or WITH (after stripping comments).

    This is intentionally strict: the agent's contract is to produce read-only
    queries, and the safest place to enforce that is here, before the SQL
    reaches the connection pool.
    """
    if not sql or not sql.strip():
        return False
    cleaned = _strip_leading_noise(sql)
    return bool(_ALLOWED_LEAD_KEYWORDS.match(cleaned))


class PostgresSqlRunner:
    """
    PostgreSQL SQL runner for executing queries.

    One instance is built per active connection (see ConnectionService),
    so the database it talks to depends on which connection the caller
    selected — it is not pinned to any specific data source.
    """
    
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.pool: Optional[asyncpg.Pool] = None
    
    async def initialize(self):
        """Initialize connection pool."""
        self.pool = await asyncpg.create_pool(
            self.connection_string,
            min_size=2,
            max_size=10
        )
    
    async def close(self):
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
    
    async def run_sql(
        self,
        sql: str,
        limit: Optional[int] = 100,
        max_rows: int = 10000,
        statement_timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        """Execute a read-only SQL query and return its result rows.

        Three layers of safety:

        1. **Pre-check**: only SQL whose leading keyword (after stripping
           comments) is ``SELECT`` or ``WITH`` is allowed through. Anything
           else is rejected without ever touching the connection pool.
        2. **Hard row cap**: the query is wrapped in an outer
           ``SELECT * FROM (<query>) LIMIT n`` where ``n`` never exceeds
           ``max_rows``. This caps results even when the inner SQL already
           has its own (possibly huge) LIMIT, and is robust against the
           comment/casing tricks a plain string check would miss.
        3. **Read-only transaction + statement timeout**: the query runs
           inside a Postgres ``READ ONLY`` transaction with a per-statement
           ``statement_timeout`` so a runaway query can't exhaust the pool.
           If a function call or anything the pre-check missed tries to
           mutate state, Postgres raises an error and the transaction rolls
           back automatically.

        Note: for the strongest possible guarantee, also connect with a
        Postgres role whose only privilege on the schema is ``SELECT``.
        """
        if not is_read_only_sql(sql):
            logger.warning(
                "run_sql: blocked non-read-only SQL (first 80 chars): %s",
                (sql or "").strip()[:80],
            )
            return {
                "error": (
                    "Only read-only queries are allowed. The query must start "
                    "with SELECT or WITH."
                ),
                "columns": [],
                "rows": [],
                "row_count": 0,
            }

        # Enforce a hard row cap. The requested ``limit`` is honoured but can
        # never exceed ``max_rows``. Wrapping the query in an outer LIMIT means
        # the cap holds even if the model emitted its own LIMIT or none at all.
        effective_limit = max_rows if max_rows and max_rows > 0 else (limit or 0)
        if limit and limit > 0:
            effective_limit = min(limit, effective_limit) if effective_limit else limit
        inner = sql.rstrip().rstrip(";").rstrip()
        if effective_limit and effective_limit > 0:
            sql = f"SELECT * FROM (\n{inner}\n) AS _jeen_capped LIMIT {int(effective_limit)}"
        else:
            sql = inner

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    if statement_timeout_ms and statement_timeout_ms > 0:
                        # SET LOCAL scopes the timeout to this transaction only.
                        await conn.execute(
                            f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"
                        )
                    rows = await conn.fetch(sql)

            if not rows:
                return {"columns": [], "rows": [], "row_count": 0}

            columns = list(rows[0].keys())
            result_rows = [dict(row) for row in rows]
            return {
                "columns": columns,
                "rows": result_rows,
                "row_count": len(result_rows),
            }
        except asyncpg.exceptions.QueryCanceledError as e:
            # statement_timeout fired before the query finished.
            logger.warning("run_sql: query cancelled by statement_timeout: %s", e)
            return {
                "error": (
                    "The query took too long and was cancelled. Try narrowing "
                    "it (add filters or a smaller date range) or contact an "
                    "admin to raise the statement timeout."
                ),
                "columns": [],
                "rows": [],
                "row_count": 0,
            }
        except asyncpg.exceptions.ReadOnlySQLTransactionError as e:
            # The READ ONLY transaction rejected something the pre-check
            # accepted (e.g. a SELECT that calls a function with side effects).
            logger.warning("run_sql: read-only transaction rejected query: %s", e)
            return {
                "error": (
                    "This query attempted to modify the database and was "
                    "blocked. Only read-only queries are allowed."
                ),
                "columns": [],
                "rows": [],
                "row_count": 0,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "error": str(e),
                "columns": [],
                "rows": [],
                "row_count": 0,
            }
    
    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a table."""
        sql = """
            SELECT 
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY ordinal_position
        """
        
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql, table_name)
                return [dict(row) for row in rows]
        except Exception as e:
            return []
    
    async def list_tables(self) -> List[str]:
        """List all tables in the database."""
        sql = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """
        
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql)
                return [row['table_name'] for row in rows]
        except Exception as e:
            return []


class RunSqlTool:
    """
    Tool wrapper for SQL execution, for the LLM's function-calling interface.

    Wraps a `PostgresSqlRunner` bound to a specific active connection. The
    tool description sent to the LLM is built from the connection's display
    name + database type so the model knows which database it is querying.
    """

    def __init__(
        self,
        sql_runner: PostgresSqlRunner,
        connection_display_name: Optional[str] = None,
        database_type: Optional[str] = None,
    ):
        self.sql_runner = sql_runner
        self.name = "run_sql"
        # Build a connection-aware description; fall back to a neutral one.
        if connection_display_name:
            db_type = (database_type or "sql").strip() or "sql"
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
                            "description": "The SQL query to execute"
                        }
                    },
                    "required": ["sql"]
                }
            }
        }
    
    async def execute(self, sql: str, **kwargs) -> Dict[str, Any]:
        """Execute the SQL query."""
        return await self.sql_runner.run_sql(sql)
