"""Tracked fire-and-forget tasks for the API process.

``asyncio.create_task`` alone loses the task reference (it can be garbage
collected mid-flight) and gives the lifespan no way to drain work before the
DB pool closes. Everything detached from a request should go through
``spawn`` so ``shutdown`` can cancel and await it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Optional, Set

logger = logging.getLogger(__name__)

_tasks: Set[asyncio.Task] = set()
_accepting = True


def spawn(coro: Awaitable, *, name: Optional[str] = None) -> Optional[asyncio.Task]:
    """Schedule *coro* as a tracked task. Returns None once shutdown began."""
    if not _accepting:
        close = getattr(coro, "close", None)
        if callable(close):
            close()  # avoid "coroutine was never awaited" warnings
        return None
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("background task %s failed: %s", task.get_name(), exc)


def pending_count() -> int:
    return len(_tasks)


async def shutdown(timeout: float = 5.0) -> None:
    """Stop accepting new tasks, cancel the running ones and await them."""
    global _accepting
    _accepting = False
    if not _tasks:
        return
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning("background tasks did not finish within %.1fs", timeout)
    _tasks.clear()


def reset_for_tests() -> None:
    global _accepting
    _accepting = True
    _tasks.clear()
