"""Unit tests for the shared LLM token accounting helper.

This is the one place the provider's per-call ``prompt_tokens`` /
``completion_tokens`` vocabulary is translated into the cumulative
``input_tokens`` / ``output_tokens`` totals carried in graph state, so the
translation and the "missing usage is free, not fatal" rule are pinned here.
"""

from __future__ import annotations

import asyncio

from src.agent.langgraph_agent.graph import _timed as sql_timed
from src.agent.langgraph_agent_dax.graph import _timed as dax_timed
from src.agent.token_usage import merge_usage, usage_delta


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


class TestUsageDelta:
    """A step's own tokens and model time, taken from the running totals."""

    BEFORE = {"token_usage": {"input_tokens": 100, "output_tokens": 10}, "llm_latency_ms": 900}

    def test_reports_what_the_step_added(self):
        after = {"token_usage": {"input_tokens": 3112, "output_tokens": 68, "total_tokens": 3180}, "llm_latency_ms": 1300}
        assert usage_delta(self.BEFORE, after) == {"input_tokens": 3012, "output_tokens": 58, "llm_ms": 400}

    def test_a_step_without_a_model_call_adds_nothing(self):
        assert usage_delta(self.BEFORE, {"generated_sql": "select 1"}) == {}
        unchanged = {"token_usage": dict(self.BEFORE["token_usage"]), "llm_latency_ms": 900}
        assert usage_delta(self.BEFORE, unchanged) == {}

    def test_first_call_starts_from_empty_totals(self):
        after = {"token_usage": {"input_tokens": 50, "output_tokens": 20}, "llm_latency_ms": 120}
        assert usage_delta({}, after) == {"input_tokens": 50, "output_tokens": 20, "llm_ms": 120}


def _llm_node(state):
    return {"token_usage": merge_usage(state.get("token_usage") or {}, {"prompt_tokens": 40, "completion_tokens": 7}),
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + 250}


async def _async_llm_node(state):
    return _llm_node(state)


def test_timed_nodes_record_their_own_usage_in_the_trace():
    state = {"token_usage": {"input_tokens": 10, "output_tokens": 1}, "llm_latency_ms": 100}
    for timed in (sql_timed, dax_timed):
        for fn in (_llm_node, _async_llm_node):
            wrapped = timed("fused_router", fn)
            out = asyncio.run(wrapped(state)) if asyncio.iscoroutinefunction(wrapped) else wrapped(state)
            event = out["trace"][0]
            assert (event["input_tokens"], event["output_tokens"], event["llm_ms"]) == (40, 7, 250), (timed, fn)
    plain = sql_timed("prompt_builder", lambda s: {"system_prompt": "x"})(state)["trace"][0]
    assert not {"input_tokens", "output_tokens", "llm_ms"} & set(plain)
