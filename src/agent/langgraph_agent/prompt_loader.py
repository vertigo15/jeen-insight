"""PromptLoader — loads and caches prompt templates from markdown files.

Usage:
    loader = PromptLoader()
    text = loader.render("fused_router", question="...", conversation_summary="...", source_description="...")
    loader.reload()   # hot-reload all templates from disk — no container restart needed

All prompt files live in ``src/agent/prompts/`` as ``<name>.md``.
Each file may contain ``{placeholder}`` variables documented in a comment header.
The ``render`` method performs Python ``str.format(**kwargs)`` substitution.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Default prompts directory — same folder that holds jeen_insights_system.md
_DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class PromptLoader:
    """Loads and caches prompt templates from ``.md`` files on disk.

    Parameters
    ----------
    prompts_dir:
        Override the directory to read from (defaults to ``src/agent/prompts/``).
    """

    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        self._dir = prompts_dir or _DEFAULT_PROMPTS_DIR
        self._cache: Dict[str, str] = {}
        # Optional DB-backed prompt store. When attached, ``arender`` and
        # ``model_override_for`` prefer the active DB version (honouring Settings
        # UI edits and per-prompt model assignments) and fall back to disk.
        self._prompt_cache: Any = None
        self._load_all()

    # ── DB backing ────────────────────────────────────────────────────────

    def attach_cache(self, prompt_cache: Any) -> None:
        """Attach a DB-backed ``PromptCache`` so the graph honours DB prompt
        versions and per-prompt model overrides. Disk files remain the fallback.
        """
        self._prompt_cache = prompt_cache

    # ── Public API ────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Hot-reload all templates from disk.

        Call this after editing any ``.md`` file to pick up changes without
        restarting the API container.  Safe to call during a live request
        because the dict replacement is atomic in CPython.
        """
        self._load_all()
        logger.info("PromptLoader: reloaded %d templates from %s", len(self._cache), self._dir)

    def get(self, name: str) -> str:
        """Return the raw (un-rendered) template string for *name*.

        Raises ``KeyError`` with a helpful message if the file doesn't exist.
        """
        if name not in self._cache:
            available = sorted(self._cache)
            raise KeyError(
                f"Prompt '{name}' not found in {self._dir}. "
                f"Available: {available}"
            )
        return self._cache[name]

    def render(self, name: str, **kwargs: object) -> str:
        """Render *name* by substituting ``{placeholder}`` variables.

        Parameters
        ----------
        name:
            Prompt file stem (without ``.md``), e.g. ``"fused_router"``.
        **kwargs:
            Values for each ``{placeholder}`` in the template.

        Raises
        ------
        KeyError
            If the prompt file doesn't exist or a required placeholder is missing.
        """
        return self._format(name, self.get(name), kwargs)

    async def arender(self, name: str, **kwargs: object) -> str:
        """Async render that prefers the DB version when a cache is attached.

        Falls back to the disk template when no cache is attached or the DB
        lookup fails, so the graph never breaks if the DB is unavailable.
        """
        template = await self._aget(name)
        return self._format(name, template, kwargs)

    async def model_override_for(self, name: str):
        """Return the per-prompt ``ModelOverride`` for *name*, or ``None``.

        ``None`` means "use the caller's default model". Requires an attached
        DB cache; without one this always returns ``None``.
        """
        if self._prompt_cache is None:
            return None
        try:
            return await self._prompt_cache.get_model_override(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PromptLoader: no model override for %r (%s)", name, exc)
            return None

    # ── Internals ─────────────────────────────────────────────────────────

    def _format(self, name: str, template: str, kwargs: dict) -> str:
        try:
            return template.format(**kwargs)
        except KeyError as exc:
            raise KeyError(
                f"Prompt '{name}' is missing placeholder {exc}. "
                f"Provided keys: {sorted(kwargs)}"
            ) from exc

    async def _aget(self, name: str) -> str:
        """Return the DB template for *name* when available, else the disk copy."""
        if self._prompt_cache is not None:
            try:
                content = await self._prompt_cache.get_content(name)
                if content:
                    return content
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PromptLoader: DB prompt %r unavailable (%s); using disk copy",
                    name, exc,
                )
        return self.get(name)

    def _load_all(self) -> None:
        new_cache: Dict[str, str] = {}
        if not self._dir.exists():
            logger.warning("PromptLoader: prompts directory %s not found", self._dir)
            self._cache = new_cache
            return
        for path in sorted(self._dir.glob("*.md")):
            new_cache[path.stem] = path.read_text(encoding="utf-8")
            logger.debug("PromptLoader: loaded %s", path.name)
        self._cache = new_cache
        logger.info("PromptLoader: %d template(s) from %s", len(new_cache), self._dir)
