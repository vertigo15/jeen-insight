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


def make_sqlglot_validate(enabled: bool):
    """Return a sync ``sqlglot_validate`` node.

    Two checks are performed when *enabled* is True:
    1. Parse check — sqlglot must be able to parse the SQL.
    2. Table existence check — every referenced table must be in ``known_tables``.
       (Skipped when the catalog is empty to avoid false positives during
       early startup or unit tests.)
    """

    def sqlglot_validate(state: AgentState) -> Dict[str, Any]:
        sql = state.get("generated_sql") or ""
        if not enabled or not sql.strip():
            return {"sqlglot_error": None}

        try:
            import sqlglot
            import sqlglot.errors
        except ImportError:
            logger.warning("sqlglot not installed — skipping SQL validation")
            return {"sqlglot_error": None}

        # 1. Parse check
        try:
            stmts = sqlglot.parse(sql, dialect="postgres", error_level=sqlglot.errors.ErrorLevel.RAISE)
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

        # 2. Table existence check (only when catalog is populated)
        known = {t.lower() for t in (state.get("known_tables") or [])}
        if known:
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
                    if tname and tname not in known and tname not in cte_aliases:
                        error_msg = (
                            f"Table '{table.name}' not found in catalog. "
                            f"Available: {sorted(known)}"
                        )
                        logger.info("sqlglot_validate: %s", error_msg)
                        return {"sqlglot_error": error_msg}

        # 3. Conservative column existence check (only when columns are known)
        table_columns = _normalise_table_columns(state.get("table_columns"))
        if table_columns:
            for stmt in stmts:
                if stmt is None:
                    continue
                col_error = _check_columns(stmt, table_columns, sqlglot)
                if col_error:
                    logger.info("sqlglot_validate: %s", col_error)
                    return {"sqlglot_error": col_error}

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
        out[str(table).lower()] = {str(c).lower() for c in cols}
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


# ── dlp_check ─────────────────────────────────────────────────────────────────


def _resolve_referenced_columns(
    sql: str, table_columns: Dict[str, Set[str]]
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
        stmts = sqlglot.parse(sql, dialect="postgres")
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


def make_dlp_check(enabled: bool):
    """Return a sync ``dlp_check`` node.

    When *enabled*, blocks queries that reference a governed column. The check
    is catalog/column-aware: it resolves the columns a query actually touches
    (expanding ``SELECT *`` against the catalog) and only blocks when one of
    them matches a governed pattern. When sqlglot can't parse the SQL it falls
    back to a coarse raw-text scan so governance is never silently skipped.

    Blocked queries set ``dlp_blocked=True`` and populate ``governance_error``.
    """

    def dlp_check(state: AgentState) -> Dict[str, Any]:
        if not enabled:
            return {"dlp_blocked": False, "governance_error": None}

        sql = state.get("generated_sql") or ""
        table_columns = _normalise_table_columns(state.get("table_columns"))

        referenced = _resolve_referenced_columns(sql, table_columns)
        if referenced is not None:
            # Column-aware path: only block on an actual governed column.
            for col in referenced:
                if _DLP_RE.search(col):
                    error = (
                        f"Query blocked by data governance policy: "
                        f"references a governed column ('{col}')."
                    )
                    logger.warning("dlp_check: BLOCKED — %s", error)
                    return {"dlp_blocked": True, "governance_error": error}
            logger.info("dlp_check: SQL passed governance check (column-aware)")
            return {"dlp_blocked": False, "governance_error": None}

        # Fallback: coarse raw-text scan when the SQL couldn't be parsed.
        match = _DLP_RE.search(sql)
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
