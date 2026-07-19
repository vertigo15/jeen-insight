"""Dialect metadata shared across the app (no driver dependencies).

This module is intentionally dependency-free so it can be imported from the
LangGraph validation node and prompt builder without pulling in any database
driver. It maps a connection's ``database_type`` (the normalised, lower-cased
``service_type``) to:

* the sqlglot dialect used to parse/validate generated SQL, and
* a short block of engine-specific SQL rules injected into the system prompt.

Add a new engine by extending ``_SQLGLOT_DIALECT`` and ``_DIALECT_RULES`` here,
plus a runner in this package and an entry in ``factory.SERVICE_TYPE_ALIASES``.
"""

from __future__ import annotations

# Canonical database_type -> sqlglot dialect name.
# sqlglot ships dialects for all of these; an unknown/empty value falls back to
# sqlglot's default parser (dialect=None) which is the most permissive.
_SQLGLOT_DIALECT: dict[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "trino": "trino",
    "presto": "presto",
    "databricks": "databricks",
    "spark": "spark",
    "spark2": "spark2",
}


# Engine-specific guidance appended to the system prompt so the model emits SQL
# in the correct dialect. Keep these short and high-signal — they are prepended
# to the schema in every SQL-generation call.
_DIALECT_RULES: dict[str, str] = {
    "postgres": (
        "Target dialect: PostgreSQL.\n"
        '- Quote mixed-case or reserved identifiers with double quotes ("DimProduct"); '
        "unquoted identifiers are folded to lowercase. Strings use single quotes.\n"
        "- Cast with value::type or CAST(value AS type).\n"
        "- Use ILIKE for case-insensitive matching and LIMIT n for row limits.\n"
        "- Money handling: `money`<->numeric/integer casts are NOT implicit in "
        "expressions, so mixing a `money` column with a plain number raises "
        '"cannot convert type money to integer" (or the reverse). Before mixing a '
        "money column with numeric values or using it in CASE, COALESCE, AVG, ROUND, "
        "or ratios, cast the money inputs to numeric (never integer, to preserve "
        'precision): CAST("SalesAmount" AS numeric) or "SalesAmount"::numeric. Keep '
        'every CASE/COALESCE branch numeric, e.g. COALESCE("SalesAmount"::numeric, 0). '
        'Ratios: ("cur"::numeric - "prev"::numeric) / NULLIF("prev"::numeric, 0); '
        "rounding: ROUND(value::numeric, 2). Note: SUM(money), money +/- money, and "
        "money-to-money comparisons work natively without casting.\n"
        '- Example: SELECT "ProductKey" FROM "DimProduct" LIMIT 10'
    ),
    "trino": (
        "Target dialect: Trino (Presto SQL).\n"
        "- Reference tables as catalog.schema.table when catalog/schema are provided. "
        'Quote mixed-case or reserved identifiers with double quotes ("DimProduct"). Strings use single quotes.\n'
        "- Cast with CAST(value AS type) (Postgres :: casting is not supported).\n"
        "- Use approx_distinct() for large distinct counts and LIMIT n for row limits.\n"
        "- Date math examples: date_add('day', 7, order_date), date_diff('day', start_date, end_date).\n"
        '- Example: SELECT "ProductKey" FROM catalog.schema."DimProduct" LIMIT 10'
    ),
    "databricks": (
        "Target dialect: Databricks SQL (Spark SQL).\n"
        "- Reference tables as catalog.schema.table when catalog/schema are provided. "
        "Quote mixed-case or reserved identifiers with backticks (`DimProduct`). Strings use single quotes.\n"
        "- Cast with CAST(value AS type) (Postgres :: casting is not supported).\n"
        "- Use LIMIT n for row limits.\n"
        "- Date math examples: date_add(order_date, 7), datediff(end_date, start_date), date_trunc('MONTH', order_date).\n"
        "- Example: SELECT `ProductKey` FROM catalog.schema.`DimProduct` LIMIT 10"
    ),
}
_DIALECT_RULES["postgresql"] = _DIALECT_RULES["postgres"]
_DIALECT_RULES["presto"] = _DIALECT_RULES["trino"]
_DIALECT_RULES["spark"] = _DIALECT_RULES["databricks"]
_DIALECT_RULES["spark2"] = _DIALECT_RULES["databricks"]


def sqlglot_dialect_for(database_type: str | None) -> str | None:
    """Return the sqlglot dialect name for a ``database_type`` (or None).

    ``None`` tells sqlglot to use its permissive default parser, which is the
    safest fallback for an unknown engine.
    """
    if not database_type:
        return None
    return _SQLGLOT_DIALECT.get(database_type.strip().lower())


def dialect_rules_for(database_type: str | None) -> str:
    """Return a short block of engine-specific SQL rules for the system prompt.

    Falls back to a neutral, engine-agnostic instruction when the engine is not
    explicitly known so the prompt always renders.
    """
    if database_type:
        rules = _DIALECT_RULES.get(database_type.strip().lower())
        if rules:
            return rules
    label = (database_type or "SQL").strip() or "SQL"
    return (
        f"Target dialect: {label}.\n"
        "- Generate standard ANSI SQL and use LIMIT n to cap rows."
    )
