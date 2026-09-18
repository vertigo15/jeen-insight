"""Prompt loader for the text-to-DAX agent.

``DaxPromptLoader`` loads templates from BOTH the shared SQL prompts directory
(``src/agent/prompts/``) and the DAX-specific directory
(``src/agent/prompts_dax/``). DAX templates take precedence on name collisions.

This is required because the DAX graph reuses the shared, engine-agnostic nodes
(``fused_router``, ``memory_answer``, ``fused_eval_analytics``)
whose prompts live in ``prompts/``, while the DAX query-core nodes need their own
templates from ``prompts_dax/``. Loading both into one loader lets a single
``PromptLoader`` instance serve every node — and it still supports the DB-backed
``PromptCache`` (Settings-UI edits / per-prompt model overrides) for the shared
names. DAX-owned names are always served from disk (see ``_aget``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from src.agent.langgraph_agent.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

_DAX_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts_dax"
_BASE_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class DaxPromptLoader(PromptLoader):
    """PromptLoader that merges the base and DAX prompt directories."""

    def __init__(
        self,
        prompts_dir: Optional[Path] = None,
        base_dir: Optional[Path] = None,
    ) -> None:
        self._dax_dir = prompts_dir or _DAX_PROMPTS_DIR
        self._base_dir = base_dir or _BASE_PROMPTS_DIR
        # PromptLoader.__init__ calls _load_all(); point ._dir at the DAX dir for
        # any error messages, but _load_all (overridden) merges both dirs.
        super().__init__(prompts_dir=self._dax_dir)

    def _load_all(self) -> None:  # type: ignore[override]
        new_cache: Dict[str, str] = {}
        dax_names: set[str] = set()
        # Base first so DAX templates override on name collision.
        for directory in (self._base_dir, self._dax_dir):
            if not directory or not directory.exists():
                if directory:
                    logger.warning("DaxPromptLoader: prompts dir %s not found", directory)
                continue
            for path in sorted(directory.glob("*.md")):
                new_cache[path.stem] = path.read_text(encoding="utf-8")
                if directory == self._dax_dir:
                    dax_names.add(path.stem)
        self._cache = new_cache
        self._dax_names = dax_names
        logger.info(
            "DaxPromptLoader: %d template(s) from %s + %s",
            len(new_cache), self._base_dir, self._dax_dir,
        )

    # DAX-owned templates are served from disk, never from ``insights_prompts``:
    # they are not in the Settings registry, and when a DAX file shadows a shared
    # name (``capability_answer``) the DB row of that name is the SQL graph's
    # text, which must not leak into the Power BI flow. Shared names still go
    # through the DB so Settings-UI edits and model overrides apply to both graphs.

    async def _aget(self, name: str) -> str:  # type: ignore[override]
        if name in self._dax_names:
            return self.get(name)
        return await super()._aget(name)

    async def model_override_for(self, name: str):  # type: ignore[override]
        if name in self._dax_names:
            return None
        return await super().model_override_for(name)


__all__ = ["DaxPromptLoader"]
