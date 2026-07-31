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


__all__ = ["merge_usage"]
