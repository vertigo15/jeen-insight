"""history_lookup: window resolution, answer rendering, node and DB query text."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.agent.langgraph_agent.nodes.history import (
    make_history_search,
    render_history_answer,
    resolve_window,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class TestResolveWindow:
    def test_defaults_to_last_n_days_until_now(self):
        since, until = resolve_window({}, default_days=30, now=NOW)
        assert until > NOW and (until - NOW) < timedelta(minutes=2)
        assert since == until - timedelta(days=30)

    def test_bare_dates_are_inclusive_days(self):
        since, until = resolve_window({"since": "2026-09-13", "until": "2026-09-15"}, default_days=30, now=NOW)
        assert since == datetime(2026, 9, 13, tzinfo=timezone.utc)
        assert until == datetime(2026, 9, 16, tzinfo=timezone.utc)  # through the 15th

    def test_garbage_and_inverted_ranges_fall_back(self):
        since, until = resolve_window({"since": "yesterday-ish", "until": "2026-09-15"}, default_days=4, now=NOW)
        assert until == datetime(2026, 9, 16, tzinfo=timezone.utc) and since == until - timedelta(days=4)
        since, until = resolve_window({"since": "2026-09-20", "until": "2026-09-10"}, default_days=4, now=NOW)
        assert since < until

    def test_future_until_is_clamped(self):
        _, until = resolve_window({"until": "2030-01-01"}, default_days=30, now=NOW)
        assert until <= NOW + timedelta(days=1)


class TestRenderAnswer:
    def test_no_matches(self):
        text = render_history_answer([], keywords=["revenue"], since=NOW - timedelta(days=4), until=NOW,
                                     current_session="s1", limit=10)
        assert text.startswith('I couldn\'t find a question about "revenue"')

    def test_lists_matches_marks_current_conversation_and_trims_answers(self):
        matches = [
            {"session_id": "s1", "natural_language_query": "revenue by region", "created_at": NOW - timedelta(days=1),
             "answer": "North led " + "x" * 200},
            {"session_id": "s2", "natural_language_query": "revenue trend", "created_at": NOW - timedelta(days=3), "answer": ""},
        ]
        text = render_history_answer(matches, keywords=["revenue"], since=NOW - timedelta(days=4), until=NOW,
                                     current_session="s1", limit=10)
        assert text.startswith('Yes — 2 questions about "revenue"')
        assert '"revenue by region" (this conversation) → North led' in text
        assert "…" in text  # long answer trimmed
        assert '"revenue trend"' in text and "(this conversation)" not in text.split("\n")[2]


def test_node_queries_the_history_log_and_returns_answer_and_matches():
    service = MagicMock()
    service.search_turns = AsyncMock(return_value=[
        {"id": "q1", "session_id": "s1", "natural_language_query": "revenue by region",
         "created_at": NOW, "answer": "North led.", "execution_status": "success", "row_count": 12},
    ])
    node = make_history_search(service, default_days=30, max_results=10)
    out = asyncio.run(node({
        "user_id": "u1", "source_key": "conn", "session_id": "s1", "query_id": "q-current",
        "history_query": {"keywords": ["revenue"], "since": "2026-09-13", "until": None},
    }))
    kwargs = service.search_turns.await_args.kwargs
    assert kwargs["user_id"] == "u1" and kwargs["source_key"] == "conn" and kwargs["keywords"] == ["revenue"]
    assert kwargs["since"] == datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert kwargs["exclude_query_id"] == "q-current"          # a question never matches itself
    assert "revenue by region" in out["answer"]
    assert out["history_matches"] == [{
        "query_id": "q1", "session_id": "s1", "question": "revenue by region",
        "answer": "North led.", "status": "success", "row_count": 12, "asked_at": NOW.isoformat(),
    }]


def test_node_without_service_still_answers():
    node = make_history_search(None, default_days=7, max_results=5)
    out = asyncio.run(node({"history_query": {"keywords": ["churn"]}}))
    assert "couldn't find" in out["answer"] and out["history_matches"] == []
