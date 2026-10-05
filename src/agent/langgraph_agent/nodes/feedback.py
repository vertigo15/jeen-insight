"""feedback_classifier node — classifies errors and routes retries.

This is a pure-Python sync node.  It increments ``retry_count`` and sets
``feedback_type`` to one of:

  syntax        sqlglot found a parse/syntax error → sql_repair, then sql_generator.
  missing_table sqlglot found an unknown table → retry catalog_lookup + sql_generator.
  exec          PostgreSQL returned an execution error → sql_repair, then sql_generator
                (a timeout skips the repair and regenerates with a lighter-query note).
  semantic      fused_eval_analytics said answers_intent=False → retry sql_generator
                once (own budget); a second mismatch keeps the result.
  empty_recheck a suspicious 0-row result → regenerate SQL once (own budget).
  exhausted     retry_count has reached max_retries → route to response_formatter.

``use_local_repair`` is set alongside ``syntax`` / ``exec`` while the one-shot
``sql_repair_attempts`` budget lasts. ``max_retries`` stays the overall cap.

The graph routing function in graph.py reads ``feedback_type`` and
``use_local_repair`` to decide the next node.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)


# Retry budgets, independent of each other; ``max_retries`` still caps the total.
MAX_SQL_REPAIRS = 1
MAX_SEMANTIC_RETRIES = 1

_TIMEOUT_RE = re.compile(
    r"timed?[ -]?out|canceling statement|statement_timeout|deadline exceeded|exceeded the (?:time|execution) limit",
    re.IGNORECASE,
)
_TIMEOUT_GUIDANCE = (
    "The query timed out, so it scans or joins too much. Write a lighter query: "
    "filter early on indexed or date columns, aggregate before joining, drop "
    "unneeded joins and columns, and avoid cross joins and correlated subqueries."
)


def _is_timeout(error: str) -> bool:
    return bool(error and _TIMEOUT_RE.search(error))


_FALLBACK_KEYS = ("generated_sql", "query_result", "eval_result", "is_trivial")


def restore_earlier_result(state: AgentState, *, replace_current: bool = False) -> Optional[Dict[str, Any]]:
    """State updates that bring back the answer set aside by a semantic retry.

    ``None`` when there is nothing to restore, or when the retry produced a
    result of its own (unless ``replace_current``, for callers that know the
    retry produced no SQL at all). The restored answer is flagged low-confidence:
    the evaluator doubted it, but a retry that failed outright is no reason to
    throw it away.
    """
    saved = state.get("semantic_fallback")
    if not saved or not saved.get("query_result"):
        return None
    # A failed execution still stores its error payload as ``query_result``.
    retry_has_result = bool(state.get("query_result")) and not state.get("exec_error")
    if retry_has_result and not replace_current:
        return None
    return {
        **{key: saved.get(key) for key in _FALLBACK_KEYS},
        "sqlglot_error": None,
        "exec_error": None,
        "dlp_blocked": False,
        "governance_error": None,
        "clarification": None,
        "error_context": None,
        "low_confidence": True,
        "semantic_fallback": None,
    }


def _semantic_error_context(state: AgentState, eval_result: Dict[str, Any]) -> str:
    """Retry brief for a result the evaluator judged off-target.

    The evaluator's reason and the SQL that produced the result are the two
    facts a rewrite needs; without them the model tends to re-emit the same
    query. The reason is model text derived from result rows, so it is fenced as
    untrusted data like any other data-derived text injected into a prompt.
    """
    parts = [
        "The query result does not appear to answer the question: "
        f"'{state.get('question', '')}'"
    ]
    sql = (state.get("generated_sql") or "").strip()
    if sql:
        parts.append(f"The SQL that produced that result:\n{sql}")
    reason = str(eval_result.get("mismatch_reason") or "").strip()
    if reason:
        parts.append(fence_untrusted(reason, label="evaluator note on what was wrong"))
    parts.append(
        "Use a different approach — do not return the same query again."
    )
    return "\n\n".join(parts)


def make_feedback_classifier(max_retries: int):
    """Return a sync ``feedback_classifier`` node."""

    def feedback_classifier(state: AgentState) -> Dict[str, Any]:
        # ``use_local_repair`` is a per-pass decision; every other branch leaves
        # it False so a stale True can never re-route a later retry.
        return {"use_local_repair": False, **_classify(state)}

    def _classify(state: AgentState) -> Dict[str, Any]:
        retry_count = state.get("retry_count") or 0

        # A valid empty result with an unverified filter literal should first
        # re-enter deterministic value grounding. Keep this budget independent
        # of SQL-generation retries so a typo does not burn the syntax budget.
        if state.get("needs_filter_reground"):
            return {
                "feedback_type": "resolve_filters",
                "retry_count": retry_count,
                "error_context": (
                    "The query returned no rows and one or more filter values "
                    "could not be verified. Re-check those values before retrying."
                ),
            }

        # A suspicious empty result (an aggregate over data that exists dropped by
        # a bad JOIN/filter) regenerates SQL once. This runs BEFORE the retry
        # counter is touched so it uses its own one-pass budget
        # (``empty_result_diagnostics``); the flag is cleared here so a stale
        # value can never route back into this branch forever.
        if state.get("needs_sql_recheck"):
            return {
                "feedback_type": "empty_recheck",
                "retry_count": retry_count,
                "needs_sql_recheck": False,
                "error_context": state.get("empty_recheck_context") or (
                    "The query returned no rows, which is unexpected. Re-examine "
                    "the JOINs and WHERE filters and regenerate a corrected query."
                ),
            }

        new_retry_count = retry_count + 1

        # Check exhaustion FIRST so we never exceed the cap
        if new_retry_count > max_retries:
            logger.info(
                "feedback_classifier: retries exhausted (%d/%d) — routing to response_formatter",
                retry_count,
                max_retries,
            )
            restored = restore_earlier_result(state)
            if restored:
                logger.info("feedback_classifier: semantic retry failed — restoring the earlier result")
                return {"feedback_type": "exhausted", "retry_count": new_retry_count, **restored}
            update: Dict[str, Any] = {"feedback_type": "exhausted", "retry_count": new_retry_count}
            if not (state.get("eval_result") or {}).get("answers_intent", True):
                update["low_confidence"] = True
            return update

        sqlglot_error = state.get("sqlglot_error") or ""
        exec_error = state.get("exec_error") or ""
        eval_result = state.get("eval_result") or {}

        semantic_retries = state.get("semantic_retries") or 0
        if sqlglot_error:
            if "not found in catalog" in sqlglot_error.lower():
                feedback_type = "missing_table"
            else:
                feedback_type = "syntax"
            error_context = sqlglot_error
        elif exec_error:
            feedback_type = "exec"
            error_context = exec_error
            if _is_timeout(exec_error):
                error_context = f"{exec_error}\n\n{_TIMEOUT_GUIDANCE}"
        elif not eval_result.get("answers_intent", True):
            # A semantic rewrite costs a full generate + execute + evaluate pass
            # and rarely improves on a second attempt, so it has its own small
            # budget. When spent, the result stands, marked as low confidence.
            if (state.get("semantic_retries") or 0) >= MAX_SEMANTIC_RETRIES:
                logger.info("feedback_classifier: semantic budget spent — keeping the result")
                return {
                    "feedback_type": "exhausted",
                    "retry_count": retry_count,
                    "low_confidence": True,
                    "error_context": None,
                }
            feedback_type = "semantic"
            error_context = _semantic_error_context(state, eval_result)
            semantic_retries += 1
        else:
            # Nothing actionable to retry — give up
            feedback_type = "exhausted"
            error_context = state.get("error_context")
            logger.info("feedback_classifier: no actionable error — exhausted")
            restored = restore_earlier_result(state)
            if restored:
                return {"feedback_type": feedback_type, "retry_count": new_retry_count, **restored}
            return {
                "feedback_type": feedback_type,
                "retry_count": new_retry_count,
                "error_context": error_context,
            }

        logger.info(
            "feedback_classifier: type=%s, attempt %d/%d — error: %s",
            feedback_type,
            new_retry_count,
            max_retries,
            (error_context or "")[:120],
        )

        # A fault in the SQL text itself (parse error, unknown column, bad cast)
        # is cheapest to fix with a focused edit of that SQL once. Timeouts are
        # a shape problem, not a typo, and a repeat failure means the edit did
        # not help — both go to the full generator.
        sql_repair_attempts = state.get("sql_repair_attempts") or 0
        use_local_repair = (
            feedback_type in ("syntax", "exec")
            and bool(state.get("generated_sql"))
            and sql_repair_attempts < MAX_SQL_REPAIRS
            and not (feedback_type == "exec" and _is_timeout(exec_error))
        )
        if use_local_repair:
            sql_repair_attempts += 1

        update = {
            "feedback_type": feedback_type,
            "retry_count": new_retry_count,
            "error_context": error_context,
            "use_local_repair": use_local_repair,
            "sql_repair_attempts": sql_repair_attempts,
            "semantic_retries": semantic_retries,
        }
        if feedback_type == "semantic" and state.get("query_result"):
            update["semantic_fallback"] = {key: state.get(key) for key in _FALLBACK_KEYS}
        return update

    return feedback_classifier
