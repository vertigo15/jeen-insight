"""Cumulative LLM token accounting, shared by both graphs.

Every node that calls a model folds what that call cost into a running total
carried in graph state. This lives outside either graph tree because token
accounting is engine-neutral: the text-to-SQL and text-to-DAX graphs count the
same way, and neither should have to import the other to do it.

The translation matters. Providers report ``prompt_tokens`` /
``completion_tokens`` per call, while state carries ``input_tokens`` /
``output_tokens`` totals for the whole question; this is the one place that
mapping is written down.
"""

from __future__ import annotations

from typing import Any, Dict


def merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    """Add one call's provider-reported usage to the running totals.

    Missing and ``None`` counts are treated as zero: a provider that omits usage
    should cost a question its accounting, not its answer.
    """
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


def usage_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, int]:
    """One step's own share of the running totals, for its trace event.

    ``before`` is the state a node received and ``after`` the update it
    returned. Both carry question-wide totals, so the difference is what this
    step's model calls cost: ``input_tokens``, ``output_tokens`` and ``llm_ms``
    (time spent waiting on the model). A key is present only when it grew.
    """
    fields: Dict[str, int] = {}
    new_usage = after.get("token_usage")
    if isinstance(new_usage, dict):
        old_usage = before.get("token_usage") or {}
        for key in ("input_tokens", "output_tokens"):
            gained = int(new_usage.get(key) or 0) - int(old_usage.get(key) or 0)
            if gained > 0:
                fields[key] = gained
    if after.get("llm_latency_ms") is not None:
        gained_ms = int(after.get("llm_latency_ms") or 0) - int(before.get("llm_latency_ms") or 0)
        if gained_ms > 0:
            fields["llm_ms"] = gained_ms
    return fields


__all__ = ["merge_usage", "usage_delta"]
