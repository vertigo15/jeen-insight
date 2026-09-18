"""history_search — answer "did I ask about X in the last 4 days?" from the
application history log.

Route ``history_lookup``. The router has already extracted ``history_query``
(keywords + optional ISO ``since``/``until``); this node runs one database
search over the same rows the History log drawer shows (every query of this
user on the current connection, ``ConversationHistoryService.search_turns``)
and writes a deterministic answer — no LLM call. Matches are also returned
structurally (``history_matches``) so the UI can link back to the original
entries.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)

_ANSWER_MAX_CHARS = 120


def _parse_when(value: Any) -> Optional[datetime]:
    """ISO date or datetime → aware UTC datetime; None when unreadable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_window(query: Dict[str, Any], *, default_days: int, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """``[since, until)`` with defaults: ``until`` exclusive of the day after the
    given date, ``since`` = ``default_days`` before now when not given."""
    now = now or datetime.now(timezone.utc)
    until = _parse_when(query.get("until"))
    if until is not None and len(str(query.get("until") or "")) <= 10:
        until = until + timedelta(days=1)  # a bare date means "through that day"
    if until is None or until > now + timedelta(days=1):
        until = now + timedelta(minutes=1)
    since = _parse_when(query.get("since"))
    if since is None or since >= until:
        since = until - timedelta(days=max(1, int(default_days)))
    return since, until


def _fmt_day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y %H:%M")
    return str(value or "")


def render_history_answer(
    matches: List[Dict[str, Any]],
    *,
    keywords: List[str],
    since: datetime,
    until: datetime,
    current_session: Any,
    limit: int,
) -> str:
    topic = " / ".join(f'"{k}"' for k in keywords) if keywords else "anything"
    window = f"between {since.strftime('%d %b %Y')} and {(until - timedelta(seconds=1)).strftime('%d %b %Y')}"
    if not matches:
        return f"I couldn't find a question about {topic} {window}."
    lines = [f"Yes — {len(matches)} question{'s' if len(matches) != 1 else ''} about {topic} {window}"
             f"{' (showing the most recent ' + str(limit) + ')' if len(matches) >= limit else ''}:"]
    for m in matches:
        here = " (this conversation)" if current_session and str(m.get("session_id")) == str(current_session) else ""
        answer = " ".join(str(m.get("answer") or "").split())
        if len(answer) > _ANSWER_MAX_CHARS:
            answer = answer[: _ANSWER_MAX_CHARS - 1] + "…"
        line = f'• {_fmt_day(m.get("created_at"))} — "{m.get("natural_language_query")}"{here}'
        if answer:
            line += f" → {answer}"
        lines.append(line)
    return "\n".join(lines)


def make_history_search(history_service: Any, *, default_days: Optional[int] = None, max_results: Optional[int] = None):
    """Return the async ``history_search`` node."""
    from src.config import settings  # noqa: PLC0415

    days = int(default_days if default_days is not None else settings.HISTORY_LOOKUP_DEFAULT_DAYS)
    limit = int(max_results if max_results is not None else settings.HISTORY_LOOKUP_MAX_RESULTS)

    async def history_search(state: AgentState) -> Dict[str, Any]:
        query = dict(state.get("history_query") or {})
        keywords = [str(k) for k in (query.get("keywords") or []) if str(k).strip()]
        since, until = resolve_window(query, default_days=days)

        matches: List[Dict[str, Any]] = []
        if history_service is not None and hasattr(history_service, "search_turns"):
            matches = await history_service.search_turns(
                user_id=str(state.get("user_id") or ""),
                source_key=state.get("source_key"),
                keywords=keywords,
                since=since,
                until=until,
                limit=limit,
                exclude_query_id=state.get("query_id"),
            )
        answer = render_history_answer(
            matches, keywords=keywords, since=since, until=until,
            current_session=state.get("session_id"), limit=limit,
        )
        logger.info("history_search: %d match(es) for %s in [%s, %s)", len(matches), keywords, since.date(), until.date())
        return {
            "answer": answer,
            # Same shape as /api/user/history-log entries, plus the stored answer.
            "history_matches": [
                {
                    "query_id": str(m.get("id")),
                    "session_id": str(m.get("session_id")) if m.get("session_id") else None,
                    "question": m.get("natural_language_query"),
                    "answer": m.get("answer") or None,
                    "status": m.get("execution_status"),
                    "row_count": m.get("row_count"),
                    "asked_at": m["created_at"].isoformat() if isinstance(m.get("created_at"), datetime) else m.get("created_at"),
                }
                for m in matches
            ],
        }

    return history_search
