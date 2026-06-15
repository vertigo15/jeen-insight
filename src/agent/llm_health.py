"""Live credential/health probing for configured LLM models.

The model catalogue (``admin_models`` + ``admin_models_providers``) only records
*whether a credential row exists* — not whether that credential actually works.
A model can be flagged ``available`` in the UI while every call returns 401.

This module closes that gap: it builds each enabled model and runs a tiny
one-token generation to confirm the provider credentials and endpoint really
work, then caches the verdict so the settings API can surface a truthful
red/green health signal without re-probing on every request.

The same :func:`probe_row` is reused by ``scripts/test_llm_connections.py`` so
the smoke test and the live endpoint can never drift apart.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from src.agent.llm_service import _build_chat_model  # shared model factory

# Status values returned by a probe.
PASS = "pass"   # credentials + endpoint confirmed working
FAIL = "fail"   # build or generation failed (bad key, wrong endpoint, 404, …)
SKIP = "skip"   # not chat-capable, or provider driver not installed — can't be tested here

# admin_models.type values that are NOT chat/completion models. These don't
# answer a chat prompt, so probing them with one would always (mis)report a
# failure — we skip them instead of showing a false red.
_NON_CHAT_TYPES = {
    "embedding", "rerank", "transcription", "tts", "audio",
    "image", "moderation", "vision",
}

# Models enabled in the catalogue, with everything _build_chat_model needs.
_LIST_ENABLED_MODELS = """
    SELECT
        am.name                  AS model_name,
        am.display_name          AS model_display_name,
        am.type                  AS model_type,
        p.name                   AS provider_name,
        amp.provider_model_identifier,
        amp.api_key,
        amp.config,
        amp.is_default
    FROM admin_models am
    JOIN admin_models_providers amp ON amp.model_id = am.id
    JOIN admin_providers p          ON p.id = amp.provider_id
    WHERE am.is_enabled  = true
      AND amp.is_enabled = true
    ORDER BY amp.is_default DESC, am.sort_order, am.id
"""


def _is_chat_model(model_type: Any) -> bool:
    """True when a model takes a chat prompt (so a chat probe is meaningful).

    Unknown/NULL types are treated as chat so a real chat model is never hidden;
    only the explicitly non-chat kinds are skipped.
    """
    return str(model_type or "").strip().lower() not in _NON_CHAT_TYPES

_PROBE_PROMPT = "Reply with the single word: OK"

# How long a single probe may run before it's treated as a failure.
_PROBE_TIMEOUT_S = 20.0
# Max probes in flight at once — bounds load on providers and the event loop.
_MAX_CONCURRENCY = 8
# How long a cached health snapshot is considered fresh.
_CACHE_TTL_S = 300.0


def _token_kwargs(provider: str) -> Dict[str, Any]:
    """Return the token-limit kwarg each provider's LangChain class accepts."""
    if provider == "azure_openai":
        return {"max_completion_tokens": 16}
    if provider == "google":
        return {"max_output_tokens": 16}
    return {"max_tokens": 16}


@dataclass
class ModelHealth:
    """Result of probing a single model's credentials."""

    name: str
    provider: str
    identifier: str
    status: str            # PASS / FAIL / SKIP
    detail: str            # short human-readable reason / sample reply
    elapsed_s: float
    is_default: bool = False
    model_type: str = ""

    @property
    def healthy(self) -> Optional[bool]:
        """True when the probe passed, False when it failed, None when skipped.

        Tri-state so a skipped (non-chat) model renders neutrally in the UI
        instead of as a false red, and is never picked as a fallback target.
        """
        if self.status == PASS:
            return True
        if self.status == SKIP:
            return None
        return False

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["healthy"] = self.healthy
        return d


async def probe_row(row: Dict[str, Any]) -> ModelHealth:
    """Build the chat model for *row* and run a tiny generation against it.

    Never raises — every failure mode is captured as a ``FAIL``/``SKIP``
    verdict so callers can probe many models without defensive wrapping.
    """
    name = row.get("model_name") or row.get("name") or "?"
    provider = row.get("provider_name") or "?"
    identifier = row.get("provider_model_identifier") or ""
    is_default = bool(row.get("is_default"))
    model_type = str(row.get("model_type") or "")
    start = time.perf_counter()

    # Non-chat models (embeddings, rerank, transcription, …) can't answer a chat
    # prompt — skip them so they don't show up as false failures.
    if not _is_chat_model(model_type):
        return ModelHealth(name, provider, identifier, SKIP,
                           f"{model_type or 'non-chat'} model — not health-checked",
                           0.0, is_default, model_type)

    try:
        chat_model = _build_chat_model(row)
    except ModuleNotFoundError as exc:
        return ModelHealth(name, provider, identifier, SKIP,
                           f"driver not installed: {exc}", 0.0, is_default, model_type)
    except Exception as exc:  # noqa: BLE001
        return ModelHealth(name, provider, identifier, FAIL,
                           f"build failed: {exc}", 0.0, is_default, model_type)

    try:
        from langchain_core.messages import HumanMessage

        model = chat_model.bind(**_token_kwargs(provider))
        resp = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=_PROBE_PROMPT)]),
            timeout=_PROBE_TIMEOUT_S,
        )
        elapsed = time.perf_counter() - start
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        detail = text.strip().replace("\n", " ")[:80] or "(empty reply)"
        return ModelHealth(name, provider, identifier, PASS, detail, elapsed, is_default, model_type)
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start
        return ModelHealth(name, provider, identifier, FAIL,
                           f"timed out after {_PROBE_TIMEOUT_S:.0f}s", elapsed, is_default, model_type)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        return ModelHealth(name, provider, identifier, FAIL,
                           f"{type(exc).__name__}: {exc}", elapsed, is_default, model_type)


async def probe_all(pool: Any) -> List[ModelHealth]:
    """Probe every enabled model concurrently and return their health."""
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_LIST_ENABLED_MODELS)]

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _guarded(r: Dict[str, Any]) -> ModelHealth:
        async with sem:
            return await probe_row(r)

    return await asyncio.gather(*(_guarded(r) for r in rows))


# ── TTL cache ──────────────────────────────────────────────────────────────────

_cache: Dict[str, ModelHealth] = {}
_cache_ts: float = 0.0
_lock = asyncio.Lock()


def cache_age_seconds() -> Optional[float]:
    """Seconds since the last successful probe, or None if never probed."""
    if not _cache_ts:
        return None
    return time.monotonic() - _cache_ts


def cached_health() -> Dict[str, ModelHealth]:
    """Return the last probed health snapshot keyed by model name (may be empty)."""
    return dict(_cache)


async def get_health(pool: Any, *, refresh: bool = False) -> Dict[str, ModelHealth]:
    """Return per-model health, re-probing when stale or when ``refresh`` is set.

    Concurrent callers share a single in-flight probe via an ``asyncio.Lock``.
    """
    global _cache, _cache_ts

    age = cache_age_seconds()
    fresh = age is not None and age < _CACHE_TTL_S
    if _cache and fresh and not refresh:
        return dict(_cache)

    async with _lock:
        # Re-check after acquiring the lock — another caller may have refreshed.
        age = cache_age_seconds()
        fresh = age is not None and age < _CACHE_TTL_S
        if _cache and fresh and not refresh:
            return dict(_cache)

        results = await probe_all(pool)
        _cache = {h.name: h for h in results}
        _cache_ts = time.monotonic()
        return dict(_cache)
