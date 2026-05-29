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
from typing import Any, Dict, List

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

        logger.info("sqlglot_validate: SQL passed validation")
        return {"sqlglot_error": None}

    return sqlglot_validate


# ── dlp_check ─────────────────────────────────────────────────────────────────


def make_dlp_check(enabled: bool):
    """Return a sync ``dlp_check`` node.

    When *enabled*, scans the SQL for any pattern in ``_DLP_PATTERNS``.
    Blocked queries set ``dlp_blocked=True`` and populate ``governance_error``.
    """

    def dlp_check(state: AgentState) -> Dict[str, Any]:
        if not enabled:
            return {"dlp_blocked": False, "governance_error": None}

        sql = state.get("generated_sql") or ""
        match = _DLP_RE.search(sql)
        if match:
            error = (
                f"Query blocked by data governance policy: "
                f"references a governed keyword ('{match.group()}')."
            )
            logger.warning("dlp_check: BLOCKED — %s", error)
            return {"dlp_blocked": True, "governance_error": error}

        logger.info("dlp_check: SQL passed governance check")
        return {"dlp_blocked": False, "governance_error": None}

    return dlp_check
