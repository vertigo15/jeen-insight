"""On-open retention trigger for conversation artifacts.

``GET /api/conversations/last`` is the "user opened the app" signal. After it
has resolved the conversation being restored, it calls ``maybe_schedule_prune``
which, without ever delaying the response:

1. skips when the feature or the on-open prune is disabled;
2. skips when this process pruned the same (user, connection) within the
   configured interval (free first-level debounce; the DB claim row inside
   ``prune_user_conversations`` is the guard that holds across replicas);
3. skips, rather than queues, when both prune slots are busy so a burst of
   logins cannot starve request handlers of pool connections;
4. otherwise spawns the prune as a tracked background task.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple
from uuid import UUID

from src.agent.conversation_history import ConversationHistoryService
from src.api import background
from src.config import settings

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_PRUNES = 2

_last_run: Dict[Tuple[str, str], float] = {}
# Slots are reserved synchronously (no await between the check and the
# reservation), so a burst of logins in one event-loop turn can never queue a
# third prune behind the two running ones.
_in_flight = 0


def in_flight() -> int:
    return _in_flight


def maybe_schedule_prune(
    history: ConversationHistoryService,
    *,
    user_id: str,
    source_key: str,
    protect_conversation_id: Optional[UUID],
) -> Optional[str]:
    """Schedule the per-user prune. Returns the skip reason, or None if spawned."""
    global _in_flight
    if not getattr(history, "persistence_enabled", False):
        return "persistence_disabled"
    if not settings.CONVERSATION_RETENTION_ON_OPEN:
        return "retention_disabled"

    key = (user_id, source_key)
    now = time.monotonic()
    interval = max(0, int(settings.CONVERSATION_RETENTION_MIN_INTERVAL_SECONDS))
    last = _last_run.get(key)
    if last is not None and now - last < interval:
        return "debounced"

    if _in_flight >= _MAX_CONCURRENT_PRUNES:
        logger.info(
            "conversation_prune skipped user=%s source=%s reason=saturated",
            user_id, source_key,
            extra={"event": "conversation_prune", "skipped": "saturated"},
        )
        return "saturated"

    _last_run[key] = now
    _in_flight += 1

    async def _run() -> None:
        global _in_flight
        try:
            await history.prune_user_conversations(
                user_id=user_id,
                source_key=source_key,
                keep_last=settings.CONVERSATION_KEEP_LAST,
                keep_last_turns=settings.CONVERSATION_SNAPSHOT_KEEP_LAST_TURNS,
                min_interval_seconds=interval,
                protect_conversation_id=protect_conversation_id,
            )
        finally:
            _in_flight = max(0, _in_flight - 1)

    task = background.spawn(_run(), name=f"conversation_prune:{user_id}:{source_key}")
    if task is None:
        _in_flight = max(0, _in_flight - 1)
        return "shutting_down"
    return None


def reset_for_tests() -> None:
    global _in_flight
    _last_run.clear()
    _in_flight = 0
