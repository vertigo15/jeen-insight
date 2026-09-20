"""One aggregation query, every registered dialect.

The series query is built once as a sqlglot AST and emitted per dialect via
the same ``sqlglot_dialect_for`` mapping ``sqlglot_validate`` uses, so the
emitted text is exactly what the validator will parse back. Rules:

* only ``DATE_TRUNC + AGG + GROUP BY`` — no calendar generation (it does not
  transpile; gap-fill is pandas' job, see :mod:`src.analysis.series`);
* no positional ``GROUP BY 1`` (illegal on SQL Server) — the truncation
  expression is repeated;
* every identifier quoted (``identify=True``) so Postgres keeps the catalog's
  case and reserved words cannot bite;
* the table is qualified with the connection's schema/catalog so the
  schema-qualifier guard passes;
* half-open time range with typed date literals;
* grounded filters are carried verbatim from ``filter_grounder``.

Weeks start on Monday on every registered engine's ``DATE_TRUNC('WEEK')``;
``SeriesRequest.week_start='sunday'`` is not supported on the SQL path.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlglot import exp

from src.analysis.contracts import CohortRequest, EntityRequest, ExperimentRequest, FilterSpec, SeriesRequest
from src.connectors.dialects import sqlglot_dialect_for, supports_catalog_qualifier

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER = re.compile(r"^[-+]?\d+(\.\d+)?$")
_MONEY_TYPES = ("money",)
_NUMERIC_TYPES = ("int", "decimal", "numeric", "float", "double", "real", "money", "number", "bigint", "smallint")
_DATE_TYPES = ("date", "time", "timestamp", "datetime")

ColumnTypes = Mapping[Tuple[str, str], str]


class UnsupportedFilter(ValueError):
    """A grounded filter the v1 builder cannot express (e.g. on another table)."""


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _table(
    req: "SeriesRequest | EntityRequest | CohortRequest | ExperimentRequest",
    *,
    connection_schema: Optional[str],
    connection_catalog: Optional[str],
    database_type: Optional[str] = None,
) -> exp.Table:
    schema = req.schema_name or connection_schema or None
    catalog = req.catalog or connection_catalog or None
    # Only catalog.schema.table engines (Trino, Databricks) may qualify a table
    # with the catalog. On Postgres/MySQL the database is fixed by the connection
    # and the catalog would be read as a (missing) schema, so drop it there.
    if not supports_catalog_qualifier(database_type):
        catalog = None
    return exp.Table(
        this=_ident(req.table),
        db=_ident(schema) if schema else None,
        catalog=_ident(catalog) if catalog else None,
    )


def _column(name: str) -> exp.Column:
    return exp.Column(this=_ident(name))


def _date_trunc(req: SeriesRequest) -> exp.Expression:
    # TimestampTrunc is the node sqlglot itself builds when it parses
    # DATE_TRUNC('week', col); it transpiles cleanly (DATETRUNC on tsql,
    # DATE_TRUNC on Databricks) where the DateTrunc node does not.
    return exp.TimestampTrunc(unit=exp.Var(this=req.grain.upper()), this=_column(req.date_column))


def _type_of(column_types: Optional[ColumnTypes], table: str, column: str) -> str:
    if not column_types:
        return ""
    return str(column_types.get((table.lower(), column.lower())) or "").lower()


def _numeric_column(table: str, column: str, database_type: Optional[str], column_types: Optional[ColumnTypes]) -> exp.Expression:
    """The column as a number the driver returns numerically.

    Postgres ``money`` is the one numeric type that comes back as text
    (``$90,000.00``) and casts to ``numeric`` only explicitly, so a measure,
    feature or target of that type is cast here; everything else is untouched.
    """
    col: exp.Expression = _column(column)
    mtype = _type_of(column_types, table, column)
    if (database_type or "").lower() in ("postgres", "postgresql") and any(t in mtype for t in _MONEY_TYPES):
        col = exp.Cast(this=col, to=exp.DataType.build("DECIMAL"))
    return col


def _measure(req: SeriesRequest, database_type: Optional[str], column_types: Optional[ColumnTypes]) -> exp.Expression:
    if req.measure_column.strip() == "*":
        if req.agg != "count":
            raise ValueError("only COUNT may aggregate '*'")
        return exp.Count(this=exp.Star())
    col = _numeric_column(req.table, req.measure_column, database_type, column_types)
    agg = {
        "sum": exp.Sum, "count": exp.Count, "avg": exp.Avg, "min": exp.Min, "max": exp.Max,
    }[req.agg]
    return agg(this=col)


def _date_literal(value: date | str) -> exp.Expression:
    iso = value.isoformat() if isinstance(value, date) else str(value)
    return exp.Cast(this=exp.Literal.string(iso), to=exp.DataType.build("DATE"))


def _literal(value: Any, kind: str) -> exp.Expression:
    text = str(value)
    if kind == "date" or (not kind and _ISO_DATE.match(text)):
        return _date_literal(text)
    if kind == "number" or (not kind and _NUMBER.match(text)):
        return exp.Literal.number(text)
    return exp.Literal.string(text)


def _kind(dtype: str) -> str:
    if any(t in dtype for t in _DATE_TYPES):
        return "date"
    if any(t in dtype for t in _NUMERIC_TYPES):
        return "number"
    return "text" if dtype else ""


def _filter_predicate(f: FilterSpec, req: SeriesRequest, column_types: Optional[ColumnTypes]) -> exp.Expression:
    if f.table and f.table.lower() != req.table.lower():
        raise UnsupportedFilter(f"filter on {f.table}.{f.column} is not on the series table {req.table}")
    col = _column(f.column)
    kind = _kind(_type_of(column_types, req.table, f.column))
    values: List[Any] = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
    if f.op == "equals":
        return exp.EQ(this=col, expression=_literal(values[0], kind))
    if f.op == "in":
        return exp.In(this=col, expressions=[_literal(v, kind) for v in values])
    if f.op in ("gt", "gte", "lt", "lte"):
        cls = {"gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}[f.op]
        return cls(this=col, expression=_literal(values[0], kind))
    if f.op == "between":
        if len(values) != 2:
            raise UnsupportedFilter("between needs two values")
        return exp.Between(this=col, low=_literal(values[0], kind), high=_literal(values[1], kind))
    if f.op == "contains":
        return exp.ILike(this=col, expression=exp.Literal.string(f"%{values[0]}%"))
    raise UnsupportedFilter(f"unsupported operator {f.op!r}")


def _where(req: SeriesRequest, column_types: Optional[ColumnTypes], *, include_range: bool) -> Optional[exp.Expression]:
    preds: List[exp.Expression] = []
    date_col = _column(req.date_column)
    if include_range and req.start is not None:
        preds.append(exp.GTE(this=date_col, expression=_date_literal(req.start)))
    if include_range and req.end is not None:
        preds.append(exp.LT(this=date_col, expression=_date_literal(req.end)))
    for f in req.filters:
        preds.append(_filter_predicate(f, req, column_types))
    if not preds:
        return None
    return exp.and_(*preds)


# ── Public builders ───────────────────────────────────────────────────────────


def _agg_expr(agg: str, column: str, req: SeriesRequest, database_type: Optional[str],
              column_types: Optional[ColumnTypes]) -> exp.Expression:
    """AGG(column) with the Postgres money cast; ``*`` only with COUNT."""
    if column.strip() == "*":
        if agg != "count":
            raise ValueError("only COUNT may aggregate '*'")
        return exp.Count(this=exp.Star())
    col = _numeric_column(req.table, column, database_type, column_types)
    cls = {"sum": exp.Sum, "count": exp.Count, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[agg]
    return cls(this=col)


def build_series_expression(
    req: SeriesRequest,
    database_type: Optional[str] = None,
    *,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    extra_measures: Optional[Iterable[Tuple[str, str, str]]] = None,
) -> exp.Select:
    """The AST behind :func:`build_series_sql`, for callers that want to inspect it.

    ``extra_measures`` are ``(agg, column, alias)`` triples added as further
    aggregated columns (correlation's second measure). ``req.group_by`` adds a
    ``series_id`` column and groups by it, one series per distinct value.
    """
    trunc = _date_trunc(req)
    columns = [exp.alias_(trunc, "ts", quoted=True)]
    group_cols: List[exp.Expression] = [trunc.copy()]
    if req.group_by:
        gcol = exp.Cast(this=_column(req.group_by), to=exp.DataType.build("TEXT"))
        columns.append(exp.alias_(gcol, "series_id", quoted=True))
        group_cols.append(gcol.copy())
    columns.append(exp.alias_(_measure(req, database_type, column_types), "value", quoted=True))
    for agg, column, alias in (extra_measures or []):
        columns.append(exp.alias_(_agg_expr(agg, column, req, database_type, column_types), alias, quoted=True))
    select = (
        exp.select(*columns)
        .from_(_table(req, connection_schema=connection_schema, connection_catalog=connection_catalog, database_type=database_type))
        .group_by(*group_cols)
        .order_by(exp.Ordered(this=exp.column("ts", quoted=True)))
    )
    where = _where(req, column_types, include_range=True)
    if where is not None:
        select = select.where(where)
    return select


def build_series_sql(
    req: SeriesRequest,
    database_type: Optional[str] = None,
    *,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    dialect: Optional[str] = None,
    extra_measures: Optional[Iterable[Tuple[str, str, str]]] = None,
) -> str:
    """The aggregation query for ``req`` in the connection's dialect.

    ``dialect`` overrides the registry mapping (portability tests only); real
    callers pass the connection's ``database_type``.
    """
    tree = build_series_expression(
        req, database_type,
        connection_schema=connection_schema, connection_catalog=connection_catalog, column_types=column_types,
        extra_measures=extra_measures,
    )
    return tree.sql(dialect=dialect or sqlglot_dialect_for(database_type), identify=True, pretty=False)


def build_contribution_sql(
    req: SeriesRequest,
    dimensions: List[str],
    *,
    before: Tuple[date | str, date | str],
    after: Tuple[date | str, date | str],
    database_type: Optional[str] = None,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    dialect: Optional[str] = None,
) -> str:
    """Before/after totals per slice, one UNION ALL branch per dimension.

    Output columns: ``dimension, slice, period ('before'|'after'), value``.
    Both ranges are half-open. Slices are cast to text so the four branches
    union cleanly whatever the dimension's type.
    """
    date_col = _column(req.date_column)
    b_start, b_end = _date_literal(before[0]), _date_literal(before[1])
    a_start, a_end = _date_literal(after[0]), _date_literal(after[1])
    in_before = exp.and_(exp.GTE(this=date_col.copy(), expression=b_start), exp.LT(this=date_col.copy(), expression=b_end))
    in_after = exp.and_(exp.GTE(this=date_col.copy(), expression=a_start), exp.LT(this=date_col.copy(), expression=a_end))
    period = exp.Case(
        ifs=[exp.If(this=in_before.copy(), true=exp.Literal.string("before")),
             exp.If(this=in_after.copy(), true=exp.Literal.string("after"))],
    )
    filters = _where(req, column_types, include_range=False)
    branches: List[exp.Select] = []
    for dim in dimensions:
        slice_expr = exp.Cast(this=_column(dim), to=exp.DataType.build("TEXT"))
        select = (
            exp.select(
                exp.alias_(exp.Literal.string(dim), "dimension", quoted=True),
                exp.alias_(slice_expr, "slice", quoted=True),
                exp.alias_(period.copy(), "period", quoted=True),
                exp.alias_(_measure(req, database_type, column_types), "value", quoted=True),
            )
            .from_(_table(req, connection_schema=connection_schema, connection_catalog=connection_catalog, database_type=database_type))
            .group_by(slice_expr.copy(), period.copy())
        )
        window = exp.paren(exp.or_(in_before.copy(), in_after.copy()))
        select = select.where(exp.and_(window, filters.copy()) if filters is not None else window)
        branches.append(select)
    tree: exp.Expression = branches[0]
    for branch in branches[1:]:
        tree = exp.union(tree, branch, distinct=False)
    return tree.sql(dialect=dialect or sqlglot_dialect_for(database_type), identify=True, pretty=False)


def build_entity_sql(
    req: EntityRequest,
    *,
    database_type: Optional[str] = None,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    dialect: Optional[str] = None,
) -> str:
    """Tier B: one row per entity — key, features[, target] — under the filters,
    ordered by key so the row cap is deterministic. The runner's ``max_rows``
    (``req.row_cap``) bounds what leaves the database. Features and the target
    are numeric to the engine, so a Postgres ``money`` column is cast (and keeps
    its name) rather than arriving as text and being refused as non-numeric."""
    cols: List[exp.Expression] = [exp.alias_(_column(req.entity_key), "entity_key", quoted=True)]
    for feature in req.features:
        col = _numeric_column(req.table, feature, database_type, column_types)
        cols.append(exp.alias_(col, feature, quoted=True) if isinstance(col, exp.Cast) else col)
    if req.target:
        cols.append(exp.alias_(_numeric_column(req.table, req.target, database_type, column_types), "target", quoted=True))
    table = _table(req, connection_schema=connection_schema, connection_catalog=connection_catalog, database_type=database_type)
    select = exp.select(*cols).from_(table).order_by(exp.Ordered(this=_column(req.entity_key)))
    preds: List[exp.Expression] = []
    pseudo = SeriesRequest(table=req.table, date_column=req.entity_key, measure_column="*", agg="count", filters=req.filters)
    for f in req.filters:
        preds.append(_filter_predicate(f, pseudo, column_types))
    if preds:
        select = select.where(exp.and_(*preds))
    return select.sql(dialect=dialect or sqlglot_dialect_for(database_type), identify=True, pretty=False)


def build_cohort_sql(
    req: CohortRequest,
    *,
    database_type: Optional[str] = None,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    dialect: Optional[str] = None,
) -> str:
    """Tier A: aggregated cohort retention counts, no row-level data.

    Two UNION ALL branches, both grouped by the truncated signup cohort:
    * activity — ``cohort, period, COUNT(DISTINCT entity)`` per active period;
    * cohort size — ``cohort, NULL period, COUNT(DISTINCT entity)`` (the base).

    The period *offset* (0, 1, 2 …) is computed in the engine from the two
    truncated dates, so no ``DATE_DIFF`` (which does not transpile) is emitted.
    """
    key = _column(req.entity_key)
    cohort_trunc = exp.TimestampTrunc(unit=exp.Var(this=req.grain.upper()), this=_column(req.cohort_date))
    period_trunc = exp.TimestampTrunc(unit=exp.Var(this=req.grain.upper()), this=_column(req.activity_date))
    active = exp.Count(this=exp.Distinct(expressions=[key]))
    table = _table(req, connection_schema=connection_schema, connection_catalog=connection_catalog, database_type=database_type)
    pseudo = SeriesRequest(table=req.table, date_column=req.activity_date, measure_column="*", agg="count", filters=req.filters)
    preds = [_filter_predicate(f, pseudo, column_types) for f in req.filters]
    where = exp.and_(*preds) if preds else None

    activity = (
        exp.select(
            exp.alias_(cohort_trunc.copy(), "cohort", quoted=True),
            exp.alias_(period_trunc.copy(), "period", quoted=True),
            exp.alias_(active.copy(), "active", quoted=True),
        )
        .from_(table.copy())
        .group_by(cohort_trunc.copy(), period_trunc.copy())
    )
    size = (
        exp.select(
            exp.alias_(cohort_trunc.copy(), "cohort", quoted=True),
            exp.alias_(exp.Cast(this=exp.Null(), to=exp.DataType.build("DATE")), "period", quoted=True),
            exp.alias_(active.copy(), "active", quoted=True),
        )
        .from_(table.copy())
        .group_by(cohort_trunc.copy())
    )
    if where is not None:
        activity = activity.where(where.copy())
        size = size.where(where.copy())
    tree = exp.union(activity, size, distinct=False)
    return tree.sql(dialect=dialect or sqlglot_dialect_for(database_type), identify=True, pretty=False)


def build_experiment_sql(
    req: ExperimentRequest,
    *,
    database_type: Optional[str] = None,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    dialect: Optional[str] = None,
) -> str:
    """Tier A: one summary row per arm — ``arm, n, SUM(x), SUM(x*x)``.

    The count, first moment and second moment are all the two-proportion z-test
    (binary) and Welch's t-test (continuous) need, so the same query serves both
    and no row-level data leaves the database. Variance is reconstructed in the
    engine from the moments (``Var = (Σx² − n·mean²)/(n−1)``), which avoids the
    dialect-specific ``STDDEV`` spelling.
    """
    arm = _column(req.group_column)
    col: exp.Expression = _column(req.outcome_column)
    otype = _type_of(column_types, req.table, req.outcome_column)
    if (database_type or "").lower() in ("postgres", "postgresql") and any(t in otype for t in _MONEY_TYPES):
        col = exp.Cast(this=col, to=exp.DataType.build("DECIMAL"))
    sq = exp.Mul(this=col.copy(), expression=col.copy())
    table = _table(req, connection_schema=connection_schema, connection_catalog=connection_catalog, database_type=database_type)
    pseudo = SeriesRequest(table=req.table, date_column=req.group_column, measure_column="*", agg="count",
                           filters=req.filters)
    preds = [_filter_predicate(f, pseudo, column_types) for f in req.filters]
    select = (
        exp.select(
            exp.alias_(arm.copy(), "arm", quoted=True),
            exp.alias_(exp.Count(this=exp.Star()), "n", quoted=True),
            exp.alias_(exp.Sum(this=col.copy()), "sum_x", quoted=True),
            exp.alias_(exp.Sum(this=sq), "sum_x2", quoted=True),
        )
        .from_(table)
        .group_by(arm.copy())
    )
    if preds:
        select = select.where(exp.and_(*preds))
    return select.sql(dialect=dialect or sqlglot_dialect_for(database_type), identify=True, pretty=False)


def build_span_probe_sql(
    req: SeriesRequest,
    database_type: Optional[str] = None,
    *,
    connection_schema: Optional[str] = None,
    connection_catalog: Optional[str] = None,
    column_types: Optional[ColumnTypes] = None,
    dialect: Optional[str] = None,
) -> str:
    """One row: ``min_ts, max_ts, n`` under the same filters (no time range).

    Runs before the series query so the pre-SQL guards and the planner can
    infer grain and window from real numbers without pulling the series.
    """
    date_col = _column(req.date_column)
    select = exp.select(
        exp.alias_(exp.Min(this=date_col), "min_ts", quoted=True),
        exp.alias_(exp.Max(this=date_col), "max_ts", quoted=True),
        exp.alias_(exp.Count(this=exp.Star()), "n", quoted=True),
    ).from_(_table(req, connection_schema=connection_schema, connection_catalog=connection_catalog, database_type=database_type))
    where = _where(req, column_types, include_range=False)
    if where is not None:
        select = select.where(where)
    return select.sql(dialect=dialect or sqlglot_dialect_for(database_type), identify=True, pretty=False)


def describe_filters(filters: Iterable[FilterSpec]) -> str:
    """Human summary for provenance: ``Territory = Northwest; Category in (Bikes, Accessories)``."""
    parts: List[str] = []
    for f in filters:
        values = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
        if f.op == "in":
            parts.append(f"{f.column} in ({', '.join(str(v) for v in values)})")
        elif f.op == "between":
            parts.append(f"{f.column} between {values[0]} and {values[-1]}")
        elif f.op == "contains":
            parts.append(f"{f.column} contains {values[0]}")
        else:
            sym = {"equals": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(f.op, f.op)
            parts.append(f"{f.column} {sym} {values[0]}")
    return "; ".join(parts)


def filters_from_grounder(items: Iterable[Dict[str, Any]], series_table: str) -> Tuple[List[FilterSpec], List[str]]:
    """Convert ``resolved_filters`` items into :class:`FilterSpec`.

    Filters on other tables cannot be expressed without a join in v1; they are
    returned separately as human-readable strings so the planner can tell the
    user which ones were dropped.
    """
    kept: List[FilterSpec] = []
    dropped: List[str] = []
    for item in items or []:
        table = str(item.get("table") or "").strip()
        column = str(item.get("column") or "").strip()
        if not table or not column:
            continue
        op = str(item.get("op") or "equals").strip().lower()
        if op not in {"equals", "in", "gt", "gte", "lt", "lte", "between", "contains"}:
            dropped.append(f"{column} ({op})")
            continue
        if table.lower() != series_table.lower():
            dropped.append(f"{table}.{column}")
            continue
        kept.append(FilterSpec(table=table, column=column, op=op, value=item.get("value")))  # type: ignore[arg-type]
    return kept, dropped
