"""Tests for conversation-history helpers.

`insight_text` guards the one place where the rich UI format and the text
database column meet: summaries may legitimately be highlight-fragment arrays,
and passing one straight to asyncpg fails the whole insight write.
"""

from __future__ import annotations

import pytest

from src.agent.conversation_history import insight_text


class TestInsightText:
    def test_plain_string_is_unchanged(self):
        assert insight_text("Revenue rose 12%") == "Revenue rose 12%"

    def test_fragment_array_is_joined_in_order(self):
        fragments = [
            {"t": "Each of the ", "hl": None},
            {"t": "25 salespeople", "hl": "num"},
            {"t": " sold at a profit.", "hl": "pos"},
        ]
        assert insight_text(fragments) == "Each of the 25 salespeople sold at a profit."

    def test_fragment_without_text_is_skipped_not_stringified(self):
        assert insight_text([{"hl": "num"}, {"t": "ok"}]) == "ok"

    def test_bare_strings_in_a_list_are_kept(self):
        assert insight_text(["a", "b"]) == "ab"

    def test_single_fragment_dict(self):
        assert insight_text({"t": "one", "hl": "accent"}) == "one"

    @pytest.mark.parametrize("value,expected", [(None, ""), (12, "12"), ([], "")])
    def test_edge_values(self, value, expected):
        assert insight_text(value) == expected
