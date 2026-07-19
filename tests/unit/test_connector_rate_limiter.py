"""Cross-cutting tests: pure fixed-window rate-limit decision logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.connectors.rate_limiter import decide_fixed_window

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestFixedWindow:
    def test_zero_limit_disables(self):
        d = decide_fixed_window(now=_NOW, window_start=_NOW, count=999, limit=0, window_seconds=60)
        assert d.allowed is True

    def test_first_request_no_window(self):
        d = decide_fixed_window(now=_NOW, window_start=None, count=0, limit=5, window_seconds=60)
        assert d.allowed is True
        assert d.new_count == 1
        assert d.reset_window is True

    def test_window_elapsed_resets(self):
        old = _NOW - timedelta(seconds=120)
        d = decide_fixed_window(now=_NOW, window_start=old, count=100, limit=5, window_seconds=60)
        assert d.allowed is True
        assert d.new_count == 1
        assert d.reset_window is True

    def test_within_window_increments_allowed(self):
        ws = _NOW - timedelta(seconds=10)
        d = decide_fixed_window(now=_NOW, window_start=ws, count=3, limit=5, window_seconds=60)
        assert d.new_count == 4
        assert d.allowed is True
        assert d.reset_window is False

    def test_within_window_over_limit_blocked(self):
        ws = _NOW - timedelta(seconds=10)
        d = decide_fixed_window(now=_NOW, window_start=ws, count=5, limit=5, window_seconds=60)
        assert d.new_count == 6
        assert d.allowed is False

    def test_limit_of_one_blocks_second(self):
        ws = _NOW - timedelta(seconds=1)
        d = decide_fixed_window(now=_NOW, window_start=ws, count=1, limit=1, window_seconds=60)
        assert d.allowed is False
