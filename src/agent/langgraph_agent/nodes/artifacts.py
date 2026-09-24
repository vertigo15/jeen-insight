"""Helpers for reasoning about prior result artifacts.

A *result artifact* is the compact, durable summary persisted alongside each
executed query (see ``conversation_history.update_execution`` and
``output._build_result_artifact``): columns, column types, row count and
per-column stats. The turn ledger (``nodes/context.py``) reads it to describe
each prior turn's result shape without touching the rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def parse_artifact(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a result-artifact dict from a DB value (dict or JSON string)."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None
