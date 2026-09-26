"""Durable usage ledger (``insights_usage_events``, migration 036) and the
read side that powers the admin Analytics page.

Why a separate table: conversation retention deletes old conversations and
cascades into turns and ``insights_answer_feedback``. The ledger has NO foreign
key to the turn, so usage/feedback history survives.

Writers (:class:`UsageLedger`) are fire-and-forget: they never raise into the
caller (a failed analytics row must not cost the user an answer) and are no-ops
when the migration is not applied or the feature is disabled. Every write is
idempotent through the partial UNIQUE indexes + ``ON CONFLICT DO NOTHING``.

Readers (:class:`UsageAnalyticsRepository`) run every report inside a
transaction with a local ``statement_timeout`` and cap grouped results.

Metric definitions (mirrored in the API response models and UI tooltips):

* *active user* — distinct ``user_id`` with at least one ``query`` or
  ``analysis`` event in the window (UTC day boundaries). Logins are a separate
  series and never count towards DAU.
* *question* — a ``query`` event whose route is a SQL/ML route
  (:data:`QUESTION_ROUTES`) or unknown. Greetings, capability answers,
  clarifications and memory answers are *text-only turns*.
* *success rate* — ``success / (success + error)`` over questions; ``refused``
  (an ML guard declined) is reported separately.
* *current thumb* of a turn — its newest ``feedback`` event with a non-NULL
  ``thumb``, **including** ``cleared`` (which means "no thumb"). Ignoring
  ``cleared`` would resurrect a withdrawn thumb.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# Routes counted as questions. 'sql' / 'text' are the values the 036 backfill
# assigns to turns recorded before the ledger existed.
QUESTION_ROUTES = ("needs_query", "needs_analysis", "sql")

# Keys allowed in ``detail`` — names and categories only, never values.
DETAIL_ALLOWLIST = frozenset(
    {"method", "refused_by", "connector_error_type", "low_confidence", "runner"}
)

QUESTION_EXCERPT_CHARS = 500
PRUNE_BATCH = 5000
# Arbitrary, stable key for pg_try_advisory_lock so replicas don't prune together.
PRUNE_LOCK_KEY = 0x4A45454E_0036  # "JEEN" + migration number

_QUESTION_SQL = "(route IS NULL OR route = ANY($__ROUTES__))"


def _as_uuid(value: Any) -> Optional[UUID]:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _excerpt(text: Any) -> Optional[str]:
    if not text:
        return None
    s = str(text).strip()
    return s[:QUESTION_EXCERPT_CHARS] if s else None


def filter_detail(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only allowlisted, scalar-ish keys. Guard names are kept, values dropped."""
    if not isinstance(detail, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in DETAIL_ALLOWLIST:
        if key not in detail:
            continue
        value = detail[key]
        if key == "refused_by" and isinstance(value, list):
            names = []
            for item in value:
                if isinstance(item, dict):
                    name = item.get("guard") or item.get("name")
                    if name:
                        names.append(str(name))
                elif item:
                    names.append(str(item))
            out[key] = names[:10]
        elif isinstance(value, (str, int, float, bool)):
            out[key] = value if not isinstance(value, str) else value[:120]
    return out


class UsageLedger:
    """Write side of the ledger. Best-effort; never raises."""

    def __init__(self, pool: Any, *, schema_ready: bool = True, enabled: bool = True) -> None:
        self.pool = pool
        self.schema_ready = bool(schema_ready)
        self.enabled = bool(enabled)

    @property
    def active(self) -> bool:
        return self.enabled and self.schema_ready and self.pool is not None

    async def _execute(self, sql: str, *args: Any) -> None:
        if not self.active:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(sql, *args)
        except Exception:  # noqa: BLE001 — analytics must never cost the answer
            logger.debug("usage ledger write failed", exc_info=True)

    async def record_query(
        self,
        *,
        user_id: str,
        source_key: Optional[str],
        query_id: Any,
        session_id: Any = None,
        outcome: str,
        error_type: Optional[str] = None,
        route: Optional[str] = None,
        skill: Optional[str] = None,
        llm_model: Optional[str] = None,
        token_usage: Optional[Dict[str, Any]] = None,
        llm_latency_ms: Any = None,
        execution_time_ms: Any = None,
        graph_time_ms: Any = None,
        row_count: Any = None,
        question: Any = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        qid = _as_uuid(query_id)
        if not user_id or qid is None:
            return
        tokens = token_usage or {}
        await self._execute(
            """
            INSERT INTO insights_usage_events (
                event_type, user_id, source_key, query_id, session_id,
                outcome, error_type, route, skill, llm_model,
                total_tokens, input_tokens, llm_latency_ms, execution_time_ms,
                graph_time_ms, row_count, question, detail
            ) VALUES (
                'query', $1, $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12, $13,
                $14, $15, $16, $17::jsonb
            )
            ON CONFLICT DO NOTHING
            """,
            str(user_id),
            (str(source_key) or None) if source_key else None,
            qid,
            _as_uuid(session_id),
            outcome if outcome in ("success", "error", "refused") else "error",
            (str(error_type)[:64] if error_type else None),
            (str(route)[:40] if route else None),
            (str(skill)[:64] if skill else None),
            (str(llm_model)[:128] if llm_model else None),
            _as_int(tokens.get("total_tokens")),
            _as_int(tokens.get("input_tokens")),
            _as_int(llm_latency_ms),
            _as_int(execution_time_ms),
            _as_int(graph_time_ms),
            _as_int(row_count),
            _excerpt(question),
            json.dumps(filter_detail(detail)),
        )

    async def record_feedback(
        self,
        *,
        user_id: str,
        source_key: Optional[str],
        query_id: Any,
        feedback_id: Any,
        thumb: Optional[str] = None,
        rating: Any = None,
        feedback_type: Optional[str] = None,
        message: Optional[str] = None,
        question: Any = None,
        session_id: Any = None,
    ) -> None:
        qid = _as_uuid(query_id)
        fid = _as_uuid(feedback_id)
        if not user_id or qid is None or fid is None:
            return
        if thumb not in (None, "thumbs_up", "thumbs_down", "cleared"):
            thumb = None
        msg = (str(message).strip() or None) if message else None
        if msg and len(msg) > 4000:
            msg = msg[:4000]
        await self._execute(
            """
            INSERT INTO insights_usage_events (
                event_type, user_id, source_key, query_id, session_id,
                feedback_id, thumb, rating, feedback_type, message, question
            ) VALUES ('feedback', $1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT DO NOTHING
            """,
            str(user_id),
            (str(source_key) or None) if source_key else None,
            qid,
            _as_uuid(session_id),
            fid,
            thumb,
            _as_int(rating),
            (str(feedback_type)[:20] if feedback_type else None),
            msg,
            _excerpt(question),
        )

    async def record_analysis(
        self,
        *,
        user_id: str,
        source_key: Optional[str],
        query_id: Any,
        skill: Optional[str],
        outcome: str,
        execution_time_ms: Any = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """One row per ML run. ``outcome`` accepts the runner's
        ``ok | guard_failed | error`` and maps them onto the ledger's values."""
        qid = _as_uuid(query_id)
        if not user_id or qid is None:
            return
        mapped = {"ok": "success", "success": "success", "guard_failed": "refused",
                  "refused": "refused"}.get(str(outcome or ""), "error")
        await self._execute(
            """
            INSERT INTO insights_usage_events (
                event_type, user_id, source_key, query_id, skill, outcome,
                execution_time_ms, detail
            ) VALUES ('analysis', $1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT DO NOTHING
            """,
            str(user_id),
            (str(source_key) or None) if source_key else None,
            qid,
            (str(skill)[:64] if skill else None),
            mapped,
            _as_int(execution_time_ms),
            json.dumps(filter_detail(detail)),
        )

    async def prune(self, retention_days: int) -> int:
        """Delete rows older than *retention_days* in batches, under an
        advisory lock so replicas never prune concurrently. Returns rows deleted."""
        days = int(retention_days or 0)
        if not self.active or days <= 0:
            return 0
        deleted_total = 0
        try:
            async with self.pool.acquire() as conn:
                locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", PRUNE_LOCK_KEY)
                if not locked:
                    return 0
                try:
                    while True:
                        status = await conn.execute(
                            """
                            DELETE FROM insights_usage_events
                            WHERE id IN (
                                SELECT id FROM insights_usage_events
                                WHERE occurred_at < NOW() - make_interval(days => $1)
                                ORDER BY id
                                LIMIT $2
                            )
                            """,
                            days, PRUNE_BATCH,
                        )
                        deleted = _rows_from_status(status)
                        deleted_total += deleted
                        if deleted < PRUNE_BATCH:
                            break
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", PRUNE_LOCK_KEY)
        except Exception:  # noqa: BLE001
            logger.warning("usage ledger prune failed", exc_info=True)
        if deleted_total:
            logger.info("usage ledger: pruned %d event(s) older than %d days", deleted_total, days)
        return deleted_total


def _rows_from_status(status: Any) -> int:
    """asyncpg returns e.g. ``'DELETE 42'``."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


# ── Read side ───────────────────────────────────────────────────────────────

def _window(days: int) -> tuple[datetime, datetime, datetime]:
    """(prev_start, start, now) for a *days*-long window ending now (UTC)."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    return start - timedelta(days=days), start, now


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(num: Any, den: Any) -> Optional[float]:
    n, d = int(num or 0), int(den or 0)
    return (n / d) if d else None


class UsageAnalyticsRepository:
    """Read side. Every report is one short transaction with a local timeout."""

    STATEMENT_TIMEOUT = "10s"
    MAX_GROUPS = 50

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    # asyncpg needs an actual array parameter for = ANY(...); the placeholder
    # index differs per statement, so each query interpolates the right $n.
    @staticmethod
    def _q(sql: str, routes_param: str) -> str:
        return sql.replace("__IS_QUESTION__", _QUESTION_SQL.replace("$__ROUTES__", routes_param))

    async def _run(self, fn):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL statement_timeout = '{self.STATEMENT_TIMEOUT}'")
                return await fn(conn)

    # ── overview ────────────────────────────────────────────────────────────

    async def overview(self, days: int) -> Dict[str, Any]:
        prev_start, start, now = _window(days)
        routes = list(QUESTION_ROUTES)

        period_sql = self._q(
            """
            SELECT
              COUNT(*) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__)                         AS questions,
              COUNT(*) FILTER (WHERE event_type = 'query' AND NOT __IS_QUESTION__)                     AS text_only_turns,
              COUNT(DISTINCT user_id) FILTER (WHERE event_type IN ('query', 'analysis'))              AS active_users,
              COUNT(DISTINCT source_key) FILTER (WHERE event_type = 'query')                          AS active_connections,
              COUNT(*) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__ AND outcome = 'success') AS successes,
              COUNT(*) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__ AND outcome = 'error')   AS errors,
              COUNT(*) FILTER (WHERE event_type = 'query' AND outcome = 'refused')                     AS refused,
              AVG(graph_time_ms) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__)               AS avg_graph_time_ms,
              COALESCE(SUM(total_tokens) FILTER (WHERE event_type = 'query'), 0)                       AS total_tokens,
              COUNT(*) FILTER (WHERE event_type = 'login')                                             AS logins,
              COUNT(*) FILTER (WHERE event_type = 'feedback' AND message IS NOT NULL AND message <> '') AS comments,
              COUNT(*) FILTER (WHERE event_type = 'feedback')                                          AS feedback_events,
              AVG(rating) FILTER (WHERE event_type = 'feedback')                                       AS avg_rating,
              COUNT(*) FILTER (WHERE event_type = 'feedback' AND thumb = 'thumbs_up')                  AS thumbs_up_events,
              COUNT(*) FILTER (WHERE event_type = 'feedback' AND thumb = 'thumbs_down')                AS thumbs_down_events
            FROM insights_usage_events
            WHERE occurred_at >= $1 AND occurred_at < $2
            """,
            "$3::text[]",
        )
        thumbs_sql = """
            SELECT
              COUNT(*) FILTER (WHERE thumb = 'thumbs_up')   AS thumbs_up,
              COUNT(*) FILTER (WHERE thumb = 'thumbs_down') AS thumbs_down
            FROM (
              SELECT DISTINCT ON (query_id) query_id, thumb, occurred_at
              FROM insights_usage_events
              WHERE event_type = 'feedback' AND thumb IS NOT NULL AND query_id IS NOT NULL
              ORDER BY query_id, id DESC
            ) t
            WHERE t.occurred_at >= $1 AND t.occurred_at < $2
        """
        activity_sql = """
            SELECT
              COUNT(DISTINCT user_id) FILTER (WHERE occurred_at >= $1) AS dau,
              COUNT(DISTINCT user_id) FILTER (WHERE occurred_at >= $2) AS wau,
              COUNT(DISTINCT user_id)                                  AS mau
            FROM insights_usage_events
            WHERE event_type IN ('query', 'analysis') AND occurred_at >= $3
        """
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        async def _go(conn):
            cur = await conn.fetchrow(period_sql, start, now, routes)
            prev = await conn.fetchrow(period_sql, prev_start, start, routes)
            cur_thumbs = await conn.fetchrow(thumbs_sql, start, now)
            prev_thumbs = await conn.fetchrow(thumbs_sql, prev_start, start)
            activity = await conn.fetchrow(
                activity_sql, today_start, now - timedelta(days=7), now - timedelta(days=30)
            )
            return cur, prev, cur_thumbs, prev_thumbs, activity

        cur, prev, cur_thumbs, prev_thumbs, activity = await self._run(_go)

        def _shape(row, thumbs) -> Dict[str, Any]:
            row = dict(row or {})
            thumbs = dict(thumbs or {})
            return {
                "questions": int(row.get("questions") or 0),
                "text_only_turns": int(row.get("text_only_turns") or 0),
                "active_users": int(row.get("active_users") or 0),
                "active_connections": int(row.get("active_connections") or 0),
                "successes": int(row.get("successes") or 0),
                "errors": int(row.get("errors") or 0),
                "refused": int(row.get("refused") or 0),
                "success_rate": _rate(row.get("successes"), int(row.get("successes") or 0) + int(row.get("errors") or 0)),
                "avg_graph_time_ms": _f(row.get("avg_graph_time_ms")),
                "total_tokens": int(row.get("total_tokens") or 0),
                "logins": int(row.get("logins") or 0),
                "comments": int(row.get("comments") or 0),
                "feedback_events": int(row.get("feedback_events") or 0),
                "avg_rating": _f(row.get("avg_rating")),
                "thumbs_up": int(thumbs.get("thumbs_up") or 0),
                "thumbs_down": int(thumbs.get("thumbs_down") or 0),
                "thumbs_up_events": int(row.get("thumbs_up_events") or 0),
                "thumbs_down_events": int(row.get("thumbs_down_events") or 0),
            }

        activity = dict(activity or {})
        return {
            "days": days,
            "start": start.isoformat(),
            "end": now.isoformat(),
            "dau": int(activity.get("dau") or 0),
            "wau": int(activity.get("wau") or 0),
            "mau": int(activity.get("mau") or 0),
            "current": _shape(cur, cur_thumbs),
            "previous": _shape(prev, prev_thumbs),
        }

    # ── time series ─────────────────────────────────────────────────────────

    async def timeseries(self, days: int) -> List[Dict[str, Any]]:
        _, start, now = _window(days)
        sql = self._q(
            """
            WITH e AS (
              SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, event_type, user_id,
                     outcome, thumb, route
              FROM insights_usage_events
              WHERE occurred_at >= $1
            ),
            days AS (
              SELECT generate_series($2::date, $3::date, INTERVAL '1 day')::date AS day
            )
            SELECT d.day,
              COUNT(DISTINCT e.user_id) FILTER (WHERE e.event_type IN ('query', 'analysis')) AS active_users,
              COUNT(e.day) FILTER (WHERE e.event_type = 'query' AND __IS_QUESTION__)        AS questions,
              COUNT(e.day) FILTER (WHERE e.event_type = 'query' AND e.outcome = 'error')     AS errors,
              COUNT(e.day) FILTER (WHERE e.event_type = 'feedback' AND e.thumb = 'thumbs_up')   AS thumbs_up,
              COUNT(e.day) FILTER (WHERE e.event_type = 'feedback' AND e.thumb = 'thumbs_down') AS thumbs_down,
              COUNT(e.day) FILTER (WHERE e.event_type = 'login')                             AS logins
            FROM days d
            LEFT JOIN e ON e.day = d.day
            GROUP BY d.day
            ORDER BY d.day
            """,
            "$4::text[]",
        )
        rows = await self._run(lambda conn: conn.fetch(sql, start, start.date(), now.date(), list(QUESTION_ROUTES)))
        return [
            {
                "day": r["day"].isoformat(),
                "active_users": int(r["active_users"] or 0),
                "questions": int(r["questions"] or 0),
                "errors": int(r["errors"] or 0),
                "thumbs_up": int(r["thumbs_up"] or 0),
                "thumbs_down": int(r["thumbs_down"] or 0),
                "logins": int(r["logins"] or 0),
            }
            for r in rows
        ]

    # ── top users / connections ─────────────────────────────────────────────

    _CURRENT_THUMBS_CTE = """
        current_thumbs AS (
          SELECT DISTINCT ON (query_id) query_id, user_id, source_key, thumb, occurred_at
          FROM insights_usage_events
          WHERE event_type = 'feedback' AND thumb IS NOT NULL AND query_id IS NOT NULL
          ORDER BY query_id, id DESC
        )
    """

    async def top_users(self, days: int, limit: int = 20) -> List[Dict[str, Any]]:
        _, start, now = _window(days)
        limit = max(1, min(int(limit), 100))
        sql = self._q(
            f"""
            WITH {self._CURRENT_THUMBS_CTE},
            q AS (
              SELECT user_id,
                     COUNT(*) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__)                          AS questions,
                     COUNT(*) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__ AND outcome = 'success')  AS successes,
                     COUNT(*) FILTER (WHERE event_type = 'query' AND __IS_QUESTION__ AND outcome = 'error')    AS errors,
                     COUNT(*) FILTER (WHERE event_type = 'analysis')                                          AS analyses,
                     MAX(occurred_at)                                                                         AS last_active
              FROM insights_usage_events
              WHERE event_type IN ('query', 'analysis') AND occurred_at >= $1
              GROUP BY user_id
            ),
            t AS (
              SELECT user_id,
                     COUNT(*) FILTER (WHERE thumb = 'thumbs_up')   AS thumbs_up,
                     COUNT(*) FILTER (WHERE thumb = 'thumbs_down') AS thumbs_down
              FROM current_thumbs
              WHERE occurred_at >= $1
              GROUP BY user_id
            ),
            r AS (
              SELECT user_id, AVG(rating) AS avg_rating
              FROM insights_usage_events
              WHERE event_type = 'feedback' AND rating IS NOT NULL AND occurred_at >= $1
              GROUP BY user_id
            )
            SELECT q.user_id, au.name, au.email, au.role,
                   q.questions, q.successes, q.errors, q.analyses, q.last_active,
                   COALESCE(t.thumbs_up, 0) AS thumbs_up, COALESCE(t.thumbs_down, 0) AS thumbs_down,
                   r.avg_rating
            FROM q
            LEFT JOIN t ON t.user_id = q.user_id
            LEFT JOIN r ON r.user_id = q.user_id
            LEFT JOIN auth_users au ON au.id::text = q.user_id
            ORDER BY q.questions DESC, q.analyses DESC, q.last_active DESC
            LIMIT $2
            """,
            "$3::text[]",
        )
        rows = await self._run(lambda conn: conn.fetch(sql, start, limit, list(QUESTION_ROUTES)))
        return [
            {
                "user_id": r["user_id"],
                "name": r["name"],
                "email": r["email"],
                "role": r["role"],
                "questions": int(r["questions"] or 0),
                "analyses": int(r["analyses"] or 0),
                "success_rate": _rate(r["successes"], int(r["successes"] or 0) + int(r["errors"] or 0)),
                "last_active": r["last_active"].isoformat() if r["last_active"] else None,
                "thumbs_up": int(r["thumbs_up"] or 0),
                "thumbs_down": int(r["thumbs_down"] or 0),
                "avg_rating": _f(r["avg_rating"]),
            }
            for r in rows
        ]

    async def top_connections(self, days: int, limit: int = 20) -> List[Dict[str, Any]]:
        _, start, now = _window(days)
        limit = max(1, min(int(limit), 100))
        sql = self._q(
            f"""
            WITH {self._CURRENT_THUMBS_CTE},
            q AS (
              SELECT source_key,
                     COUNT(*) FILTER (WHERE __IS_QUESTION__)                          AS questions,
                     COUNT(DISTINCT user_id)                                          AS distinct_users,
                     COUNT(*) FILTER (WHERE __IS_QUESTION__ AND outcome = 'success')  AS successes,
                     COUNT(*) FILTER (WHERE __IS_QUESTION__ AND outcome = 'error')    AS errors,
                     AVG(graph_time_ms) FILTER (WHERE __IS_QUESTION__)                AS avg_graph_time_ms,
                     MAX(occurred_at)                                                 AS last_used
              FROM insights_usage_events
              WHERE event_type = 'query' AND source_key IS NOT NULL AND occurred_at >= $1
              GROUP BY source_key
            ),
            t AS (
              SELECT source_key,
                     COUNT(*) FILTER (WHERE thumb = 'thumbs_up')   AS thumbs_up,
                     COUNT(*) FILTER (WHERE thumb = 'thumbs_down') AS thumbs_down
              FROM current_thumbs
              WHERE occurred_at >= $1 AND source_key IS NOT NULL
              GROUP BY source_key
            )
            SELECT q.source_key, q.questions, q.distinct_users, q.successes, q.errors,
                   q.avg_graph_time_ms, q.last_used,
                   COALESCE(t.thumbs_up, 0) AS thumbs_up, COALESCE(t.thumbs_down, 0) AS thumbs_down
            FROM q
            LEFT JOIN t ON t.source_key = q.source_key
            ORDER BY q.questions DESC, q.distinct_users DESC
            LIMIT $2
            """,
            "$3::text[]",
        )
        rows = await self._run(lambda conn: conn.fetch(sql, start, limit, list(QUESTION_ROUTES)))
        out = []
        for r in rows:
            up, down = int(r["thumbs_up"] or 0), int(r["thumbs_down"] or 0)
            out.append({
                "source_key": r["source_key"],
                "questions": int(r["questions"] or 0),
                "distinct_users": int(r["distinct_users"] or 0),
                "success_rate": _rate(r["successes"], int(r["successes"] or 0) + int(r["errors"] or 0)),
                "avg_graph_time_ms": _f(r["avg_graph_time_ms"]),
                "last_used": r["last_used"].isoformat() if r["last_used"] else None,
                "thumbs_up": up,
                "thumbs_down": down,
                "thumbs_down_rate": _rate(down, up + down),
            })
        return out

    # ── feedback feed ───────────────────────────────────────────────────────

    async def feedback_feed(
        self,
        days: int,
        *,
        thumb: Optional[str] = None,
        feedback_type: Optional[str] = None,
        connection: Optional[str] = None,
        limit: int = 50,
        before_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _, start, _ = _window(days)
        limit = max(1, min(int(limit), 200))
        sql = """
            SELECT e.id, e.occurred_at, e.user_id, au.name, au.email, e.source_key,
                   e.thumb, e.rating, e.feedback_type, e.message, e.question, e.query_id
            FROM insights_usage_events e
            LEFT JOIN auth_users au ON au.id::text = e.user_id
            WHERE e.event_type = 'feedback'
              AND e.occurred_at >= $1
              AND ($2::text IS NULL OR e.thumb = $2)
              AND ($3::text IS NULL OR e.feedback_type = $3)
              AND ($4::text IS NULL OR e.source_key = $4)
              AND ($5::bigint IS NULL OR e.id < $5)
            ORDER BY e.id DESC
            LIMIT $6
        """
        rows = await self._run(
            lambda conn: conn.fetch(sql, start, thumb or None, feedback_type or None,
                                    connection or None, before_id, limit)
        )
        items = [
            {
                "id": int(r["id"]),
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
                "user_id": r["user_id"],
                "name": r["name"],
                "email": r["email"],
                "source_key": r["source_key"],
                "thumb": r["thumb"],
                "rating": r["rating"],
                "feedback_type": r["feedback_type"],
                "message": r["message"],
                "question": r["question"],
                "query_id": str(r["query_id"]) if r["query_id"] else None,
            }
            for r in rows
        ]
        return {
            "items": items,
            "next_before": items[-1]["id"] if len(items) == limit else None,
        }

    # ── ML skills ───────────────────────────────────────────────────────────

    async def analysis_usage(self, days: int) -> List[Dict[str, Any]]:
        _, start, _ = _window(days)
        runs_sql = f"""
            SELECT COALESCE(skill, 'unknown') AS skill,
                   COUNT(*)                                        AS runs,
                   COUNT(*) FILTER (WHERE outcome = 'success')     AS ok,
                   COUNT(*) FILTER (WHERE outcome = 'refused')     AS guard_failed,
                   COUNT(*) FILTER (WHERE outcome = 'error')       AS errors,
                   AVG(execution_time_ms)                          AS avg_execution_ms,
                   COUNT(DISTINCT user_id)                         AS distinct_users
            FROM insights_usage_events
            WHERE event_type = 'analysis' AND occurred_at >= $1
            GROUP BY 1
            ORDER BY runs DESC
            LIMIT {self.MAX_GROUPS}
        """
        thumbs_sql = f"""
            WITH {self._CURRENT_THUMBS_CTE}
            SELECT q.skill,
                   COUNT(*) FILTER (WHERE t.thumb = 'thumbs_up')   AS thumbs_up,
                   COUNT(*) FILTER (WHERE t.thumb = 'thumbs_down') AS thumbs_down
            FROM current_thumbs t
            JOIN insights_usage_events q
              ON q.event_type = 'query' AND q.query_id = t.query_id AND q.skill IS NOT NULL
            WHERE t.occurred_at >= $1
            GROUP BY q.skill
            LIMIT {self.MAX_GROUPS}
        """

        async def _go(conn):
            return await conn.fetch(runs_sql, start), await conn.fetch(thumbs_sql, start)

        runs, thumbs = await self._run(_go)
        by_skill = {r["skill"]: (int(r["thumbs_up"] or 0), int(r["thumbs_down"] or 0)) for r in thumbs}
        out = []
        for r in runs:
            up, down = by_skill.get(r["skill"], (0, 0))
            out.append({
                "skill": r["skill"],
                "runs": int(r["runs"] or 0),
                "ok": int(r["ok"] or 0),
                "guard_failed": int(r["guard_failed"] or 0),
                "errors": int(r["errors"] or 0),
                "avg_execution_ms": _f(r["avg_execution_ms"]),
                "distinct_users": int(r["distinct_users"] or 0),
                "thumbs_up": up,
                "thumbs_down": down,
            })
        return out

    # ── errors ──────────────────────────────────────────────────────────────

    async def error_breakdown(self, days: int, limit: int = 20) -> Dict[str, Any]:
        _, start, _ = _window(days)
        limit = max(1, min(int(limit), self.MAX_GROUPS))
        by_type_sql = f"""
            SELECT COALESCE(error_type, 'unknown') AS error_type, source_key, COUNT(*) AS failures
            FROM insights_usage_events
            WHERE event_type = 'query' AND outcome = 'error' AND occurred_at >= $1
            GROUP BY 1, 2
            ORDER BY failures DESC
            LIMIT {self.MAX_GROUPS}
        """
        # Only questions that failed more than once: a one-off failure would
        # surface a single user's exact wording for no analytical gain.
        questions_sql = """
            SELECT question, COUNT(*) AS failures, COUNT(DISTINCT user_id) AS distinct_users,
                   COUNT(DISTINCT source_key) AS distinct_connections, MAX(occurred_at) AS last_seen
            FROM insights_usage_events
            WHERE event_type = 'query' AND outcome = 'error' AND occurred_at >= $1
              AND question IS NOT NULL AND question <> ''
            GROUP BY question
            HAVING COUNT(*) >= 2
            ORDER BY failures DESC, last_seen DESC
            LIMIT $2
        """

        async def _go(conn):
            return await conn.fetch(by_type_sql, start), await conn.fetch(questions_sql, start, limit)

        by_type, questions = await self._run(_go)
        return {
            "by_type": [
                {"error_type": r["error_type"], "source_key": r["source_key"], "failures": int(r["failures"] or 0)}
                for r in by_type
            ],
            "top_failing_questions": [
                {
                    "question": r["question"],
                    "failures": int(r["failures"] or 0),
                    "distinct_users": int(r["distinct_users"] or 0),
                    "distinct_connections": int(r["distinct_connections"] or 0),
                    "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                }
                for r in questions
            ],
        }
