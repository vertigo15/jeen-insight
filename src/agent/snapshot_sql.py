"""Compute over prior results with SQL in the metadata PostgreSQL, without an LLM
doing arithmetic.

A follow-up such as "what was the max?", "sort it by revenue" or "what if prices
were 10% higher?" is answered by exposing the referenced turns' stored rows as
relations — one per turn, named by its ledger handle (``insights_mem_t3``) — and
running a small, validated SELECT over them. The model only writes the SELECT;
the numbers come from Postgres over the exact stored rows.

No table is ever created. Each turn's rows travel as one JSONB bind parameter
and become a typed common table expression::

    WITH "insights_mem_t3" AS (
        SELECT * FROM jsonb_to_recordset($1::jsonb) AS t("product" TEXT, "price" NUMERIC)
    )
    SELECT * FROM (<model SQL>) AS insights_mem_result LIMIT n + 1

The model's SQL is nested as a subquery, so a ``WITH`` of its own stays inside
while our CTEs remain visible to it. Nothing is written, so nothing needs
cleaning up, no ``TEMP`` privilege is required and — unlike DDL — the statement
is legal inside a ``READ ONLY`` transaction.

The metadata database is shared with Jeen Schema Modeler (``metadata_*``,
``admin_*``); every object Insights creates there carries the ``insights_``
prefix, and the CTE names follow the same convention (``INSIGHTS_TABLE_PREFIX``)
so model SQL can never collide with, or be mistaken for, a real relation.

Safety, in layers:

* sqlglot must parse the statement as a single read-only SELECT (CTEs allowed,
  but not named like ours) whose table references are all snapshot relations —
  so the model's SQL can never reach ``insights_*`` or any other table in the
  metadata database — and that calls none of the server-side functions on the
  deny list; validation fails closed;
* everything runs inside one ``READ ONLY`` transaction that is always rolled
  back;
* ``SET LOCAL statement_timeout`` bounds the work.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Prefix of the SQL recorded for a memory computation. It marks the statement as
# a query over stored rows so it is shown to the user but never re-run at the
# customer's data source.
MEMORY_SQL_MARKER = "-- memory:"

# Naming convention of every Insights-owned object in the shared metadata DB
# (``insights_conversation_sessions``, ``insights_turn_artifacts``, …). The
# memory relations use it too: ``insights_mem_t3`` is the CTE for ledger turn
# ``T3``. The model may not name its own CTEs with this prefix.
INSIGHTS_TABLE_PREFIX = "insights_"
MEMORY_TABLE_PREFIX = f"{INSIGHTS_TABLE_PREFIX}mem_"

_DEFAULT_TIMEOUT_MS = 5000
_RESULT_ALIAS = "insights_mem_result"

# Server-side functions a SELECT over snapshot rows never needs; refusing them
# closes file-system, network and session side channels regardless of the
# database role's privileges.
_DENIED_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir",
    "pg_stat_file", "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "dblink", "dblink_connect", "dblink_exec", "dblink_open", "dblink_fetch",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_create", "lo_unlink",
    "set_config", "current_setting", "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf", "pg_rotate_logfile", "pg_notify", "pg_advisory_lock",
    "query_to_xml", "table_to_xml", "database_to_xml", "cursor_to_xml",
    # Catalog / session probes: they leak nothing from the rows but describe
    # the shared database, which a memory computation has no reason to see.
    "has_table_privilege", "has_schema_privilege", "has_database_privilege",
    "to_regclass", "to_regproc", "pg_backend_pid", "pg_stat_get_activity",
    "inet_server_addr", "inet_client_addr",
})


def is_memory_sql(sql: Any) -> bool:
    """True for SQL recorded by a memory computation (query over stored rows)."""
    return str(sql or "").lstrip().startswith(MEMORY_SQL_MARKER)


def snapshot_table_name(handle: str) -> str:
    """``T3`` → ``insights_mem_t3``: the relation (CTE) exposing a ledger turn's rows."""
    return f"{MEMORY_TABLE_PREFIX}{str(handle or '').strip().lower()}"


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


# ── Validation ────────────────────────────────────────────────────────────────


def validate_snapshot_sql(sql: str, allowed_tables: Iterable[str]) -> Optional[str]:
    """Return an error message, or None when *sql* is one read-only SELECT over
    the snapshot relations only, without denied functions.

    Fails closed: when the parser is unavailable nothing is allowed through.
    """
    text = (sql or "").strip().rstrip(";")
    if not text:
        return "The computation is empty."
    try:
        import sqlglot
        import sqlglot.errors
        from sqlglot import exp
    except ImportError:  # pragma: no cover — sqlglot is a hard dependency
        return "SQL validation is unavailable (sqlglot could not be imported); refusing to run."
    try:
        statements = sqlglot.parse(text, dialect="postgres", error_level=sqlglot.errors.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as err:
        return f"SQL syntax error: {err}"
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return "Exactly one SELECT statement is allowed."
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        return "Only SELECT statements are allowed."
    allowed = {str(t).lower() for t in allowed_tables}
    ctes = {(c.alias or "").lower() for c in stmt.find_all(exp.CTE) if c.alias}
    for cte in ctes:
        # Our relations are CTEs wrapped around the model's query; a model CTE
        # with the same prefix would shadow them and silently change the data.
        if cte.startswith(MEMORY_TABLE_PREFIX):
            return f"CTE names starting with '{MEMORY_TABLE_PREFIX}' are reserved for the stored results."
    for table in stmt.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in ctes:
            continue
        if table.db or table.catalog or name not in allowed:
            return f"Unknown table '{table.sql()}'. Only {sorted(allowed)} are available."
    for func in stmt.find_all(exp.Func):
        fname = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if fname in _DENIED_FUNCTIONS:
            return f"Function '{fname}' is not allowed in a memory computation."
    return None


# ── Table preparation ─────────────────────────────────────────────────────────


def _as_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    return None


def _column_type(values: Iterable[Any]) -> str:
    """BOOLEAN / BIGINT / NUMERIC when every non-null value fits, else TEXT."""
    seen = False
    all_bool = all_int = all_numeric = True
    for v in values:
        if v is None:
            continue
        seen = True
        if not isinstance(v, bool):
            all_bool = False
        if isinstance(v, bool) or not isinstance(v, int):
            all_int = False
        if _as_decimal(v) is None:
            all_numeric = False
        if not (all_bool or all_int or all_numeric):
            return "TEXT"
    if not seen:
        return "TEXT"
    if all_bool:
        return "BOOLEAN"
    if all_int:
        return "BIGINT"
    if all_numeric:
        return "NUMERIC"
    return "TEXT"


def _coerce(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type == "BOOLEAN":
        return bool(value)
    if pg_type == "BIGINT":
        return int(value)
    if pg_type == "NUMERIC":
        return _as_decimal(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def prepare_table(dataset: Dict[str, Any]) -> Tuple[List[str], List[str], List[Tuple[Any, ...]]]:
    """``(columns, postgres_types, records)`` for one stored result.

    Positional rows are mapped by column order; dict rows by name. Types are
    inferred per column so numbers stay numbers (``ORDER BY price`` must not
    sort text) while everything else — dates included — is TEXT the model can
    cast explicitly.
    """
    rows = list(dataset.get("rows") or [])
    columns = [str(c) for c in (dataset.get("columns") or [])]
    if not columns and rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())

    def cell(row: Any, idx: int, col: str) -> Any:
        if isinstance(row, dict):
            return row.get(col)
        try:
            return row[idx]
        except (IndexError, TypeError):
            return None

    types = [_column_type(cell(r, i, c) for r in rows) for i, c in enumerate(columns)]
    records = [
        tuple(_coerce(cell(r, i, c), types[i]) for i, c in enumerate(columns))
        for r in rows
    ]
    return columns, types, records


def schema_and_sample(dataset: Dict[str, Any], *, sample_rows: int = 3) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Columns and the first few rows of a recovered result, for the prompt."""
    columns = [str(c) for c in (dataset.get("columns") or [])]
    rows = list(dataset.get("rows") or [])[: max(0, sample_rows)]
    sample = [r if isinstance(r, dict) else dict(zip(columns, r)) for r in rows]
    return columns, sample


def wrap_for_fetch(sql: str, *, max_rows: int) -> str:
    """Cap the model's SELECT without touching its own ORDER BY / LIMIT."""
    return f"SELECT * FROM ({sql.strip().rstrip(';').strip()}) AS {_RESULT_ALIAS} LIMIT {int(max_rows) + 1}"


def _json_cell(value: Any) -> Any:
    """JSON form of a coerced cell: NUMERIC travels as text so Postgres parses it
    exactly (``jsonb_to_recordset`` feeds strings to the column type's input
    function); everything else is already JSON-native."""
    if isinstance(value, Decimal):
        return str(value)
    return value


def compose_statement(
    tables: Dict[str, Dict[str, Any]],
    sql: str,
    *,
    max_rows: int,
) -> Tuple[str, List[str]]:
    """``(statement, params)``: one SELECT exposing every stored result as a typed
    CTE over a JSONB bind parameter, with the model's SQL nested and capped.

    Raises ``ValueError`` for a relation without the ``insights_mem_`` prefix or
    without columns — a missing CTE would let the name resolve to a real table.
    """
    ctes: List[str] = []
    params: List[str] = []
    for name, dataset in tables.items():
        if not str(name).startswith(MEMORY_TABLE_PREFIX):
            raise ValueError(f"memory table '{name}' must carry the {MEMORY_TABLE_PREFIX} prefix")
        columns, types, records = prepare_table(dataset)
        if not columns:
            raise ValueError(f"stored result '{name}' has no columns to compute over")
        params.append(json.dumps(
            [{c: _json_cell(v) for c, v in zip(columns, rec)} for rec in records],
            ensure_ascii=False, default=str,
        ))
        typed = ", ".join(f"{_quote(c)} {t}" for c, t in zip(columns, types))
        ctes.append(
            f"{_quote(name)} AS (SELECT * FROM jsonb_to_recordset(${len(params)}::jsonb) AS t({typed}))"
        )
    prefix = f"WITH {', '.join(ctes)} " if ctes else ""
    return prefix + wrap_for_fetch(sql, max_rows=max_rows), params


# ── Engine ────────────────────────────────────────────────────────────────────


class SnapshotSqlEngine:
    """Run validated SELECTs over stored prior results in the metadata Postgres.

    ``pool`` is the asyncpg pool of the metadata database (the one that holds
    ``insights_turn_artifacts``). With ``pool=None`` every run reports an error,
    which the memory nodes turn into a fall-through to a live query.
    """

    def __init__(self, pool: Any, *, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        self._pool = pool
        self._timeout_ms = max(100, int(timeout_ms))

    async def run(
        self,
        tables: Dict[str, Dict[str, Any]],
        sql: str,
        *,
        max_rows: int = 2000,
    ) -> Dict[str, Any]:
        """Validate and execute *sql* over the snapshot *tables*.

        Returns ``{columns, rows, row_count, truncated}`` or ``{error}``. Rows
        are dicts (column → value) like every other ``query_result`` in the graph.
        """
        error = validate_snapshot_sql(sql, tables.keys())
        if error:
            return {"error": error}
        if self._pool is None:
            return {"error": "No metadata database connection is available for memory computations."}

        import asyncpg  # noqa: PLC0415 — keep module import light for tests

        try:
            statement_sql, params = compose_statement(tables, sql, max_rows=max_rows)
        except ValueError as err:
            return {"error": f"Memory computation failed: {err}"}
        try:
            async with self._pool.acquire() as conn:
                tr = conn.transaction(readonly=True)
                await tr.start()
                try:
                    await conn.execute(f"SET LOCAL statement_timeout = {self._timeout_ms}")
                    statement = await conn.prepare(statement_sql)
                    out_columns = [a.name for a in statement.get_attributes()]
                    fetched = await statement.fetch(*params)
                finally:
                    # Nothing was written; the rollback only ends the READ ONLY
                    # transaction and releases the SET LOCAL.
                    await tr.rollback()
        except asyncpg.exceptions.QueryCanceledError:
            return {"error": f"The computation exceeded {self._timeout_ms} ms and was stopped."}
        except asyncpg.exceptions.PostgresError as err:
            return {"error": f"SQL error: {err}"}
        except Exception as err:  # noqa: BLE001 — a memory computation must never raise into the graph
            logger.warning("snapshot_sql: computation failed", exc_info=True)
            return {"error": f"Memory computation failed: {err}"}

        truncated = len(fetched) > max_rows
        rows = [dict(zip(out_columns, tuple(r))) for r in fetched[:max_rows]]
        return {"columns": out_columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


__all__ = [
    "INSIGHTS_TABLE_PREFIX",
    "MEMORY_SQL_MARKER",
    "MEMORY_TABLE_PREFIX",
    "SnapshotSqlEngine",
    "compose_statement",
    "is_memory_sql",
    "prepare_table",
    "schema_and_sample",
    "snapshot_table_name",
    "validate_snapshot_sql",
    "wrap_for_fetch",
]
