"""Real-PostgreSQL proof that the sqlglot series builder emits SQL the engine
actually runs, that the runner's row cap / read-only gate accept it, and that
the rows feed ``prepare_series`` into a complete weekly calendar.

Emission tests prove parseability; only a live engine proves semantics
(DATE_TRUNC week boundaries, money casts, typed literals). Run against an
ephemeral Postgres::

    docker run -d --name jeen-e2e-pg -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e \
        -e POSTGRES_DB=e2e -p 55440:5432 postgres:16-alpine
    JEEN_E2E_DB=1 METADATA_DB_HOST=localhost METADATA_DB_PORT=55440 METADATA_DB_NAME=e2e \
      METADATA_DB_USER=e2e METADATA_DB_PASSWORD=e2e METADATA_DB_SSL=false \
      python3 -m pytest tests/integration/test_analysis_sql_postgres.py -q

SKIPS unless ``JEEN_E2E_DB`` is truthy.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

_ENABLED = (os.getenv("JEEN_E2E_DB") or "").strip().lower() in ("1", "true", "yes", "on")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ENABLED, reason="Set JEEN_E2E_DB=1 (+ test METADATA_DB_*) to run"),
]

os.environ.setdefault("AZURE_OPENAI_API_KEY", "test")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.invalid")


def _dsn() -> dict:
    return dict(
        host=os.environ["METADATA_DB_HOST"],
        port=int(os.environ.get("METADATA_DB_PORT", "5432")),
        database=os.environ["METADATA_DB_NAME"],
        username=os.environ["METADATA_DB_USER"],
        password=os.environ["METADATA_DB_PASSWORD"],
        enable_ssl=(os.environ.get("METADATA_DB_SSL", "false").lower() in ("1", "true")),
    )


async def _seed(conn) -> None:
    await conn.execute('DROP TABLE IF EXISTS "AnalysisFact"')
    await conn.execute(
        'CREATE TABLE "AnalysisFact" ("OrderDate" TIMESTAMP NOT NULL, "SalesAmount" MONEY NOT NULL, '
        '"Territory" VARCHAR(32) NOT NULL)'
    )
    start = date(2026, 1, 5)  # a Monday
    rows = []
    for d in range(0, 7 * 20):  # 20 weeks, skip week 7 entirely to create a gap
        day = start + timedelta(days=d)
        if 49 <= d < 56:
            continue
        rows.append((day, f"{100 + d:.2f}", "Northwest" if d % 3 else "Southwest"))
    await conn.executemany(
        'INSERT INTO "AnalysisFact" ("OrderDate", "SalesAmount", "Territory") VALUES ($1, $2::money, $3)', rows
    )


def test_series_sql_runs_on_postgres_and_prepares_a_complete_calendar():
    import asyncpg

    from src.analysis.contracts import FilterSpec, SeriesRequest
    from src.analysis.series import prepare_series
    from src.analysis.sql_builder import build_series_sql, build_span_probe_sql
    from src.connectors.postgres import PostgresSqlRunner

    async def _run():
        cfg = _dsn()
        conn = await asyncpg.connect(
            host=cfg["host"], port=cfg["port"], database=cfg["database"],
            user=cfg["username"], password=cfg["password"],
        )
        try:
            await _seed(conn)
        finally:
            await conn.close()

        runner = PostgresSqlRunner(source_key="e2e", schema="public", **cfg)
        await runner.initialize()
        try:
            req = SeriesRequest(
                table="AnalysisFact", date_column="OrderDate", measure_column="SalesAmount", grain="week",
                start=date(2026, 1, 5), end=date(2026, 5, 25),
                filters=[FilterSpec(table="AnalysisFact", column="Territory", op="equals", value="Northwest")],
            )
            types = {("analysisfact", "salesamount"): "money", ("analysisfact", "territory"): "character varying"}
            probe_sql = build_span_probe_sql(req, "postgres", connection_schema="public", column_types=types)
            probe = await runner.run_sql(probe_sql, limit=1, max_rows=1)
            assert "error" not in probe, probe
            assert probe["rows"][0]["n"] > 0

            sql = build_series_sql(req, "postgres", connection_schema="public", column_types=types)
            result = await runner.run_sql(sql, limit=1000, max_rows=1500)
            assert "error" not in result, result
            assert result["columns"] == ["ts", "value"]
            # 20 weeks requested, one week absent from the source.
            assert len(result["rows"]) == 19
            sf = prepare_series(result["rows"], req, columns=result["columns"])
            assert sf.n == 20 and sf.periods_filled == 1
            gap = sf.frame[~sf.frame["observed"]]
            assert len(gap) == 1 and gap["y"].iloc[0] == 0.0
            # Every calendar point is a Monday (DATE_TRUNC('WEEK') is ISO on Postgres).
            assert all(ts.weekday() == 0 for ts in sf.frame.index)
        finally:
            await runner.close()

    asyncio.run(_run())
