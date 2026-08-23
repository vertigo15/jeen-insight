"""Connector abstraction shared by every data-source engine.

``SqlRunner`` is the single interface the rest of Jeen Insights depends on to
execute read-only SQL and introspect a data source. Concrete engines
(PostgreSQL, Trino, Databricks, …) subclass it and implement a small amount of
engine-specific I/O; all the safety machinery (SELECT-only gate, hard row cap,
uniform error shaping) lives here so it can never drift between engines.

Design: ``run_sql`` is a *template method*. It performs the read-only pre-check,
wraps the query in an outer ``LIMIT`` and calls the subclass hook ``_execute``,
which is the only method each engine must implement for query execution. Engines
raise :class:`QueryTimeout` / :class:`ReadOnlyViolation` from ``_execute`` and
the base turns them into the same friendly, UI-ready error payloads for every
engine.
"""

from __future__ import annotations

import abc
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────
class UnsupportedConnectionType(Exception):
    """Raised when a ``service_type`` has no registered connector."""


class ConnectorError(Exception):
    """Base connector exception with a stable public error category."""

    error_type = "execution_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.error_type)


class QueryTimeout(ConnectorError):
    """Raised by a connector when a query exceeds its statement timeout."""

    error_type = "timeout"


class ReadOnlyViolation(ConnectorError):
    """Raised by a connector when a query tried to mutate state."""

    error_type = "read_only_blocked"


class ConnectorAuthError(ConnectorError):
    """Raised by a connector for authentication/authorization failures."""

    error_type = "auth_error"


class ConnectorPermissionError(ConnectorError):
    """Raised by a connector for permission/privilege failures."""

    error_type = "permission_error"


class ConnectorConnectionError(ConnectorError):
    """Raised by a connector for network/connection failures."""

    error_type = "connection_error"


class ConnectorSyntaxError(ConnectorError):
    """Raised by a connector for SQL syntax/analysis failures."""

    error_type = "syntax_error"


# ── Read-only gate (engine-agnostic) ──────────────────────────────────────────
# Only SQL whose first keyword (after stripping comments) is SELECT or WITH is
# allowed through. Everything else — INSERT/UPDATE/DELETE/DDL/COPY/GRANT — is
# rejected before it ever reaches a driver.
_ALLOWED_LEAD_KEYWORDS = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)
_LEADING_COMMENTS = re.compile(r"\s*(?:--[^\n]*\n|/\*.*?\*/)\s*", re.DOTALL)


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
    """Return True iff ``sql`` starts with SELECT or WITH (after stripping comments)."""
    if not sql or not sql.strip():
        return False
    cleaned = _strip_leading_noise(sql)
    return bool(_ALLOWED_LEAD_KEYWORDS.match(cleaned))


# ── Structural single-statement enforcement ───────────────────────────────────
# The leading-keyword gate above is necessary but not sufficient: statements
# like ``SELECT 1; DELETE FROM t`` or ``WITH x AS (DELETE ... RETURNING *)
# SELECT * FROM x`` also start with SELECT/WITH. Postgres blocks these via its
# READ ONLY transaction, but Trino/Databricks have no equivalent, so we enforce
# the invariant structurally (parse-based) for every engine: exactly one
# top-level read query, with no DML/DDL anywhere in the tree (incl. CTEs).

#: sqlglot expression types that represent a data mutation or DDL.
_MUTATION_EXP_NAMES = (
    "Insert", "Update", "Delete", "Merge",
    "Create", "Drop", "Alter", "AlterTable", "TruncateTable",
    "Command", "Copy",
)
#: sqlglot expression types allowed as the single top-level statement.
_QUERY_EXP_NAMES = ("Select", "Union", "Intersect", "Except", "Subquery")

# Conservative textual fallback used only when sqlglot is unavailable/unparseable.
_FORBIDDEN_KEYWORD_RE = re.compile(
    r"(?is)\b(insert|update|delete|merge|upsert|create|drop|alter|truncate|"
    r"grant|revoke|copy|call|vacuum|reindex|attach|detach)\b"
)


def _sqlglot_exp_types(names: Tuple[str, ...]) -> tuple:
    try:
        from sqlglot import exp
    except ImportError:
        return ()
    return tuple(t for t in (getattr(exp, n, None) for n in names) if t is not None)


def check_read_only_statements(statements: List[Any]) -> Optional[str]:
    """Validate a list of parsed sqlglot statements as one read-only query.

    Returns an error message when the SQL is not exactly one read query with no
    mutation/DDL anywhere (including inside CTEs), else ``None``. Shared by the
    ``sqlglot_validate`` graph node and the runner so the rule can't drift.
    """
    real = [s for s in statements if s is not None]
    if not real:
        return "No executable SQL statement was found."
    if len(real) > 1:
        return (
            "Only a single SQL statement may be executed. "
            "Multiple statements are not allowed."
        )
    stmt = real[0]
    query_types = _sqlglot_exp_types(_QUERY_EXP_NAMES)
    if query_types and not isinstance(stmt, query_types):
        return "Only read-only SELECT queries are allowed."
    mutation_types = _sqlglot_exp_types(_MUTATION_EXP_NAMES)
    if mutation_types and any(True for _ in stmt.find_all(*mutation_types)):
        return (
            "Data-modifying statements (INSERT/UPDATE/DELETE/MERGE/DDL) are not "
            "allowed, including inside CTEs."
        )
    return None


def _assert_read_only_query_textual(sql: str) -> Optional[str]:
    """Conservative structural check for when sqlglot can't parse the SQL."""
    cleaned = _strip_leading_noise(sql or "")
    without_trailing = cleaned.rstrip().rstrip(";").rstrip()
    if ";" in without_trailing:
        return (
            "Only a single SQL statement may be executed. "
            "Multiple statements are not allowed."
        )
    if _FORBIDDEN_KEYWORD_RE.search(cleaned):
        return "Data-modifying statements (INSERT/UPDATE/DELETE/DDL) are not allowed."
    return None


def assert_read_only_query(sql: str, dialect: Optional[str] = None) -> Optional[str]:
    """Return an error message if *sql* is not exactly one read-only query.

    Parses with sqlglot when available; falls back to a conservative textual
    scan otherwise. Engine-agnostic and safe to call before every execution.
    """
    try:
        import sqlglot
        import sqlglot.errors
    except ImportError:
        return _assert_read_only_query_textual(sql)
    try:
        statements = sqlglot.parse(
            sql, dialect=dialect, error_level=sqlglot.errors.ErrorLevel.RAISE
        )
    except Exception:  # noqa: BLE001 — unparseable → conservative textual fallback
        return _assert_read_only_query_textual(sql)
    return check_read_only_statements(statements)


# ── Result helpers ────────────────────────────────────────────────────────────
def empty_result() -> Dict[str, Any]:
    return {"columns": [], "rows": [], "row_count": 0}


def error_result(message: str, *, error_type: str = "execution_error") -> Dict[str, Any]:
    return {
        "error": message,
        "error_type": error_type,
        "columns": [],
        "rows": [],
        "row_count": 0,
    }


class SqlRunner(abc.ABC):
    """Abstract read-only SQL runner for one active data-source connection.

    Subclasses set :attr:`database_type` / :attr:`sqlglot_dialect` and implement
    :meth:`initialize`, :meth:`close`, :meth:`_execute`, :meth:`list_tables` and
    :meth:`get_table_schema`. They inherit the safety-hardened :meth:`run_sql`.
    """

    #: Normalised engine name, e.g. "postgres" | "trino" | "databricks".
    database_type: str = "sql"
    #: sqlglot dialect used to validate generated SQL (see ``dialects``).
    sqlglot_dialect: Optional[str] = None
    #: Source key used in logs; set by concrete runners/factory.
    source_key: Optional[str] = None

    # -- lifecycle -------------------------------------------------------------
    @abc.abstractmethod
    async def initialize(self) -> None:
        """Open pools / validate connectivity. Call once before first query."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release all resources held by this runner."""

    # -- engine hook -----------------------------------------------------------
    @abc.abstractmethod
    async def _execute(
        self, sql: str, statement_timeout_ms: int
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Execute already-validated, row-capped read-only ``sql``.

        Returns ``(columns, rows)`` where ``rows`` is a list of dicts keyed by
        column name. Must raise :class:`QueryTimeout` when the statement timeout
        fires and :class:`ReadOnlyViolation` when the engine rejects a mutation.
        """

    # -- introspection ---------------------------------------------------------
    @abc.abstractmethod
    async def list_tables(self) -> List[str]:
        """Return the table names visible to this connection (best-effort)."""

    @abc.abstractmethod
    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Return column metadata for ``table_name`` (best-effort)."""

    # -- template method (shared, safety-critical) -----------------------------
    async def run_sql(
        self,
        sql: str,
        limit: Optional[int] = 100,
        max_rows: int = 10000,
        statement_timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        """Run a read-only query and return ``{columns, rows, row_count}``.

        Safety layers (identical for every engine):

        1. **Pre-check** — only leading ``SELECT``/``WITH`` is allowed through.
        2. **Hard row cap** — the query is wrapped in ``SELECT * FROM (<q>) LIMIT n``
           so results are capped even if the model omitted (or inflated) a LIMIT.
        3. **Engine enforcement** — the subclass adds a read-only transaction
           and/or statement timeout in :meth:`_execute`.
        """
        if not is_read_only_sql(sql):
            logger.warning(
                "run_sql[%s]: blocked non-read-only SQL (first 80 chars): %s",
                self.database_type,
                (sql or "").strip()[:80],
            )
            return error_result(
                "Only read-only queries are allowed. The query must start "
                "with SELECT or WITH.",
                error_type="read_only_blocked",
            )

        structural_error = assert_read_only_query(sql, self.sqlglot_dialect)
        if structural_error:
            logger.warning(
                "run_sql[%s]: blocked unsafe SQL structure (%s): %s",
                self.database_type,
                structural_error,
                (sql or "").strip()[:120],
            )
            return error_result(structural_error, error_type="read_only_blocked")

        effective_limit = max_rows if max_rows and max_rows > 0 else (limit or 0)
        if limit and limit > 0:
            effective_limit = min(limit, effective_limit) if effective_limit else limit
        # Fetch one sentinel row beyond the visible cap. This proves whether the
        # result was truncated without running a separate COUNT query.
        probe_limit = effective_limit + 1 if effective_limit else 0
        capped_sql = self._apply_row_cap(
            sql,
            probe_limit or limit,
            probe_limit or max_rows,
        )
        t0 = time.monotonic()

        try:
            columns, rows = await self._execute(capped_sql, statement_timeout_ms)
        except ConnectorError as exc:
            if isinstance(exc, QueryTimeout):
                logger.warning(
                    "run_sql[%s]: query cancelled by timeout: %s",
                    self.database_type,
                    _sanitize_error(str(exc)),
                )
                return error_result(
                    "The query took too long and was cancelled. Try narrowing it "
                    "(add filters or a smaller date range) or contact an admin to "
                    "raise the statement timeout.",
                    error_type=exc.error_type,
                )
            if isinstance(exc, ReadOnlyViolation):
                logger.warning(
                    "run_sql[%s]: read-only guard rejected query: %s",
                    self.database_type,
                    _sanitize_error(str(exc)),
                )
                return error_result(
                    "This query attempted to modify the database and was blocked. "
                    "Only read-only queries are allowed.",
                    error_type=exc.error_type,
                )
            logger.warning("run_sql[%s]: connector error: %s", self.database_type, exc)
            return error_result(
                _sanitize_error(str(exc)),
                error_type=exc.error_type,
            )
        except Exception as exc:  # noqa: BLE001
            safe_message = _sanitize_error(str(exc))
            logger.warning("run_sql[%s]: execution error: %s", self.database_type, safe_message)
            return error_result(safe_message)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "run_sql source_key=%s database_type=%s timeout_ms=%s requested_limit=%s max_rows=%s elapsed_ms=%s row_count=%s",
            self.source_key,
            self.database_type,
            statement_timeout_ms,
            limit,
            max_rows,
            elapsed_ms,
            len(rows or []),
        )

        truncated = bool(effective_limit and len(rows or []) > effective_limit)
        if truncated:
            rows = list(rows[:effective_limit])
        if not rows:
            return {"columns": columns or [], "rows": [], "row_count": 0}
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "cap": effective_limit or None,
        }

    # -- shared helpers --------------------------------------------------------
    @staticmethod
    def _apply_row_cap(sql: str, limit: Optional[int], max_rows: int) -> str:
        """Wrap ``sql`` in an outer LIMIT that never exceeds ``max_rows``.

        The wrapper (``SELECT * FROM (<inner>) AS _jeen_capped LIMIT n``) is
        valid ANSI SQL and works across PostgreSQL, Trino and Spark/Databricks.
        """
        effective_limit = max_rows if max_rows and max_rows > 0 else (limit or 0)
        if limit and limit > 0:
            effective_limit = min(limit, effective_limit) if effective_limit else limit
        inner = (sql or "").rstrip().rstrip(";").rstrip()
        if effective_limit and effective_limit > 0:
            return f"SELECT * FROM (\n{inner}\n) AS _jeen_capped LIMIT {int(effective_limit)}"
        return inner


_SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|pwd|token|access_token|api_key|secret)=([^&\s,;]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s,;]+"),
]


def _sanitize_error(message: str) -> str:
    """Return an error string safe for API responses and logs."""
    text = message or "Query execution failed."
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text[:1000]
