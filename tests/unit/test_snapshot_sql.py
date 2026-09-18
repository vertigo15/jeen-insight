"""Memory computations over stored prior results in the metadata PostgreSQL
(src/agent/snapshot_sql.py): validation, table preparation, statement
composition and the engine's statement sequence against a fake asyncpg pool.

The real-database behaviour (READ ONLY legality, type coercion by
``jsonb_to_recordset``) is covered by tests/integration/test_snapshot_sql_db.py."""

from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pytest

from src.agent.snapshot_sql import (
    INSIGHTS_TABLE_PREFIX,
    MEMORY_SQL_MARKER,
    MEMORY_TABLE_PREFIX,
    SnapshotSqlEngine,
    compose_statement,
    is_memory_sql,
    prepare_table,
    schema_and_sample,
    snapshot_table_name,
    validate_snapshot_sql,
    wrap_for_fetch,
)

T = snapshot_table_name("T3")   # insights_mem_t3

T3 = {
    "columns": ["product", "price", "sold_on", "in_stock"],
    "rows": [
        {"product": "Bike", "price": "1200.50", "sold_on": "2026-01-05", "in_stock": True},
        {"product": "Helmet", "price": 80, "sold_on": "2026-01-06", "in_stock": False},
        {"product": "Lock", "price": None, "sold_on": "2026-01-07", "in_stock": None},
        {"product": "Light", "price": 25.25, "sold_on": None, "in_stock": True},
    ],
}


class TestNaming:
    def test_memory_relations_carry_the_insights_prefix(self):
        """Everything Insights names in the shared metadata DB is ``insights_*``;
        the memory CTEs must not be the exception."""
        assert MEMORY_TABLE_PREFIX.startswith(INSIGHTS_TABLE_PREFIX)
        assert snapshot_table_name("T3") == "insights_mem_t3"
        assert snapshot_table_name("t12") == "insights_mem_t12"


class TestValidation:
    def test_accepts_single_select_over_snapshot_tables_incl_cte_and_casts(self):
        t4 = snapshot_table_name("T4")
        assert validate_snapshot_sql(f'SELECT "product" FROM {T} ORDER BY "price" DESC LIMIT 2', [T]) is None
        assert validate_snapshot_sql(f"WITH x AS (SELECT * FROM {T}) SELECT COUNT(*) FROM x", [T]) is None
        assert validate_snapshot_sql(f"SELECT a.product FROM {T} a JOIN {t4} b ON a.product = b.product", [T, t4]) is None
        assert validate_snapshot_sql(f'SELECT "sold_on"::date, ROUND("price" * 1.1, 2) FROM {T}', [T]) is None

    def test_refuses_other_tables_schemas_statements_and_writes(self):
        assert "Unknown table" in validate_snapshot_sql("SELECT * FROM insights_conversations", [T])
        assert "Unknown table" in validate_snapshot_sql("SELECT * FROM metadata_tables", [T])
        assert "Unknown table" in validate_snapshot_sql("SELECT * FROM pg_catalog.pg_tables", [T])
        assert "Unknown table" in validate_snapshot_sql(f"SELECT * FROM public.{T}", [T])
        assert "Only SELECT" in validate_snapshot_sql(f"DELETE FROM {T}", [T])
        assert "Exactly one" in validate_snapshot_sql("SELECT 1; SELECT 2", [T])
        assert validate_snapshot_sql("", [T])
        assert "syntax" in validate_snapshot_sql(f"SELEC product FROM {T}", [T]).lower()

    @pytest.mark.parametrize("sql", [
        f"SELECT pg_sleep(10) FROM {T}",
        f"SELECT pg_read_file('/etc/passwd') FROM {T}",
        f"SELECT current_setting('data_directory') FROM {T}",
        f"SELECT * FROM {T} WHERE product = pg_ls_dir('.')",
        # catalog / session probes
        f"SELECT has_table_privilege('insights_turn_artifacts', 'SELECT') FROM {T}",
        f"SELECT to_regclass('metadata_tables') FROM {T}",
        f"SELECT pg_backend_pid(), inet_server_addr() FROM {T}",
    ])
    def test_refuses_denied_server_functions(self, sql):
        assert "not allowed" in validate_snapshot_sql(sql, [T])

    def test_refuses_model_ctes_named_like_the_stored_results(self):
        # Our relations are CTEs around the model's query; a same-named model CTE
        # would shadow them inside the subquery and silently change the data.
        err = validate_snapshot_sql(f"WITH {T} AS (SELECT 1 AS price) SELECT * FROM {T}", [T])
        assert err and "reserved" in err
        err = validate_snapshot_sql(f"WITH insights_mem_x AS (SELECT * FROM {T}) SELECT * FROM insights_mem_x", [T])
        assert err and "reserved" in err
        # Ordinary CTE names remain fine.
        assert validate_snapshot_sql(f"WITH top AS (SELECT * FROM {T}) SELECT * FROM top", [T]) is None

    def test_fails_closed_when_the_parser_is_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlglot", None)   # makes `import sqlglot` raise ImportError
        err = validate_snapshot_sql(f'SELECT "product" FROM {T}', [T])
        assert err and "refusing" in err


class TestPrepareTable:
    def test_infers_postgres_types_per_column(self):
        columns, types, records = prepare_table(T3)
        assert columns == ["product", "price", "sold_on", "in_stock"]
        assert types == ["TEXT", "NUMERIC", "TEXT", "BOOLEAN"]   # numeric strings → NUMERIC, dates stay TEXT
        assert records[0] == ("Bike", Decimal("1200.50"), "2026-01-05", True)
        assert records[2][1] is None and records[2][3] is None

    def test_integers_and_positional_rows(self):
        columns, types, records = prepare_table({"columns": ["Order Year", "Total"], "rows": [[2007, 10], [2008, 20]]})
        assert types == ["BIGINT", "BIGINT"] and records == [(2007, 10), (2008, 20)]

    def test_mixed_and_empty_columns_fall_back_to_text(self):
        _, types, records = prepare_table({"columns": ["a", "b"], "rows": [{"a": 1, "b": None}, {"a": "x", "b": None}]})
        assert types == ["TEXT", "TEXT"] and records[0] == ("1", None)

    def test_helpers(self):
        columns, sample = schema_and_sample(T3, sample_rows=2)
        assert columns == T3["columns"] and [r["product"] for r in sample] == ["Bike", "Helmet"]
        assert is_memory_sql(f"{MEMORY_SQL_MARKER} replay of T3\nSELECT 1") and not is_memory_sql("SELECT 1")
        assert wrap_for_fetch("SELECT 1 ;", max_rows=10) == "SELECT * FROM (SELECT 1) AS insights_mem_result LIMIT 11"


class TestComposeStatement:
    """The stored rows become typed CTEs over JSONB bind parameters; nothing is
    ever created in the database, so the statement is legal under READ ONLY."""

    def test_one_turn_becomes_a_typed_cte_and_one_json_parameter(self):
        stmt, params = compose_statement({T: T3}, f'SELECT "product", "price" FROM {T} ORDER BY "price" DESC LIMIT 1',
                                         max_rows=2000)
        assert stmt == (
            'WITH "insights_mem_t3" AS (SELECT * FROM jsonb_to_recordset($1::jsonb) '
            'AS t("product" TEXT, "price" NUMERIC, "sold_on" TEXT, "in_stock" BOOLEAN)) '
            f'SELECT * FROM (SELECT "product", "price" FROM {T} ORDER BY "price" DESC LIMIT 1) '
            'AS insights_mem_result LIMIT 2001'
        )
        assert "CREATE" not in stmt and "TEMP" not in stmt and "INSERT" not in stmt
        assert len(params) == 1
        payload = json.loads(params[0])
        # NUMERIC travels as text so Postgres parses it exactly; NULL and booleans stay JSON-native.
        assert payload[0] == {"product": "Bike", "price": "1200.50", "sold_on": "2026-01-05", "in_stock": True}
        assert payload[2] == {"product": "Lock", "price": None, "sold_on": "2026-01-07", "in_stock": None}
        assert payload[3]["price"] == "25.25"

    def test_two_turns_get_numbered_parameters_in_order(self):
        t4 = snapshot_table_name("T4")
        stmt, params = compose_statement(
            {T: T3, t4: {"columns": ["product", "cost"], "rows": [["Bike", 900]]}},
            f"SELECT a.product FROM {T} a JOIN {t4} b ON a.product = b.product", max_rows=5,
        )
        assert stmt.startswith('WITH "insights_mem_t3" AS (SELECT * FROM jsonb_to_recordset($1::jsonb)')
        assert '"insights_mem_t4" AS (SELECT * FROM jsonb_to_recordset($2::jsonb) AS t("product" TEXT, "cost" BIGINT))' in stmt
        assert stmt.endswith("AS insights_mem_result LIMIT 6")
        assert [json.loads(p) for p in params] == [
            [dict(zip(T3["columns"], (r["product"], None if r["price"] is None else str(Decimal(str(r["price"]))),
                                       r["sold_on"], r["in_stock"]))) for r in T3["rows"]],
            [{"product": "Bike", "cost": 900}],
        ]

    def test_quotes_identifiers_that_need_it(self):
        stmt, _ = compose_statement(
            {T: {"columns": ['Order "Year"', "Total"], "rows": [[2007, 10]]}}, f"SELECT * FROM {T}", max_rows=1,
        )
        assert 'AS t("Order ""Year""" BIGINT, "Total" BIGINT)' in stmt

    def test_refuses_unprefixed_or_column_less_results(self):
        with pytest.raises(ValueError, match="must carry the insights_mem_ prefix"):
            compose_statement({"t3": T3}, "SELECT * FROM t3", max_rows=1)
        # A turn without columns would produce no CTE, and the bare name could then
        # resolve to a real relation — refuse instead of silently skipping.
        with pytest.raises(ValueError, match="no columns"):
            compose_statement({T: {"columns": [], "rows": []}}, f"SELECT * FROM {T}", max_rows=1)


# ── Engine against a fake asyncpg pool ────────────────────────────────────────


class _FakeTx:
    def __init__(self, log):
        self.log = log

    async def start(self):
        self.log.append(("tx", "START READONLY"))

    async def rollback(self):
        self.log.append(("tx", "ROLLBACK"))


class _FakeStatement:
    def __init__(self, log, columns, rows, error=None):
        self.log, self._columns, self._rows, self._error = log, columns, rows, error

    def get_attributes(self):
        return [SimpleNamespace(name=c) for c in self._columns]

    async def fetch(self, *params):
        self.log.append(("fetch", list(params)))
        if self._error:
            raise self._error
        return self._rows


class _FakeConn:
    def __init__(self, log, *, result=(["product", "price"], [("Bike", Decimal("1200.50"))]), error=None):
        self.log, self._result, self._error = log, result, error

    def transaction(self, *, readonly=False):
        assert readonly is True, "memory computations must run READ ONLY"
        return _FakeTx(self.log)

    async def execute(self, sql, *args):
        # The only statement the engine may execute outright is the SET LOCAL:
        # any DDL here would be rejected by a READ ONLY transaction in production.
        assert sql.upper().startswith("SET LOCAL"), f"unexpected non-SET statement: {sql}"
        self.log.append(("execute", " ".join(sql.split())))

    async def executemany(self, sql, records):
        raise AssertionError(f"the engine must not write rows: {sql}")

    async def prepare(self, sql):
        self.log.append(("prepare", " ".join(sql.split())))
        return _FakeStatement(self.log, *self._result, error=self._error)


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _run(conn, sql=f'SELECT "product", "price" FROM {T} ORDER BY "price" DESC LIMIT 1', tables=None, **kw):
    engine = SnapshotSqlEngine(_FakePool(conn), timeout_ms=kw.pop("timeout_ms", 5000))
    return asyncio.run(engine.run(tables if tables is not None else {T: T3}, sql, **kw))


def test_engine_statement_sequence_and_result_shape():
    log = []
    out = _run(_FakeConn(log))
    kinds = [entry[0] for entry in log]
    # One READ ONLY transaction, the timeout, one prepared statement carrying the
    # rows as a JSONB parameter, one fetch, rollback. No DDL, no INSERT.
    assert kinds == ["tx", "execute", "prepare", "fetch", "tx"]
    assert log[0] == ("tx", "START READONLY") and log[-1] == ("tx", "ROLLBACK")
    assert log[1][1] == "SET LOCAL statement_timeout = 5000"
    assert log[2][1] == (
        'WITH "insights_mem_t3" AS (SELECT * FROM jsonb_to_recordset($1::jsonb) '
        'AS t("product" TEXT, "price" NUMERIC, "sold_on" TEXT, "in_stock" BOOLEAN)) '
        f'SELECT * FROM (SELECT "product", "price" FROM {T} ORDER BY "price" DESC LIMIT 1) '
        'AS insights_mem_result LIMIT 2001'
    )
    (params,) = [entry[1] for entry in log if entry[0] == "fetch"]
    assert len(params) == 1 and json.loads(params[0])[0]["price"] == "1200.50"
    assert out == {"columns": ["product", "price"], "rows": [{"product": "Bike", "price": Decimal("1200.50")}],
                   "row_count": 1, "truncated": False}


def test_engine_refuses_unprefixed_or_column_less_results_before_touching_the_database():
    log = []
    out = _run(_FakeConn(log), sql="SELECT * FROM t3", tables={"t3": T3})
    assert "must carry the insights_mem_ prefix" in out["error"] and log == []
    out = _run(_FakeConn(log), sql=f"SELECT * FROM {T}", tables={T: {"columns": [], "rows": []}})
    assert "no columns" in out["error"] and log == []


def test_engine_truncates_at_max_rows():
    rows = [(f"p{i}", Decimal(i)) for i in range(5)]
    out = _run(_FakeConn([], result=(["product", "price"], rows)), max_rows=3)
    assert out["row_count"] == 3 and out["truncated"] is True


def test_engine_rolls_back_and_reports_timeout_and_sql_errors():
    log = []
    out = _run(_FakeConn(log, error=asyncpg.exceptions.QueryCanceledError("canceling statement due to statement timeout")),
               timeout_ms=1500)
    assert "exceeded 1500 ms" in out["error"] and log[-1] == ("tx", "ROLLBACK")

    log = []
    out = _run(_FakeConn(log, error=asyncpg.exceptions.UndefinedColumnError('column "nope" does not exist')))
    assert out["error"].startswith("SQL error:") and log[-1] == ("tx", "ROLLBACK")

    log = []
    out = _run(_FakeConn(log, error=RuntimeError("connection reset")))
    assert out["error"].startswith("Memory computation failed") and log[-1] == ("tx", "ROLLBACK")


def test_engine_validates_before_touching_the_database_and_needs_a_pool():
    log = []
    out = _run(_FakeConn(log), sql="SELECT * FROM insights_conversations")
    assert "Unknown table" in out["error"] and log == []
    out = asyncio.run(SnapshotSqlEngine(None).run({T: T3}, f'SELECT "product" FROM {T}'))
    assert "No metadata database connection" in out["error"]
