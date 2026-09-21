"""OnboardingService fail-soft: a failed merge must not look like a new user."""

from __future__ import annotations

from typing import Any, List

import pytest

from src.agent.onboarding import OnboardingService, _empty_state


class _FakeConn:
    def __init__(self, responses: List[Any], *, fail_sql: str | None = None):
        self.responses = list(responses)
        self.fail_sql = fail_sql
        self.calls: List[str] = []

    def _next(self):
        return self.responses.pop(0) if self.responses else None

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("column ftue_opted_out_at does not exist")
        return self._next()

    async def execute(self, sql, *args):
        self.calls.append(sql)
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("column ftue_opted_out_at does not exist")
        return "OK"


class _Acquire:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_merge_fail_soft_returns_current_row_not_empty():
    existing = _empty_state("9")
    existing["nudge_dismissed_at"] = "2026-09-11T00:00:00+00:00"
    existing["welcome_seen_at"] = "2026-09-15T00:00:00+00:00"

    conn = _FakeConn(
        [existing],  # get_or_create SELECT * after the merge INSERT fails
        fail_sql="ftue_opted_out_at",
    )
    svc = OnboardingService(_Pool(conn))

    row = await svc.merge("9", ftue_opted_out=True)

    assert row["user_id"] == "9"
    assert row["nudge_dismissed_at"] == "2026-09-11T00:00:00+00:00"
    assert row["welcome_seen_at"] == "2026-09-15T00:00:00+00:00"
    assert row.get("ftue_opted_out_at") is None
