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
        self._load_all()

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
        template = self.get(name)
        try:
            return template.format(**kwargs)
        except KeyError as exc:
            raise KeyError(
                f"Prompt '{name}' is missing placeholder {exc}. "
                f"Provided keys: {sorted(kwargs)}"
            ) from exc

    # ── Internals ─────────────────────────────────────────────────────────

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
