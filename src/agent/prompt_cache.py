"""Per-prompt lazy cache — stores prompt content and optional model override.

On first access each prompt place is fetched from ``insights_prompts`` and
an optional ``ModelOverride`` is built from the assigned ``model_id``.
Subsequent accesses are in-process memory.

Cache is invalidated selectively:
* ``invalidate(place)``  — called when prompt content or model is updated.
* ``clear()``            — called when the global active model changes, so
                          prompts without an explicit override see the new
                          default on their next access.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── DB query ──────────────────────────────────────────────────────────────────

_FETCH_ACTIVE = """
    SELECT
        ip.id,
        ip.prompt_place,
        ip.content,
        ip.version,
        ip.is_custom,
        ip.model_id,
        am.name         AS model_name,
        am.display_name AS model_display_name
    FROM insights_prompts ip
    LEFT JOIN admin_models am ON am.id = ip.model_id
    WHERE ip.prompt_place = $1
      AND ip.is_active    = true
    LIMIT 1
"""


# ── Cache entry ───────────────────────────────────────────────────────────────

@dataclass
class PromptCacheEntry:
    """Resolved prompt ready for use — no further DB I/O needed."""
    prompt_place:    str
    content:         str
    version:         int
    is_custom:       bool
    model_id:        Optional[int]
    model_name:      Optional[str]
    # None  → use the global active model at call time.
    # set   → use this override for every call on this prompt place.
    model_override: Any  # Optional[ModelOverride] – avoid circular import


# ── Cache ─────────────────────────────────────────────────────────────────────

class PromptCache:
    """Lazy in-process cache that maps ``prompt_place`` to a
    :class:`PromptCacheEntry`.

    On first access the active DB row is loaded and — if the row has a
    ``model_id`` — a :class:`~src.agent.llm_service.ModelOverride` is built
    via ``llm_service.build_model_override_for_model_id()``.

    Thread-safe: cache writes are guarded by a single ``asyncio.Lock``.
    """

    def __init__(self, pool: Any, llm_service: Any) -> None:
        self._pool        = pool
        self._llm_service = llm_service
        self._entries: Dict[str, PromptCacheEntry] = {}
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    async def get(self, prompt_place: str) -> PromptCacheEntry:
        """Return a cached entry, loading from DB on first access."""
        if prompt_place in self._entries:
            return self._entries[prompt_place]
        async with self._lock:
            # Re-check after acquiring lock (double-checked locking).
            if prompt_place in self._entries:
                return self._entries[prompt_place]
            entry = await self._load(prompt_place)
            self._entries[prompt_place] = entry
            return entry

    async def get_content(self, prompt_place: str) -> str:
        """Return the raw template string for *prompt_place*."""
        return (await self.get(prompt_place)).content

    async def get_model_override(self, prompt_place: str):
        """Return the :class:`~src.agent.llm_service.ModelOverride` for
        *prompt_place*, or ``None`` when the global active model should be
        used.
        """
        return (await self.get(prompt_place)).model_override

    def invalidate(self, prompt_place: str) -> None:
        """Drop the cached entry for *prompt_place* so the next access
        reloads from DB.
        """
        dropped = self._entries.pop(prompt_place, None)
        if dropped:
            logger.info("prompt_cache: invalidated %r (was v%d)", prompt_place, dropped.version)

    def clear(self) -> None:
        """Drop all cached entries (e.g. after a global model change)."""
        count = len(self._entries)
        self._entries.clear()
        logger.info("prompt_cache: cleared %d entr%s", count, "y" if count == 1 else "ies")

    # ── Internal ──────────────────────────────────────────────────────────

    async def _load(self, prompt_place: str) -> PromptCacheEntry:
        """Fetch one active row from DB and build its entry."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_FETCH_ACTIVE, prompt_place)

        if not row:
            raise KeyError(
                f"No active prompt found for place {prompt_place!r}. "
                "Check that insights_prompts was seeded on startup."
            )

        model_override = None
        if row["model_id"] is not None:
            try:
                model_override = await self._llm_service.build_model_override_for_model_id(
                    row["model_id"]
                )
                logger.info(
                    "prompt_cache: loaded %r v%d with model override=%s",
                    prompt_place, row["version"], row["model_name"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "prompt_cache: cannot build model override for %r "
                    "(model_id=%s, %s) — falling back to global default",
                    prompt_place, row["model_id"], exc,
                )
        else:
            logger.info(
                "prompt_cache: loaded %r v%d (global default model)",
                prompt_place, row["version"],
            )

        return PromptCacheEntry(
            prompt_place=prompt_place,
            content=row["content"],
            version=row["version"],
            is_custom=row["is_custom"],
            model_id=row["model_id"],
            model_name=row["model_name"],
            model_override=model_override,
        )
