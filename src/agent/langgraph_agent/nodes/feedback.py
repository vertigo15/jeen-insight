"""feedback_classifier node — classifies errors and routes retries.

This is a pure-Python sync node.  It increments ``retry_count`` and sets
``feedback_type`` to one of:

  syntax        sqlglot found a parse/syntax error → retry sql_generator.
  missing_table sqlglot found an unknown table → retry catalog_lookup + sql_generator.
  exec          PostgreSQL returned an execution error → retry sql_generator.
  semantic      fused_eval_analytics said answers_intent=False → retry sql_generator.
  exhausted     retry_count has reached max_retries → route to response_formatter.

The graph routing function in graph.py reads ``feedback_type`` to decide the
next node.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)


def make_feedback_classifier(max_retries: int):
    """Return a sync ``feedback_classifier`` node."""

    def feedback_classifier(state: AgentState) -> Dict[str, Any]:
        retry_count = state.get("retry_count") or 0
        new_retry_count = retry_count + 1

        # Check exhaustion FIRST so we never exceed the cap
        if new_retry_count > max_retries:
            logger.info(
                "feedback_classifier: retries exhausted (%d/%d) — routing to response_formatter",
                retry_count,
                max_retries,
            )
            return {
                "feedback_type": "exhausted",
                "retry_count": new_retry_count,
            }

        sqlglot_error = state.get("sqlglot_error") or ""
        exec_error = state.get("exec_error") or ""
        eval_result = state.get("eval_result") or {}

        if sqlglot_error:
            if "not found in catalog" in sqlglot_error.lower():
                feedback_type = "missing_table"
            else:
                feedback_type = "syntax"
            error_context = sqlglot_error
        elif exec_error:
            feedback_type = "exec"
            error_context = exec_error
        elif not eval_result.get("answers_intent", True):
            feedback_type = "semantic"
            error_context = (
                f"The query result does not appear to answer the question: "
                f"'{state.get('question', '')}'"
            )
        else:
            # Nothing actionable to retry — give up
            feedback_type = "exhausted"
            error_context = state.get("error_context")
            logger.info("feedback_classifier: no actionable error — exhausted")
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

        return {
            "feedback_type": feedback_type,
            "retry_count": new_retry_count,
            "error_context": error_context,
        }

    return feedback_classifier
