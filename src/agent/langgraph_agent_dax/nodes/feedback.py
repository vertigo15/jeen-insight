"""dax_feedback_router — DAX error taxonomy + retry routing with split budgets.

Replaces the SQL ``feedback_classifier``'s ``{syntax, missing_table, exec,
semantic, exhausted}`` with a DAX-native taxonomy and routes each category to a
repair strategy, honoring **separate** budgets so we never loop blindly:

    1 local repair / query, 2 plan regenerations, 1 catalog refresh,
    1 empty-result diagnostic, and 3 (inline) transport retries.

Categories → action:
  LEXICAL_OR_SHAPE / IDENTIFIER_KIND / MEASURE_OR_TYPE → local repair once, then regenerate.
  UNKNOWN_MODEL_OBJECT → refresh catalog once (model may be stale), else regenerate.
  CONTEXT_SEMANTICS / SEMANTIC_MISMATCH → regenerate from plan.
  RELATIONSHIP_PATH / TIME_SEMANTICS → replan (or clarify for business meaning).
  RESOURCE_LIMIT / PARTIAL_RESULT → regenerate with a "tighten grain/TOPN" note.
  EMPTY_OR_BLANK → resolve entities when a filter literal was never verified,
    else one diagnostic regeneration, then accept the empty result.
  AUTHN → transport (handled inline); AUTHZ_OR_TENANT → stop with config error.
  THROTTLED / TRANSIENT_SERVICE → stop once inline retries are spent.
  UNSAFE_OR_GOVERNED → block. EXHAUSTED → best safe explanation + clarification.

Actions map to next nodes in ``graph.py``:
  local_repair→dax_repair, regenerate→dax_generator, replan→dax_query_planner,
  resolve_entities→dax_entity_resolver, refresh_catalog→dax_catalog_lookup,
  clarify/exhausted→response_formatter.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from src.agent.langgraph_agent_dax.state import DaxAgentState

logger = logging.getLogger(__name__)

_MAX_PLAN_REGENERATIONS = 2
_MAX_CATALOG_REFRESH = 1
_MAX_LOCAL_REPAIRS = 1
# Entity resolution runs once in the normal path; this allows one broader retry
# triggered by an empty result.
_MAX_ENTITY_RESOLUTIONS = 2

# Keyword heuristics over the Power BI error message to sub-classify a 400.
_UNKNOWN_OBJECT_HINTS = (
    "cannot find", "couldn't find", "not found", "does not exist",
    "no column", "no measure", "unknown", "isn't recognized", "is not recognized",
)
_IDENTIFIER_KIND_HINTS = (
    "used as a", "cannot be used", "table of multiple", "single value",
    "a single value for column", "expects a", "scalar",
)
_RELATIONSHIP_HINTS = (
    "relationship", "ambiguous", "no relationship", "both directions", "cannot determine",
)
_TIME_HINTS = ("date", "time intelligence", "contiguous", "calendar", "datesytd", "sameperiod")
_CONTEXT_HINTS = ("context", "circular", "filter context", "row context")


def _classify(state: DaxAgentState) -> Tuple[str, str]:
    """Return ``(category, error_context)`` for the current failure."""
    result = state.get("query_result") or {}
    etype = (result.get("error_type") or "").lower()
    msg = (state.get("pbi_error_message") or state.get("exec_error") or "").strip()
    low = msg.lower()

    # Integrity / empty (set by result_integrity_check).
    if state.get("integrity_action") == "empty_diagnostic":
        return "EMPTY_OR_BLANK", state.get("error_context") or msg

    # Transport / auth (usually resolved inline; here means budget spent).
    if etype == "auth":
        return "AUTHN", msg
    if etype in ("forbidden", "not_found"):
        return "AUTHZ_OR_TENANT", msg
    if etype == "throttled":
        return "THROTTLED", msg
    if etype in ("service", "transport"):
        return "TRANSIENT_SERVICE", msg
    if etype == "read_only_blocked":
        return "UNSAFE_OR_GOVERNED", msg
    if etype in ("limit_exceeded",):
        return "RESOURCE_LIMIT", msg
    if etype == "partial_result":
        return "PARTIAL_RESULT", msg
    if etype == "empty":
        return "EMPTY_OR_BLANK", msg

    # Semantic feedback from eval (answers_intent=False).
    eval_result = state.get("eval_result") or {}
    if not result.get("error") and not eval_result.get("answers_intent", True):
        return "SEMANTIC_MISMATCH", (
            f"The result does not appear to answer: '{state.get('question', '')}'."
        )

    # A 400 / execution_error → sub-classify from the message text.
    if any(h in low for h in _RELATIONSHIP_HINTS):
        return "RELATIONSHIP_PATH", msg
    if any(h in low for h in _TIME_HINTS):
        return "TIME_SEMANTICS", msg
    if any(h in low for h in _IDENTIFIER_KIND_HINTS):
        return "MEASURE_OR_TYPE", msg
    if any(h in low for h in _UNKNOWN_OBJECT_HINTS):
        return "UNKNOWN_MODEL_OBJECT", msg
    if any(h in low for h in _CONTEXT_HINTS):
        return "CONTEXT_SEMANTICS", msg
    # Default: treat an engine 400 as a shape/lexical issue → local repair first.
    return "LEXICAL_OR_SHAPE", msg or "The DAX query was rejected by Power BI."


def make_dax_feedback_router(max_retries: int):
    """Return a sync ``dax_feedback_router`` node."""

    def dax_feedback_router(state: DaxAgentState) -> Dict[str, Any]:
        retry_count = int(state.get("retry_count") or 0)
        new_retry = retry_count + 1
        counts = dict(state.get("repair_attempts_by_category") or {})
        plan_regens = int(state.get("plan_regenerations") or 0)
        catalog_refresh = int(state.get("catalog_refresh_count") or 0)

        category, error_context = _classify(state)

        # Hard stops that never retry.
        if category == "UNSAFE_OR_GOVERNED":
            return _terminal(
                "This query was blocked because it is not read-only or references "
                "governed data.", category, new_retry, feedback="exhausted",
            )
        if category == "AUTHZ_OR_TENANT":
            return _terminal(
                state.get("pbi_error_message")
                or "Access to this Power BI dataset was denied. You need workspace "
                "access plus dataset Read + Build, and the tenant 'Dataset Execute "
                "Queries REST API' setting must be enabled.",
                category, new_retry, feedback="exhausted",
            )
        if category in ("AUTHN", "THROTTLED", "TRANSIENT_SERVICE"):
            return _terminal(
                state.get("pbi_error_message")
                or "Power BI could not complete the request after several retries. "
                "Please try again shortly.",
                category, new_retry, feedback="exhausted",
            )

        # Overall generation budget.
        if new_retry > max_retries:
            logger.info("dax_feedback_router: retries exhausted (%d/%d)", retry_count, max_retries)
            return _terminal(None, category, new_retry, feedback="exhausted")

        # Category → action with per-budget downgrades.
        action = "regenerate"
        if category in ("LEXICAL_OR_SHAPE", "IDENTIFIER_KIND", "MEASURE_OR_TYPE"):
            if counts.get("local_repair", 0) < _MAX_LOCAL_REPAIRS:
                counts["local_repair"] = counts.get("local_repair", 0) + 1
                action = "local_repair"
            else:
                action = "regenerate"
        elif category == "UNKNOWN_MODEL_OBJECT":
            if catalog_refresh < _MAX_CATALOG_REFRESH:
                catalog_refresh += 1
                action = "refresh_catalog"
            else:
                action = "regenerate"
        elif category in ("RELATIONSHIP_PATH", "TIME_SEMANTICS"):
            if plan_regens < _MAX_PLAN_REGENERATIONS:
                plan_regens += 1
                action = "replan"
            else:
                action = "regenerate"
        elif category in ("CONTEXT_SEMANTICS", "SEMANTIC_MISMATCH"):
            action = "regenerate"
        elif category in ("RESOURCE_LIMIT", "PARTIAL_RESULT"):
            action = "regenerate"
            error_context = (
                (error_context or "")
                + " Tighten the result: return an aggregate or a smaller bounded "
                "TOPN, add filters, and narrow the date range."
            )
        elif category == "EMPTY_OR_BLANK":
            # A zero-row result whose filters were never checked against real
            # column values is far more often a bad literal than bad DAX, and
            # regenerating the same literal cannot fix that.
            if (
                state.get("unresolved_entities")
                and int(state.get("entity_resolution_attempts") or 0) < _MAX_ENTITY_RESOLUTIONS
            ):
                action = "resolve_entities"
                error_context = (
                    (error_context or "")
                    + " One or more filter values were never verified against the "
                    "model; check them before regenerating."
                )
            else:
                action = "regenerate"

        logger.info(
            "dax_feedback_router: category=%s action=%s (retry %d/%d)",
            category, action, new_retry, max_retries,
        )
        return {
            "retry_count": new_retry,
            "dax_error_category": category,
            "dax_feedback_action": action,
            "feedback_type": category.lower(),
            "error_context": error_context,
            "repair_attempts_by_category": counts,
            "plan_regenerations": plan_regens,
            "catalog_refresh_count": catalog_refresh,
        }

    return dax_feedback_router


def _terminal(message, category: str, new_retry: int, *, feedback: str) -> Dict[str, Any]:
    updates: Dict[str, Any] = {
        "retry_count": new_retry,
        "dax_error_category": category,
        "dax_feedback_action": "exhausted",
        "feedback_type": feedback,
    }
    if message:
        updates["answer"] = message
        updates["error"] = message
    return updates


__all__ = ["make_dax_feedback_router"]
