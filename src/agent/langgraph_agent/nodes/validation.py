"""SQL validation and data-loss-prevention nodes.

sqlglot_validate   Pure-Python node.  Parses SQL with sqlglot and checks table
                   names against the known catalog.
dlp_check          Pure-Python node.  Blocks SQL that references governed column
                   patterns.

Both are sync factory functions (no I/O, no LLM calls).
Extend ``_DLP_PATTERNS`` in this file to add or remove governance rules.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from src.agent.langgraph_agent.state import AgentState
from src.connectors.base import check_read_only_statements
from src.connectors.dialects import sqlglot_dialect_for
from src.metadata.identifiers import table_name_from_identifier

logger = logging.getLogger(__name__)


# ── DLP patterns ──────────────────────────────────────────────────────────────
# Each entry is a regex applied case-insensitively to the raw SQL text.
# Add or remove patterns here without changing any other file.

_DLP_PATTERNS: List[str] = [
    r"\bpassword[s]?\b",
    r"\bssn\b",
    r"\bsocial_security\b",
    r"\bcredit_card[s]?\b",
    r"\bcard_number\b",
    r"\bpin\b",
    r"\bsecret[s]?\b",
    r"\bprivate_key\b",
    r"\bapi_key\b",
    r"\baccess_token\b",
]

_DLP_RE = re.compile("|".join(_DLP_PATTERNS), re.IGNORECASE)


# ── sqlglot_validate ──────────────────────────────────────────────────────────


def make_sqlglot_validate(
    enabled: bool,
    require_catalog: bool = False,
    enforce_schema_qualifier: bool = True,
):
    """Return a sync ``sqlglot_validate`` node.

    Checks performed when *enabled* is True:
    1. Parse check — sqlglot must be able to parse the SQL.
    2. Structural safety — exactly one read-only query, no DML/DDL anywhere.
    3. Schema-qualifier check — a table qualified with a schema/catalog that
       doesn't match the connection's configured schema/catalog is rejected
       (blocks ``private.users`` sneaking through just because ``users`` is
       catalogued). Only enforced when the connection schema/catalog is known
       and *enforce_schema_qualifier* is True.
    4. Table existence check — every referenced table must be in ``known_tables``.
    5. Column existence check — conservative, only when columns are known.

    When *require_catalog* is True, an empty catalog fails closed (deny) rather
    than skipping the table check. When False (the default, used by standalone
    callers/tests), the table check is skipped for an empty catalog to avoid
    false positives during early startup.
    """

    def sqlglot_validate(state: AgentState) -> Dict[str, Any]:
        sql = state.get("generated_sql") or ""
        if not enabled or not sql.strip():
            return {"sqlglot_error": None}

        # Deny-by-default: refuse to validate/execute without a usable catalog.
        if require_catalog and not (state.get("known_tables") or []):
            error_msg = (
                "No catalog metadata is available for this connection, so the "
                "query cannot be safely validated."
            )
            logger.warning("sqlglot_validate: %s", error_msg)
            return {"sqlglot_error": error_msg}

        try:
            import sqlglot
            import sqlglot.errors
        except ImportError:
            logger.warning("sqlglot not installed — skipping SQL validation")
            return {"sqlglot_error": None}

        # 1. Parse check
        try:
            dialect = sqlglot_dialect_for(state.get("database_type"))
            stmts = sqlglot.parse(
                sql,
                dialect=dialect,
                error_level=sqlglot.errors.ErrorLevel.RAISE,
            )
        except sqlglot.errors.ParseError as exc:
            error_msg = f"SQL syntax error: {exc}"
            logger.info("sqlglot_validate: %s", error_msg)
            return {"sqlglot_error": error_msg}
        except Exception as exc:  # noqa: BLE001
            error_msg = f"SQL validation error: {exc}"
            logger.info("sqlglot_validate: %s", error_msg)
            return {"sqlglot_error": error_msg}

        if not stmts or stmts[0] is None:
            return {"sqlglot_error": "SQL could not be parsed — empty statement."}

        # 2. Structural safety — exactly one read-only query, no DML/DDL anywhere
        # (including inside CTEs). Mirrors the engine-level guard in the runner so
        # the failure is caught early and fed into the retry loop.
        structural_error = check_read_only_statements(stmts)
        if structural_error:
            logger.info("sqlglot_validate: %s", structural_error)
            return {"sqlglot_error": structural_error}

        # 3. Schema-qualifier check + 4. Table existence check.
        known = {
            normalized
            for table_name in (state.get("known_tables") or [])
            if (normalized := table_name_from_identifier(str(table_name)))
        }
        expected_schema = (state.get("connection_schema") or "").strip().lower()
        expected_catalog = (state.get("connection_catalog") or "").strip().lower()
        for stmt in stmts:
            if stmt is None:
                continue
            # Collect CTE alias names so we don't flag them as unknown tables
            cte_aliases = {
                (cte.alias or "").lower()
                for cte in stmt.find_all(sqlglot.exp.CTE)
                if cte.alias
            }
            for table in stmt.find_all(sqlglot.exp.Table):
                tname = (table.name or "").lower()
                if not tname or tname in cte_aliases:
                    continue

                # Schema-qualifier guard: a mismatched schema/catalog qualifier
                # (e.g. private.users when the connection lives in public) is a
                # cross-schema escape and must be rejected even if `users` is
                # catalogued. Only enforced when we know the expected value.
                if enforce_schema_qualifier:
                    tschema = (table.db or "").strip().lower()      # schema part
                    tcatalog = (table.catalog or "").strip().lower()  # catalog/db part
                    if tschema and expected_schema and tschema != expected_schema:
                        error_msg = (
                            f"Table '{table.sql()}' uses schema '{table.db}', which is "
                            f"not the allowed schema for this connection "
                            f"('{state.get('connection_schema')}')."
                        )
                        logger.warning("sqlglot_validate: %s", error_msg)
                        return {"sqlglot_error": error_msg}
                    if tcatalog and expected_catalog and tcatalog != expected_catalog:
                        error_msg = (
                            f"Table '{table.sql()}' uses catalog '{table.catalog}', which "
                            f"is not the allowed catalog for this connection "
                            f"('{state.get('connection_catalog')}')."
                        )
                        logger.warning("sqlglot_validate: %s", error_msg)
                        return {"sqlglot_error": error_msg}

                # Table existence check (only when catalog is populated).
                if known and tname not in known:
                    available = sorted(known)
                    preview = available[:20]
                    remainder = len(available) - len(preview)
                    suffix = f" (+{remainder} more)" if remainder else ""
                    error_msg = (
                        f"Table '{table.name}' not found in catalog. "
                        f"Available: {preview}{suffix}"
                    )
                    logger.info("sqlglot_validate: %s", error_msg)
                    return {"sqlglot_error": error_msg}

        # 5. Conservative column existence check (only when columns are known)
        table_columns = _normalise_table_columns(state.get("table_columns"))
        if table_columns:
            for stmt in stmts:
                if stmt is None:
                    continue
                col_error = _check_columns(stmt, table_columns, sqlglot)
                if col_error:
                    logger.info("sqlglot_validate: %s", col_error)
                    return {"sqlglot_error": col_error}

        filter_error = _check_resolved_filters(
            stmts,
            state.get("filter_plan"),
            table_columns,
            sqlglot,
        )
        if filter_error:
            logger.info("sqlglot_validate: %s", filter_error)
            return {"sqlglot_error": filter_error}

        logger.info("sqlglot_validate: SQL passed validation")
        return {"sqlglot_error": None}

    return sqlglot_validate


# ── Column-validation helpers ──────────────────────────────────────────────────


def _normalise_table_columns(raw: Any) -> Dict[str, Set[str]]:
    """Coerce the state's ``table_columns`` into ``{table: {col, …}}`` (lower)."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Set[str]] = {}
    for table, cols in raw.items():
        if not table or not cols:
            continue
        normalized = table_name_from_identifier(str(table))
        if normalized:
            out[normalized] = {str(c).lower() for c in cols}
    return out


def _check_columns(stmt, table_columns: Dict[str, Set[str]], sqlglot) -> Optional[str]:
    """Return an error string if a referenced column clearly doesn't exist.

    Intentionally conservative to avoid false positives:
      - Skipped entirely when the statement contains CTEs (derived columns are
        not in the catalog).
      - SELECT aliases are whitelisted (they're valid downstream references).
      - Qualified columns (``table.col``) are only checked when the qualifier
        matches a real catalog table name (aliases are skipped).
      - Unqualified columns are only checked for single-base-table queries
        (joins make ownership ambiguous).
    """
    # CTEs introduce columns we can't see in the catalog — skip to stay safe.
    if any(True for _ in stmt.find_all(sqlglot.exp.CTE)):
        return None

    # Whitelist SELECT aliases (e.g. ``SUM(x) AS total`` → ``total``).
    select_aliases: Set[str] = set()
    for alias in stmt.find_all(sqlglot.exp.Alias):
        name = (alias.alias or "").lower()
        if name:
            select_aliases.add(name)

    # Base tables referenced by their real catalog name.
    base_tables = {
        (t.name or "").lower()
        for t in stmt.find_all(sqlglot.exp.Table)
        if (t.name or "").lower() in table_columns
    }
    single_table = next(iter(base_tables)) if len(base_tables) == 1 else None

    for col in stmt.find_all(sqlglot.exp.Column):
        cname = (col.name or "").lower()
        if not cname or cname == "*" or cname in select_aliases:
            continue
        # Governed columns (e.g. 'password') are intentionally absent from the
        # catalog; let dlp_check own them instead of misreporting "not found"
        # (which would otherwise trigger a pointless catalog-retry loop).
        if _DLP_RE.search(cname):
            continue
        qualifier = (col.table or "").lower()
        if qualifier:
            # Only validate when the qualifier is a real table (not an alias).
            cols = table_columns.get(qualifier)
            if cols is not None and cname not in cols:
                return (
                    f"Column '{qualifier}.{col.name}' not found in catalog for "
                    f"table '{qualifier}'."
                )
        elif single_table is not None:
            cols = table_columns[single_table]
            if cname not in cols:
                return (
                    f"Column '{col.name}' not found in catalog for table "
                    f"'{single_table}'."
                )
    return None


def _literal_values(expression, sqlglot) -> List[str]:
    """Return scalar literal values below a SQLGlot expression."""
    if expression is None:
        return []
    literals = []
    if isinstance(expression, sqlglot.exp.Literal):
        literals.append(str(expression.this))
    literals.extend(
        str(literal.this)
        for literal in expression.find_all(sqlglot.exp.Literal)
    )
    return list(dict.fromkeys(literals))


def _same_value(expected: object, actual: str) -> bool:
    """Compare SQL literal values without accepting a numeric near-match."""
    expected_text = str(expected).strip()
    actual_text = str(actual).strip()
    if expected_text.casefold() == actual_text.casefold():
        return True
    try:
        from decimal import Decimal
        return Decimal(expected_text) == Decimal(actual_text)
    except Exception:  # noqa: BLE001 - text/date values are exact only
        return False


def _predicate_contains_target(predicate, table: str, column: str, table_columns, stmt, sqlglot) -> bool:
    """True when a predicate references a planned column (including table alias)."""
    aliases: Dict[str, str] = {}
    for table_expr in stmt.find_all(sqlglot.exp.Table):
        actual = (table_expr.name or "").lower()
        alias = (table_expr.alias or "").lower()
        if actual and alias:
            aliases[alias] = actual
    for col in predicate.find_all(sqlglot.exp.Column):
        if (col.name or "").lower() != column:
            continue
        qualifier = (col.table or "").lower()
        if not qualifier:
            owners = [t for t, cols in table_columns.items() if column in cols]
            if len(owners) == 1 and owners[0] == table:
                return True
        elif qualifier == table or aliases.get(qualifier) == table:
            return True
    return False


def _check_resolved_filters(
    stmts: List[Any],
    plan: Any,
    table_columns: Dict[str, Set[str]],
    sqlglot,
) -> Optional[str]:
    """Ensure generated SQL retained the grounder's verified predicates.

    This is intentionally conservative: only resolved filters are enforced and
    only simple comparison/IN/BETWEEN predicates are inspected. Ambiguous or
    unverified filters remain an LLM decision so a transient value-probe issue
    cannot turn into a false SQL rejection.
    """
    if not isinstance(plan, dict):
        return None
    filters = [
        item for item in (plan.get("filters") or [])
        if isinstance(item, dict) and item.get("resolved")
    ]
    if not filters:
        return None
    for item in filters:
        table = str(item.get("table") or "").lower()
        column = str(item.get("column") or "").lower()
        op = str(item.get("op") or "equals").lower()
        expected = item.get("value")
        if not table or not column:
            continue
        matched = False
        for stmt in stmts:
            if stmt is None:
                continue
            if op == "in":
                predicates = stmt.find_all(sqlglot.exp.In)
            elif op == "between":
                predicates = stmt.find_all(sqlglot.exp.Between)
            else:
                exp_name = {
                    "equals": "EQ", "gt": "GT", "gte": "GTE",
                    "lt": "LT", "lte": "LTE",
                }.get(op)
                predicate_type = getattr(sqlglot.exp, exp_name, None) if exp_name else None
                predicates = stmt.find_all(predicate_type) if predicate_type else []
            for predicate in predicates:
                if not _predicate_contains_target(
                    predicate, table, column, table_columns, stmt, sqlglot
                ):
                    continue
                if op == "in":
                    actual_values = _literal_values(predicate, sqlglot)
                    expected_values = expected if isinstance(expected, list) else [expected]
                    if all(any(_same_value(value, actual) for actual in actual_values) for value in expected_values):
                        matched = True
                        break
                elif op == "between":
                    values = expected if isinstance(expected, list) else []
                    actual_values = _literal_values(predicate, sqlglot)
                    if len(values) == 2 and all(
                        any(_same_value(value, actual) for actual in actual_values)
                        for value in values
                    ):
                        matched = True
                        break
                else:
                    actual_values = _literal_values(predicate, sqlglot)
                    if any(_same_value(expected, actual) for actual in actual_values):
                        matched = True
                        break
            if matched:
                break
        if not matched:
            return (
                f"Generated SQL did not preserve the verified filter "
                f"'{table}.{column}' ({op})."
            )
    return None


# ── dlp_check ─────────────────────────────────────────────────────────────────


def _resolve_referenced_columns(
    sql: str,
    table_columns: Dict[str, Set[str]],
    database_type: Optional[str] = None,
) -> Optional[Set[str]]:
    """Return the set of column names a query actually references, or None.

    Uses sqlglot to collect ``Column`` nodes and expands ``SELECT *`` (and
    ``t.*``) to the catalog columns of the referenced tables. Returns ``None``
    when sqlglot is unavailable or the SQL can't be parsed, signalling the
    caller to fall back to a coarse raw-text scan.
    """
    try:
        import sqlglot
    except ImportError:
        return None

    try:
        stmts = sqlglot.parse(sql, dialect=sqlglot_dialect_for(database_type))
    except Exception:  # noqa: BLE001
        return None
    if not stmts or stmts[0] is None:
        return None

    referenced: Set[str] = set()
    for stmt in stmts:
        if stmt is None:
            continue
        # Explicitly referenced columns.
        for col in stmt.find_all(sqlglot.exp.Column):
            name = (col.name or "").lower()
            if name and name != "*":
                referenced.add(name)
        # Expand any Star (SELECT * / t.*) to the catalog columns of the
        # tables referenced in the statement.
        if any(True for _ in stmt.find_all(sqlglot.exp.Star)):
            for t in stmt.find_all(sqlglot.exp.Table):
                tname = (t.name or "").lower()
                if tname in table_columns:
                    referenced |= table_columns[tname]
    return referenced


def _build_dlp_regex(extra_columns: Optional[List[str]]) -> "re.Pattern[str]":
    """Combine the built-in governed patterns with ops-provided column tags."""
    patterns = list(_DLP_PATTERNS)
    for col in extra_columns or []:
        name = (col or "").strip()
        if name:
            # Exact governed column name (word-boundary), regex-escaped.
            patterns.append(rf"\b{re.escape(name)}\b")
    return re.compile("|".join(patterns), re.IGNORECASE)


def make_dlp_check(enabled: bool, governed_columns: Optional[List[str]] = None):
    """Return a sync ``dlp_check`` node.

    When *enabled*, blocks queries that reference a governed column. The check
    is catalog/column-aware: it resolves the columns a query actually touches
    (expanding ``SELECT *`` against the catalog) and only blocks when one of
    them matches a governed pattern. When sqlglot can't parse the SQL it falls
    back to a coarse raw-text scan so governance is never silently skipped.

    *governed_columns* is an optional list of extra column names to treat as
    governed (in addition to the built-in patterns), letting ops tag sensitive
    columns via config without a code change.

    Blocked queries set ``dlp_blocked=True`` and populate ``governance_error``.
    """
    dlp_re = _build_dlp_regex(governed_columns)

    def dlp_check(state: AgentState) -> Dict[str, Any]:
        if not enabled:
            return {"dlp_blocked": False, "governance_error": None}

        sql = state.get("generated_sql") or ""
        table_columns = _normalise_table_columns(state.get("table_columns"))

        referenced = _resolve_referenced_columns(
            sql,
            table_columns,
            state.get("database_type"),
        )
        if referenced is not None:
            # Column-aware path: only block on an actual governed column.
            for col in referenced:
                if dlp_re.search(col):
                    error = (
                        f"Query blocked by data governance policy: "
                        f"references a governed column ('{col}')."
                    )
                    logger.warning("dlp_check: BLOCKED — %s", error)
                    return {"dlp_blocked": True, "governance_error": error}
            logger.info("dlp_check: SQL passed governance check (column-aware)")
            return {"dlp_blocked": False, "governance_error": None}

        # Fallback: coarse raw-text scan when the SQL couldn't be parsed.
        match = dlp_re.search(sql)
        if match:
            error = (
                f"Query blocked by data governance policy: "
                f"references a governed keyword ('{match.group()}')."
            )
            logger.warning("dlp_check: BLOCKED (raw scan) — %s", error)
            return {"dlp_blocked": True, "governance_error": error}

        logger.info("dlp_check: SQL passed governance check (raw scan)")
        return {"dlp_blocked": False, "governance_error": None}

    return dlp_check
