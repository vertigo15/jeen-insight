"""Unit tests for the shared LLM token accounting helper.

This is the one place the provider's per-call ``prompt_tokens`` /
``completion_tokens`` vocabulary is translated into the cumulative
``input_tokens`` / ``output_tokens`` totals carried in graph state, so the
translation and the "missing usage is free, not fatal" rule are pinned here.
"""

from __future__ import annotations

from src.agent.token_usage import merge_usage


class TestMergeUsage:
    def test_translates_provider_names_into_state_names(self):
        got = merge_usage(
            {}, {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        )
        assert got == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}

    def test_accumulates_across_calls(self):
        first = merge_usage(
            {}, {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        )
        second = merge_usage(
            first, {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
        )
        assert second == {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20}

    def test_missing_counts_are_free(self):
        assert merge_usage({}, {}) == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def test_explicit_nulls_are_free(self):
        """Some providers send the keys with no value rather than omitting them."""
        got = merge_usage(
            {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
        )
        assert got == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}

    def test_partial_running_total_is_tolerated(self):
        got = merge_usage({"input_tokens": 7}, {"prompt_tokens": 3})
        assert got == {"input_tokens": 10, "output_tokens": 0, "total_tokens": 0}

    def test_the_running_total_is_not_mutated(self):
        current = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        merge_usage(current, {"prompt_tokens": 9})
        assert current == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
