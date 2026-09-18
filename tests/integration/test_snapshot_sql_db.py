"""Real-PostgreSQL tests for the memory computation engine
(``src/agent/snapshot_sql.SnapshotSqlEngine``).

The unit tests prove the statement the engine composes; only a real server can
prove what matters here:

* the statement is legal inside a ``READ ONLY`` transaction (the temp-table
  predecessor was not — Postgres rejects all DDL there — and the fake pool never
  noticed);
* ``jsonb_to_recordset`` coerces the JSON payload to the inferred column types
  (NUMERIC from strings, BIGINT, BOOLEAN, NULLs) so ``ORDER BY price`` sorts
  numbers, arithmetic and ``ROUND`` work, and ``::date`` casts succeed;
* joins across two stored turns, window functions, share-of-total and string
  functions all run over the CTEs;
* the outer row cap truncates and reports it; a statement timeout is reported
  as such; and nothing is left behind afterwards.

Run against an ephemeral Postgres (no migrations are needed — the engine
creates nothing):

    docker run -d --name jeen-e2e-pg -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e \
        -e POSTGRES_DB=e2e -p 55440:5432 postgres:16-alpine
    JEEN_E2E_DB=1 METADATA_DB_HOST=localhost METADATA_DB_PORT=55440 METADATA_DB_NAME=e2e \
      METADATA_DB_USER=e2e METADATA_DB_PASSWORD=e2e METADATA_DB_SSL=false \
      python3 -m pytest tests/integration/test_snapshot_sql_db.py -q

The module SKIPS unless ``JEEN_E2E_DB`` is truthy so the DB-less unit run is
unaffected.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal, localcontext

import pytest

from src.agent.snapshot_sql import SnapshotSqlEngine, snapshot_table_name

_ENABLED = (os.getenv("JEEN_E2E_DB") or "").strip().lower() in ("1", "true", "yes", "on")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ENABLED, reason="Set JEEN_E2E_DB=1 (+ test METADATA_DB_*) to run"),
]

T3 = snapshot_table_name("T3")
T2 = snapshot_table_name("T2")

# A "sales" turn with the shapes the memory prompt promises the model: numeric
# strings (as JSON snapshots store Decimals), ints, booleans, text dates, NULLs.
SALES = {
    "columns": ["product", "region", "price", "qty", "sold_on", "in_stock"],
    "rows": [
        {"product": "Bike", "region": "N", "price": "1200.50", "qty": 3, "sold_on": "2026-01-05", "in_stock": True},
        {"product": "Helmet", "region": "S", "price": 80, "qty": 10, "sold_on": "2026-01-06", "in_stock": False},
        {"product": "Lock", "region": "S", "price": None, "qty": 0, "sold_on": "2026-01-07", "in_stock": None},
        {"product": "Light", "region": "N", "price": 25.25, "qty": 7, "sold_on": None, "in_stock": True},
        {"product": "Pump", "region": "E", "price": "40", "qty": 2, "sold_on": "2025-12-30", "in_stock": True},
    ],
}
COSTS = {"columns": ["product", "cost"], "rows": [["Bike", 900], ["Helmet", 55], ["Pump", 41]]}


@pytest.fixture(scope="module")
def pool():
    from src.metadata import close_metadata_pool, get_metadata_pool

    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_metadata_pool())
    yield p, loop
    loop.run_until_complete(close_metadata_pool())
    loop.close()


def _compute(pool_loop, sql, tables=None, **kw):
    p, loop = pool_loop
    engine = SnapshotSqlEngine(p, timeout_ms=kw.pop("timeout_ms", 5000))
    return loop.run_until_complete(engine.run(tables if tables is not None else {T3: SALES}, sql, **kw))


def _ok(out):
    assert "error" not in out, out.get("error")
    return out


def test_arithmetic_order_and_limit_over_numeric_with_nulls(pool):
    out = _ok(_compute(pool, f'SELECT "product", ROUND("price" * 1.1, 2) AS plus_10pct FROM {T3} '
                             f'ORDER BY plus_10pct DESC NULLS LAST LIMIT 2'))
    assert out["columns"] == ["product", "plus_10pct"]
    assert [r["product"] for r in out["rows"]] == ["Bike", "Helmet"]        # numeric sort, not text sort
    assert out["rows"][0]["plus_10pct"] == Decimal("1320.55")
    assert out["truncated"] is False


def test_group_by_round_and_share_of_total(pool):
    out = _ok(_compute(pool, f'SELECT "region", ROUND(SUM("price"), 2) AS total, COUNT(*) AS n, '
                             f'ROUND(100.0 * SUM("price") / (SELECT SUM("price") FROM {T3}), 1) AS share '
                             f'FROM {T3} GROUP BY 1 ORDER BY total DESC NULLS LAST'))
    by_region = {r["region"]: r for r in out["rows"]}
    assert by_region["N"]["total"] == Decimal("1225.75") and by_region["N"]["n"] == 2
    assert by_region["S"]["total"] == Decimal("80.00")   # NULL price ignored by SUM, counted by COUNT
    assert by_region["S"]["n"] == 2
    assert sum(r["share"] for r in out["rows"]) == Decimal("100.0")


def test_join_across_two_stored_turns(pool):
    out = _ok(_compute(
        pool,
        f'SELECT a."product", a."price" - b."cost" AS margin FROM {T3} a JOIN {T2} b USING ("product") '
        f'ORDER BY margin DESC LIMIT 3',
        tables={T3: SALES, T2: COSTS},
    ))
    assert [(r["product"], r["margin"]) for r in out["rows"]] == [
        ("Bike", Decimal("300.50")), ("Helmet", Decimal("25")), ("Pump", Decimal("-1")),
    ]


def test_window_case_date_cast_and_string_functions(pool):
    out = _ok(_compute(pool, f'SELECT UPPER("product") AS p, LENGTH("product") AS l, '
                             f'RANK() OVER (ORDER BY "qty" DESC) AS rnk, '
                             f'CASE WHEN "in_stock" THEN \'yes\' WHEN "in_stock" IS NULL THEN \'unknown\' ELSE \'no\' END AS stock '
                             f'FROM {T3} WHERE "sold_on"::date >= DATE \'2026-01-01\' ORDER BY rnk'))
    assert [r["p"] for r in out["rows"]] == ["HELMET", "BIKE", "LOCK"]      # Light (NULL date) and Pump (2025) filtered
    assert out["rows"][0] == {"p": "HELMET", "l": 6, "rnk": 1, "stock": "no"}
    assert out["rows"][2]["stock"] == "unknown"


def test_model_ctes_and_the_outer_row_cap(pool):
    out = _ok(_compute(pool, f'WITH big AS (SELECT * FROM {T3} WHERE "qty" > 0) SELECT "product" FROM big ORDER BY 1',
                       max_rows=3))
    assert out["row_count"] == 3 and out["truncated"] is True
    assert [r["product"] for r in out["rows"]] == ["Bike", "Helmet", "Light"]


def test_two_thousand_row_payload_is_fine(pool):
    values = [Decimal(i) / 7 for i in range(2000)]
    rows = [{"k": f"K{i}", "v": str(v)} for i, v in enumerate(values)]
    out = _ok(_compute(pool, f'SELECT COUNT(*) AS n, SUM("v") AS total, MAX("v") AS mx FROM {T3}',
                       tables={T3: {"columns": ["k", "v"], "rows": rows}}))
    assert out["rows"][0]["n"] == 2000
    # NUMERIC keeps every digit the snapshot carried: exact equality, no float drift.
    # (Postgres sums exactly; Python's default 28-digit context would round, so
    # widen it for the reference value.)
    assert out["rows"][0]["mx"] == max(values)
    with localcontext() as ctx:
        ctx.prec = 60
        assert out["rows"][0]["total"] == sum(values)


def test_statement_timeout_is_reported_not_raised(pool):
    # pg_sleep is denied by the validator, so burn time with a cross join instead.
    rows = [{"i": i} for i in range(3000)]
    out = _compute(pool, f'SELECT COUNT(*) AS n FROM {T3} a, {T3} b, {T3} c',
                   tables={T3: {"columns": ["i"], "rows": rows}}, timeout_ms=200)
    assert "exceeded 200 ms" in out["error"]


def test_engine_runs_inside_a_read_only_transaction_and_leaves_nothing_behind(pool):
    p, loop = pool

    async def scenario():
        # The engine's own connection state is not observable from outside, so
        # prove the property on the statement it composes: it must run under
        # READ ONLY and must not create anything.
        from src.agent.snapshot_sql import compose_statement

        statement, params = compose_statement({T3: SALES}, f'SELECT COUNT(*) AS n FROM {T3}', max_rows=10)
        async with p.acquire() as conn:
            tr = conn.transaction(readonly=True)
            await tr.start()
            try:
                assert (await conn.fetchval("SHOW transaction_read_only")) == "on"
                n = await conn.fetchval(statement, *params)
            finally:
                await tr.rollback()
            leftovers = await conn.fetchval(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace "
                "WHERE c.relname LIKE 'insights_mem_%'"
            )
        return n, leftovers

    n, leftovers = loop.run_until_complete(scenario())
    assert n == 5 and leftovers == 0
