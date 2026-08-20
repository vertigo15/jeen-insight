"""Safe, request-scoped progress events for LangGraph query execution."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Dict[str, Any]], None]


def emit_progress(
    state: Dict[str, Any],
    *,
    node: str,
    status: str,
    icon: str,
    node_type: str,
    elapsed_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Emit a minimal node event without affecting graph execution.

    The callback is deliberately synchronous (the SSE route uses
    ``asyncio.Queue.put_nowait``), allowing both sync and async graph nodes to
    report progress through the same wrapper. Callback failures are telemetry
    failures and must never fail a query.
    """

    callback = state.get("progress_callback")
    if not callable(callback):
        return

    event: Dict[str, Any] = {
        "node": node,
        "status": status,
        "icon": icon,
        "type": node_type,
    }
    if elapsed_ms is not None:
        event["elapsed_ms"] = max(0, int(elapsed_ms))
    if error:
        event["error"] = str(error)[:500]

    try:
        callback(event)
    except Exception:  # noqa: BLE001
        logger.debug("query progress callback failed", exc_info=True)


__all__ = ["ProgressCallback", "emit_progress"]
