"""DAX rule blocks for the text-to-DAX prompt (no driver dependencies).

Mirrors :mod:`src.connectors.dialects` for the SQL path but for DAX / Power BI.
DAX has one "dialect" (there is no per-engine variation the way there is for
SQL), so this module returns a single high-signal rule block that is injected
into the DAX system prompt. Keeping it here — dependency-free — lets the prompt
builder and any validator import it without pulling in a driver.
"""

from __future__ import annotations

# High-signal DAX authoring rules injected into the generator system prompt.
# These are the DAX analogues of the SQL dialect rules: they encode the failure
# modes that dominate DAX correctness (measure vs column, context transition,
# the single-EVALUATE/one-table executeQueries contract, deterministic ordering).
_DAX_RULES = (
    "Target language: DAX (Data Analysis Expressions) for Power BI, executed via "
    "the Power BI `executeQueries` REST endpoint.\n"
    "- Emit EXACTLY ONE query statement. It must be a single top-level `EVALUATE` "
    "returning ONE table, optionally preceded by a single `DEFINE` block. The "
    "endpoint returns only one result table; multiple `EVALUATE` statements or "
    "multiple result tables are rejected.\n"
    "- `DEFINE` may contain only `MEASURE` and `VAR` declarations. Never use "
    "`DEFINE COLUMN`/`DEFINE TABLE`, and never emit MDX (`WITH MEMBER`, `SELECT "
    "... ON COLUMNS`) or DMV (`$SYSTEM`, `DISCOVER`) — those are not DAX and are "
    "blocked.\n"
    "- Reference a COLUMN as `'Table'[Column]` (always single-quote the table). "
    "Reference a MEASURE as `[Measure]` (no table qualifier). Do not invent "
    "measures: prefer the curated measures listed under MEASURES; only aggregate a "
    "raw column when no suitable measure exists.\n"
    "- Wrap measures/columns from the row context in `CALCULATE(...)` when you "
    "need context transition (e.g. inside `ADDCOLUMNS`/`SELECTCOLUMNS` over a "
    "table). Aggregate raw columns with `SUM`/`AVERAGE`/`COUNTROWS`, never bare.\n"
    "- Prefer `SUMMARIZECOLUMNS(<group-by columns>, \"Name\", <measure expr>)` for "
    "grouped aggregates; `SUMMARIZECOLUMNS` applies filters and relationships "
    "automatically. Use `CALCULATETABLE` to apply filters to a table expression, "
    "and `ROW(\"Name\", <expr>)` for a single scalar result.\n"
    "- Apply filters with `FILTER`, `KEEPFILTERS`, or boolean filter arguments to "
    "`CALCULATE`/`CALCULATETABLE`. For dates, filter on the marked Date table and "
    "use time-intelligence functions (`DATESYTD`, `SAMEPERIODLASTYEAR`, "
    "`DATEADD`) rather than string math.\n"
    "- Deterministic output: when returning detail rows use "
    "`TOPN(<n>, <table>, <orderBy>, DESC)` and add an explicit `ORDER BY`. Row "
    "results are capped by the engine (100k rows / 1M values / ~15 MB); return "
    "aggregates or a bounded `TOPN`, never an unbounded detail dump.\n"
    "- Use the invariant argument separator `,` and `.` as the decimal separator. "
    "String literals use double quotes; escape a literal quote by doubling it "
    "(`\"\"`).\n"
    "- Example: EVALUATE SUMMARIZECOLUMNS('Date'[Calendar Year], \"Total Sales\", "
    "[Total Sales]) ORDER BY 'Date'[Calendar Year]"
)


def dax_dialect_rules() -> str:
    """Return the DAX rule block for the system prompt."""
    return _DAX_RULES


__all__ = ["dax_dialect_rules"]
