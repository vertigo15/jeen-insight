"""Usage analytics read side: the SQL contracts (current thumb includes
'cleared', DAU counts only query/analysis events, questions exclude text-only
routes, every read is timeboxed and capped) and the response shaping. Offline:
the pool is a fake that records statements and returns scripted rows.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone

from src.analytics.usage_ledger import QUESTION_ROUTES, UsageAnalyticsRepository


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *args):
        self.pool.calls.append((" ".join(sql.split()), args))

    async def fetchrow(self, sql, *args):
        self.pool.calls.append((" ".join(sql.split()), args))
        return self.pool.rows.pop(0) if self.pool.rows else None

    async def fetch(self, sql, *args):
        self.pool.calls.append((" ".join(sql.split()), args))
        return self.pool.rows.pop(0) if self.pool.rows else []


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = list(rows or [])

    def acquire(self):
        return _Acquire(self)


def _run(coro):
    return asyncio.run(coro)


def _queries(pool):
    return [c for c in pool.calls if not c[0].startswith("SET LOCAL")]


# ── shared contracts ─────────────────────────────────────────────────────

def test_every_report_sets_a_local_statement_timeout():
    pool = FakePool()
    repo = UsageAnalyticsRepository(pool)
    for call in (
        lambda: repo.overview(30), lambda: repo.timeseries(7), lambda: repo.top_users(30),
        lambda: repo.top_connections(30), lambda: repo.feedback_feed(30),
        lambda: repo.analysis_usage(30), lambda: repo.error_breakdown(30),
    ):
        pool.calls.clear()
        _run(call())
        assert pool.calls[0][0] == f"SET LOCAL statement_timeout = '{repo.STATEMENT_TIMEOUT}'"


def test_current_thumb_is_the_newest_thumb_event_including_cleared():
    """Excluding 'cleared' would resurrect a withdrawn thumb (035 contract)."""
    pool = FakePool()
    _run(UsageAnalyticsRepository(pool).overview(30))
    thumbs_sql = [c[0] for c in pool.calls if "DISTINCT ON (query_id)" in c[0]][0]
    assert "thumb IS NOT NULL" in thumbs_sql
    assert "thumb <> 'cleared'" not in thumbs_sql and "thumb IN ('thumbs_up', 'thumbs_down')" not in thumbs_sql
    assert "ORDER BY query_id, id DESC" in thumbs_sql
    # Only real thumbs are counted after the newest event is chosen.
    assert "FILTER (WHERE thumb = 'thumbs_up')" in thumbs_sql
    assert "FILTER (WHERE thumb = 'thumbs_down')" in thumbs_sql


def test_active_users_count_only_query_and_analysis_events():
    pool = FakePool()
    _run(UsageAnalyticsRepository(pool).overview(30))
    activity_sql = [c[0] for c in pool.calls if "AS dau" in c[0]][0]
    assert "event_type IN ('query', 'analysis')" in activity_sql
    assert "'login'" not in activity_sql and "'feedback'" not in activity_sql


def test_questions_exclude_text_only_routes_via_the_route_allowlist():
    pool = FakePool()
    _run(UsageAnalyticsRepository(pool).overview(30))
    period_sql, args = [c for c in pool.calls if "AS questions" in c[0]][0]
    assert "route IS NULL OR route = ANY($3::text[])" in period_sql
    assert list(args[2]) == list(QUESTION_ROUTES)
    assert "NOT (route IS NULL OR route = ANY($3::text[]))" in period_sql   # text-only turns


def test_overview_windows_and_shapes_current_previous_and_activity():
    now = datetime.now(timezone.utc)
    period = {"questions": 10, "text_only_turns": 2, "active_users": 3, "active_connections": 1,
              "successes": 8, "errors": 2, "refused": 1, "avg_graph_time_ms": 1234.5, "total_tokens": 999,
              "logins": 4, "comments": 1, "feedback_events": 3, "avg_rating": 4.5,
              "thumbs_up_events": 2, "thumbs_down_events": 1}
    prev = dict(period, questions=5, successes=5, errors=0)
    pool = FakePool(rows=[period, prev, {"thumbs_up": 2, "thumbs_down": 0}, {"thumbs_up": 1, "thumbs_down": 1},
                          {"dau": 1, "wau": 2, "mau": 3}])
    out = _run(UsageAnalyticsRepository(pool).overview(30))
    assert out["days"] == 30 and (out["dau"], out["wau"], out["mau"]) == (1, 2, 3)
    assert out["current"]["success_rate"] == 0.8 and out["previous"]["success_rate"] == 1.0
    assert out["current"]["thumbs_up"] == 2 and out["previous"]["thumbs_down"] == 1
    assert out["current"]["avg_graph_time_ms"] == 1234.5 and out["current"]["total_tokens"] == 999
    # Current window is [start, now); previous is the same length right before it.
    cur_args = [c[1] for c in pool.calls if "AS questions" in c[0]]
    (p_start, p_end, _), (pp_start, pp_end, _) = cur_args[0], cur_args[1]
    assert (p_end - p_start).days == 30 and pp_end == p_start and (p_start - pp_start).days == 30
    assert p_end >= now


def test_timeseries_fills_every_day_from_generate_series_and_shapes_rows():
    pool = FakePool(rows=[[
        {"day": date(2026, 9, 24), "active_users": 2, "questions": 5, "errors": 1, "thumbs_up": 1, "thumbs_down": 0, "logins": 3},
        {"day": date(2026, 9, 25), "active_users": 0, "questions": 0, "errors": 0, "thumbs_up": 0, "thumbs_down": 0, "logins": 0},
    ]])
    out = _run(UsageAnalyticsRepository(pool).timeseries(7))
    sql, args = _queries(pool)[0]
    assert "generate_series($2::date, $3::date, INTERVAL '1 day')" in sql
    assert "(occurred_at AT TIME ZONE 'UTC')::date" in sql        # UTC day boundaries
    assert "LEFT JOIN e ON e.day = d.day" in sql
    assert out[0] == {"day": "2026-09-24", "active_users": 2, "questions": 5, "errors": 1, "thumbs_up": 1, "thumbs_down": 0, "logins": 3}
    assert out[1]["questions"] == 0


def test_top_users_joins_auth_users_best_effort_and_caps_limit():
    pool = FakePool(rows=[[{
        "user_id": "7", "name": "Dana", "email": "dana@x", "role": "editor", "questions": 12, "successes": 10,
        "errors": 2, "analyses": 1, "last_active": datetime(2026, 9, 25, tzinfo=timezone.utc),
        "thumbs_up": 3, "thumbs_down": 1, "avg_rating": 4.0,
    }]])
    out = _run(UsageAnalyticsRepository(pool).top_users(30, limit=500))
    sql, args = _queries(pool)[0]
    assert "LEFT JOIN auth_users au ON au.id::text = q.user_id" in sql
    assert args[1] == 100                                          # capped
    assert out[0]["name"] == "Dana" and out[0]["success_rate"] == 10 / 12
    assert out[0]["last_active"].startswith("2026-09-25")


def test_top_connections_reports_thumbs_down_rate():
    pool = FakePool(rows=[[{
        "source_key": "sales_db", "questions": 20, "distinct_users": 4, "successes": 18, "errors": 2,
        "avg_graph_time_ms": 900.0, "last_used": None, "thumbs_up": 3, "thumbs_down": 1,
    }]])
    out = _run(UsageAnalyticsRepository(pool).top_connections(30))
    assert out[0]["thumbs_down_rate"] == 0.25 and out[0]["success_rate"] == 0.9
    sql = _queries(pool)[0][0]
    assert "source_key IS NOT NULL" in sql and "LIMIT $2" in sql


def test_feedback_feed_filters_are_optional_and_pages_by_id_desc():
    pool = FakePool(rows=[[{
        "id": 42, "occurred_at": datetime(2026, 9, 25, tzinfo=timezone.utc), "user_id": "7", "name": None,
        "email": None, "source_key": "s", "thumb": "thumbs_down", "rating": None, "feedback_type": "report_bug",
        "message": "wrong total", "question": "revenue by month", "query_id": None,
    }]])
    repo = UsageAnalyticsRepository(pool)
    out = _run(repo.feedback_feed(30, thumb="thumbs_down", feedback_type="", connection=None, limit=1, before_id=100))
    sql, args = _queries(pool)[0]
    assert "ORDER BY e.id DESC" in sql and "($5::bigint IS NULL OR e.id < $5)" in sql
    assert args[1] == "thumbs_down" and args[2] is None and args[3] is None and args[4] == 100 and args[5] == 1
    assert out["items"][0]["id"] == 42 and out["items"][0]["message"] == "wrong total"
    assert out["next_before"] == 42                                # page was full → cursor
    pool.rows = [[]]
    assert _run(repo.feedback_feed(30, limit=50))["next_before"] is None


def test_analysis_usage_merges_runs_with_current_thumbs_on_ml_answers():
    pool = FakePool(rows=[
        [{"skill": "forecast", "runs": 5, "ok": 3, "guard_failed": 1, "errors": 1, "avg_execution_ms": 250.0, "distinct_users": 2}],
        [{"skill": "forecast", "thumbs_up": 2, "thumbs_down": 1}],
    ])
    out = _run(UsageAnalyticsRepository(pool).analysis_usage(30))
    runs_sql, thumbs_sql = [c[0] for c in _queries(pool)]
    assert "event_type = 'analysis'" in runs_sql and re.search(r"LIMIT \d+", runs_sql)
    assert "q.skill IS NOT NULL" in thumbs_sql and "DISTINCT ON (query_id)" in thumbs_sql
    assert out == [{"skill": "forecast", "runs": 5, "ok": 3, "guard_failed": 1, "errors": 1,
                    "avg_execution_ms": 250.0, "distinct_users": 2, "thumbs_up": 2, "thumbs_down": 1}]


def test_error_breakdown_only_lists_questions_that_failed_more_than_once():
    pool = FakePool(rows=[
        [{"error_type": "timeout", "source_key": "s", "failures": 4}],
        [{"question": "q", "failures": 2, "distinct_users": 1, "distinct_connections": 1, "last_seen": None}],
    ])
    out = _run(UsageAnalyticsRepository(pool).error_breakdown(30, limit=999))
    by_type_sql, questions_sql = [c[0] for c in _queries(pool)]
    assert "outcome = 'error'" in by_type_sql
    assert "HAVING COUNT(*) >= 2" in questions_sql
    assert _queries(pool)[1][1][1] == 50                          # capped at MAX_GROUPS
    assert out["by_type"][0]["error_type"] == "timeout" and out["top_failing_questions"][0]["failures"] == 2
