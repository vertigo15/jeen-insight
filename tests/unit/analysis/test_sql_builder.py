"""The series/probe SQL must be read-only, single-statement, catalog-valid and
identical in meaning across every registered dialect."""

from __future__ import annotations

import pytest
import sqlglot

from src.analysis.contracts import FilterSpec, SeriesRequest
from src.analysis.sql_builder import (
    UnsupportedFilter,
    build_series_expression,
    build_series_sql,
    build_span_probe_sql,
    describe_filters,
    filters_from_grounder,
)
from src.agent.langgraph_agent.nodes.validation import make_sqlglot_validate
from src.connectors.base import is_read_only_sql
from src.connectors.factory import CONNECTOR_REGISTRY

REGISTERED = sorted({d.canonical_type for d in CONNECTOR_REGISTRY.values()})

_TYPES = {
    ("factinternetsales", "orderdate"): "timestamp",
    ("factinternetsales", "salesamount"): "money",
    ("factinternetsales", "salesterritorykey"): "integer",
    ("factinternetsales", "productkey"): "integer",
    ("factinternetsales", "salesreason"): "varchar",
}


def _req(**over) -> SeriesRequest:
    base = dict(
        table="FactInternetSales", date_column="OrderDate", measure_column="SalesAmount",
        grain="week", start="2026-03-01", end="2026-09-01",
        filters=[
            FilterSpec(table="FactInternetSales", column="SalesTerritoryKey", op="in", value=["1", "5"]),
            FilterSpec(table="FactInternetSales", column="SalesReason", op="equals", value="Promotion"),
        ],
    )
    base.update(over)
    return SeriesRequest(**base)


def _validate(sql: str, database_type: str):
    node = make_sqlglot_validate(True, require_catalog=True, enforce_schema_qualifier=True)
    state = {
        "generated_sql": sql,
        "database_type": database_type,
        "known_tables": ["factinternetsales", "dimproduct"],
        "table_columns": {"factinternetsales": ["orderdate", "salesamount", "salesterritorykey", "productkey", "salesreason"]},
        "connection_schema": "dbo",
        "connection_catalog": None,
        "filter_plan": None,
    }
    return node(state)["sqlglot_error"]


@pytest.mark.parametrize("database_type", REGISTERED)
def test_series_sql_passes_read_only_and_validator_on_every_registered_dialect(database_type):
    sql = build_series_sql(_req(), database_type, connection_schema="dbo", column_types=_TYPES)
    assert is_read_only_sql(sql)
    assert _validate(sql, database_type) is None, sql
    # Exactly one statement, one table, the truncation repeated in GROUP BY (no positional).
    stmts = sqlglot.parse(sql, dialect=sqlglot.Dialect.get_or_raise(database_type if database_type != "databricks" else "databricks"))
    assert len(stmts) == 1
    assert "GROUP BY 1" not in sql.upper()
    assert sql.count("OrderDate") >= 4  # select, where×2, group by


@pytest.mark.parametrize("database_type", REGISTERED)
def test_span_probe_passes_validator(database_type):
    sql = build_span_probe_sql(_req(), database_type, connection_schema="dbo", column_types=_TYPES)
    assert is_read_only_sql(sql)
    assert _validate(sql, database_type) is None, sql
    assert "min_ts" in sql and "max_ts" in sql and "COUNT(*)" in sql
    # The probe ignores the time range but keeps the grounded filters.
    assert "2026-03-01" not in sql and "Promotion" in sql


def test_postgres_money_is_cast_and_identifiers_are_quoted():
    sql = build_series_sql(_req(), "postgres", connection_schema="dbo", column_types=_TYPES)
    assert 'SUM(CAST("SalesAmount" AS DECIMAL))' in sql
    assert '"dbo"."FactInternetSales"' in sql
    assert 'DATE_TRUNC(\'WEEK\', "OrderDate")' in sql
    # Half-open range with typed literals.
    assert '"OrderDate" >= CAST(\'2026-03-01\' AS DATE)' in sql
    assert '"OrderDate" < CAST(\'2026-09-01\' AS DATE)' in sql
    # Typed literals from the catalog: integers unquoted, text quoted.
    assert '"SalesTerritoryKey" IN (1, 5)' in sql and "\"SalesReason\" = 'Promotion'" in sql


def test_databricks_uses_backticks_and_trino_keeps_money_uncast():
    trino = build_series_sql(_req(), "trino", connection_schema="dbo", column_types=_TYPES)
    assert 'SUM("SalesAmount")' in trino
    bricks = build_series_sql(_req(), "databricks", connection_schema="dbo", connection_catalog="main", column_types=_TYPES)
    assert "`main`.`dbo`.`FactInternetSales`" in bricks and "SUM(`SalesAmount`)" in bricks


def test_postgres_never_qualifies_a_table_with_the_catalog():
    # Regression: on Postgres the connection's "catalog" is just the database
    # name (see factory: catalog := database), which cannot qualify a table.
    # Emitting it made Postgres read the catalog as a (missing) schema —
    # "AdventureWorksDW"."factinternetsales" → relation does not exist — which
    # blocked every analysis (the span probe failed) on Postgres connections.
    bare = build_span_probe_sql(_req(), "postgres", connection_schema=None,
                                connection_catalog="AdventureWorksDW", column_types=_TYPES)
    assert '"AdventureWorksDW"' not in bare and 'FROM "FactInternetSales"' in bare
    schema_only = build_series_sql(_req(), "postgres", connection_schema="public",
                                   connection_catalog="AdventureWorksDW", column_types=_TYPES)
    assert '"public"."FactInternetSales"' in schema_only and '"AdventureWorksDW"' not in schema_only
    # Catalog-addressed engines still carry the catalog in the table reference.
    for db in ("databricks", "trino"):
        sql = build_series_sql(_req(), db, connection_schema="dbo",
                               connection_catalog="main", column_types=_TYPES)
        assert "main" in sql, (db, sql)


@pytest.mark.parametrize("dialect", ["tsql", "snowflake", "mysql", "bigquery", "duckdb"])
def test_portability_evidence_for_unregistered_dialects(dialect):
    """Not a support claim: shows the same AST emits parseable SQL elsewhere."""
    sql = build_series_sql(_req(), None, connection_schema="dbo", column_types=_TYPES, dialect=dialect)
    parsed = sqlglot.parse_one(sql, read=dialect)
    assert parsed is not None and "GROUP BY 1" not in sql.upper()
    if dialect == "tsql":
        assert "DATETRUNC(WEEK, [OrderDate])" in sql and "[dbo].[FactInternetSales]" in sql


def test_count_star_and_no_filters():
    req = SeriesRequest(table="FactInternetSales", date_column="OrderDate", measure_column="*", agg="count", grain="month")
    sql = build_series_sql(req, "postgres", connection_schema="dbo")
    assert 'COUNT(*) AS "value"' in sql and "WHERE" not in sql
    with pytest.raises(ValueError):
        build_series_sql(SeriesRequest(table="t", date_column="d", measure_column="*", agg="sum"), "postgres")


def test_filter_operators_and_unsupported_filters():
    req = _req(filters=[
        FilterSpec(table="FactInternetSales", column="ProductKey", op="between", value=["300", "400"]),
        FilterSpec(table="FactInternetSales", column="SalesReason", op="contains", value="promo"),
        FilterSpec(table="FactInternetSales", column="OrderDate", op="gte", value="2026-01-01"),
    ])
    sql = build_series_sql(req, "postgres", connection_schema="dbo", column_types=_TYPES)
    assert '"ProductKey" BETWEEN 300 AND 400' in sql
    assert "ILIKE '%promo%'" in sql
    assert '"OrderDate" >= CAST(\'2026-01-01\' AS DATE)' in sql
    other_table = _req(filters=[FilterSpec(table="DimProduct", column="Color", op="equals", value="Red")])
    with pytest.raises(UnsupportedFilter):
        build_series_sql(other_table, "postgres")


def test_filters_from_grounder_keeps_same_table_and_reports_dropped():
    items = [
        {"table": "FactInternetSales", "column": "SalesReason", "op": "equals", "value": "Promotion", "resolved": True},
        {"table": "DimProduct", "column": "Color", "op": "in", "value": ["Red", "Black"]},
        {"table": "FactInternetSales", "column": "ProductKey", "op": "regex", "value": "x"},
        {"column": "orphan", "op": "equals", "value": 1},
    ]
    kept, dropped = filters_from_grounder(items, "factinternetsales")
    assert [f.column for f in kept] == ["SalesReason"]
    assert dropped == ["DimProduct.Color", "ProductKey (regex)"]
    assert describe_filters(kept) == "SalesReason = Promotion"
    assert describe_filters([FilterSpec(table="t", column="c", op="in", value=[1, 2])]) == "c in (1, 2)"


def test_expression_is_inspectable():
    tree = build_series_expression(_req(), "postgres", connection_schema="dbo")
    assert isinstance(tree, sqlglot.exp.Select)
    assert [t.name for t in tree.find_all(sqlglot.exp.Table)] == ["FactInternetSales"]


# ── P6: multi-series, second measure, contribution, entity ────────────────────

from datetime import date  # noqa: E402

from src.analysis.contracts import EntityRequest  # noqa: E402
from src.analysis.sql_builder import build_contribution_sql, build_entity_sql  # noqa: E402

_TYPES_P6 = {**_TYPES, ("factinternetsales", "productkey"): "integer", ("dimcustomer", "customerkey"): "integer",
             ("dimcustomer", "yearlyincome"): "money", ("dimcustomer", "totalchildren"): "integer", ("dimcustomer", "gender"): "char"}


def _validate_p6(sql: str, database_type: str):
    node = make_sqlglot_validate(True, require_catalog=True, enforce_schema_qualifier=True)
    return node({
        "generated_sql": sql, "database_type": database_type,
        "known_tables": ["factinternetsales", "dimcustomer"],
        "table_columns": {"factinternetsales": ["orderdate", "salesamount", "salesterritorykey", "productkey", "salesreason"],
                          "dimcustomer": ["customerkey", "yearlyincome", "totalchildren", "gender"]},
        "connection_schema": "dbo", "connection_catalog": None, "filter_plan": None,
    })["sqlglot_error"]


@pytest.mark.parametrize("database_type", REGISTERED)
def test_group_by_and_extra_measure_pass_the_validator(database_type):
    req = _req(group_by="SalesTerritoryKey", filters=[])
    sql = build_series_sql(req, database_type, connection_schema="dbo", column_types=_TYPES_P6,
                           extra_measures=[("sum", "SalesTerritoryKey", "value2")])
    assert is_read_only_sql(sql) and _validate_p6(sql, database_type) is None, sql
    assert "series_id" in sql and "value2" in sql
    # The split column appears in both SELECT and GROUP BY (no positional GROUP BY).
    assert sql.count("SalesTerritoryKey") >= 3 and "GROUP BY 1" not in sql.upper()


@pytest.mark.parametrize("database_type", REGISTERED)
def test_contribution_sql_is_one_union_per_dimension(database_type):
    req = _req(filters=[FilterSpec(table="FactInternetSales", column="SalesReason", op="equals", value="Promotion")])
    sql = build_contribution_sql(
        req, ["SalesTerritoryKey", "ProductKey"],
        before=(date(2026, 1, 1), date(2026, 4, 1)), after=(date(2026, 4, 1), date(2026, 7, 1)),
        database_type=database_type, connection_schema="dbo", column_types=_TYPES_P6,
    )
    assert is_read_only_sql(sql) and _validate_p6(sql, database_type) is None, sql
    assert sql.upper().count("UNION ALL") == 1
    assert "'before'" in sql and "'after'" in sql and "Promotion" in sql
    assert "2026-01-01" in sql and "2026-07-01" in sql
    # Half-open window on both periods, filters kept.
    assert sql.count("2026-04-01") >= 4


@pytest.mark.parametrize("database_type", REGISTERED)
def test_entity_sql_reads_key_features_target_under_filters(database_type):
    req = EntityRequest(table="DimCustomer", entity_key="CustomerKey", features=["YearlyIncome", "TotalChildren"],
                        target="TotalChildren", filters=[FilterSpec(table="DimCustomer", column="Gender", op="equals", value="M")])
    sql = build_entity_sql(req, database_type=database_type, connection_schema="dbo", column_types=_TYPES_P6)
    assert is_read_only_sql(sql) and _validate_p6(sql, database_type) is None, sql
    assert "entity_key" in sql and "target" in sql and "'M'" in sql
    assert "ORDER BY" in sql.upper()  # deterministic row cap


from src.analysis.contracts import CohortRequest, ExperimentRequest  # noqa: E402
from src.analysis.sql_builder import build_cohort_sql, build_experiment_sql  # noqa: E402


@pytest.mark.parametrize("database_type", REGISTERED)
def test_cohort_sql_aggregates_distinct_entities_per_period(database_type):
    req = CohortRequest(table="FactInternetSales", entity_key="ProductKey",
                        cohort_date="OrderDate", activity_date="OrderDate", grain="month")
    sql = build_cohort_sql(req, database_type=database_type, connection_schema="dbo", column_types=_TYPES_P6)
    assert is_read_only_sql(sql), sql
    up = sql.upper()
    assert up.count("UNION ALL") == 1  # activity branch + NULL-period size branch
    assert "COUNT(DISTINCT" in up
    # Aliases are quoted with the dialect's own quote char (" or `).
    assert all(f'{q}cohort{q}' in sql and f'{q}period{q}' in sql and f'{q}active{q}' in sql
               for q in ['"', '`'] if f'{q}cohort{q}' in sql)
    assert "cohort" in sql and "period" in sql and "active" in sql


@pytest.mark.parametrize("database_type", REGISTERED)
def test_experiment_sql_summarises_moments_per_arm(database_type):
    req = ExperimentRequest(table="FactInternetSales", group_column="SalesReason",
                            outcome_column="SalesAmount", outcome_type="continuous",
                            filters=[FilterSpec(table="FactInternetSales", column="SalesTerritoryKey", op="equals", value="5")])
    sql = build_experiment_sql(req, database_type=database_type, connection_schema="dbo", column_types=_TYPES_P6)
    assert is_read_only_sql(sql) and _validate_p6(sql, database_type) is None, sql
    up = sql.upper()
    assert up.count("SUM(") == 2  # sum_x and sum_x2
    assert "COUNT(*)" in up and "GROUP BY" in up and "GROUP BY 1" not in up
    assert "arm" in sql and "sum_x" in sql and "sum_x2" in sql and "SalesTerritoryKey" in sql
