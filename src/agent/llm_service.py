"""Multi-provider LLM service for Jeen Insights.

Credentials are loaded on demand from the metadata DB:

  admin_models              – model catalogue (name, display_name, …)
  admin_models_providers    – per-model credentials (base64 API key, config JSON)
  admin_providers           – provider type (azure_openai, openai, anthropic, …)

Provider routing (provider_name → LangChain class):

  azure_openai  → langchain_openai.AzureChatOpenAI
  openai        → langchain_openai.ChatOpenAI
  vllm          → langchain_openai.ChatOpenAI  (custom base_url)
  remote        → langchain_openai.ChatOpenAI  (custom base_url)
  anthropic     → langchain_anthropic.ChatAnthropic         (lazy import)
  google        → langchain_google_genai.ChatGoogleGenerativeAI  (lazy import)

The service exposes the same ``generate`` / ``generate_stream`` /
``generate_streaming`` interface as the previous ``AzureOpenAILlmService``
so all existing call sites work without change.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)


# ── Error classification ───────────────────────────────────────────────────────

class LLMUnavailableError(RuntimeError):
    """Raised when the active model and all healthy fallbacks fail.

    Carries a human-readable, actionable message (see ``classify_llm_error``)
    so the failure surfaces clearly in the API response instead of a raw
    provider stack trace.
    """


def classify_llm_error(exc: BaseException) -> str:
    """Map a provider exception to a short, actionable message.

    Recognises the common credential/availability failures (auth, missing
    deployment, rate limit, timeout) and falls back to the exception text.
    """
    text = str(exc).lower()
    if any(s in text for s in ("401", "invalid subscription key", "invalid api key",
                               "access denied", "authentication", "unauthorized", "no api key")):
        return ("the model's API key is invalid or expired (authentication error). "
                "Update the key in Settings → AI Models, or pick a model marked healthy.")
    if any(s in text for s in ("404", "not found", "deploymentnotfound", "no longer available")):
        return ("the model deployment was not found (404) — it may be decommissioned "
                "or misconfigured. Pick a model marked healthy in Settings → AI Models.")
    if any(s in text for s in ("429", "rate limit", "too many requests", "quota")):
        return "the provider is rate-limiting requests (429). Try again shortly."
    if "timeout" in text or "timed out" in text:
        return "the model request timed out. The provider may be slow or unreachable."
    return str(exc)


# ── Reasoning-model temperature handling ──────────────────────────────────────
# GPT-5.x and the o-series (o1/o3/o4) reasoning models reject any temperature
# other than the default (1); Azure/OpenAI return a 400 "unsupported_value".
# We detect them by deployment identifier or model name and simply omit the
# temperature kwarg so the provider applies its default.
_NO_CUSTOM_TEMPERATURE_RE = re.compile(
    r"(?:^|[-_/])(?:gpt-5|o[134])(?:[-._/]|$)",
    re.IGNORECASE,
)


def _model_identifier(chat_model: Any) -> str:
    """Best-effort extraction of a model/deployment identifier from a chat model."""
    for attr in ("deployment_name", "azure_deployment", "model_name", "model"):
        val = getattr(chat_model, attr, None)
        if val:
            return str(val)
    return ""


def _supports_custom_temperature(chat_model: Any, model_name: str = "") -> bool:
    """Return False for reasoning models that only allow the default temperature."""
    haystack = f"{_model_identifier(chat_model)} {model_name or ''}"
    return _NO_CUSTOM_TEMPERATURE_RE.search(haystack) is None


# ── Per-prompt model override ─────────────────────────────────────────────────

class ModelOverride(NamedTuple):
    """Carries a resolved LangChain chat model and its provider name for
    per-prompt model overrides.  Pass to ``generate*`` methods to bypass
    the global active model for a single call.
    """
    chat_model: Any
    provider_name: str


# ── DB credential loading ──────────────────────────────────────────────────────────────────

_CREDENTIAL_QUERY = """
    SELECT
        am.name                         AS model_name,
        am.display_name                 AS model_display_name,
        p.name                          AS provider_name,
        amp.provider_model_identifier,
        amp.api_key,
        amp.config,
        amp.max_output_tokens,
        amp.timeout_seconds,
        amp.max_retries
    FROM admin_models am
    JOIN admin_models_providers amp ON amp.model_id = am.id
    JOIN admin_providers p          ON p.id = amp.provider_id
    WHERE am.name      = $1
      AND amp.is_enabled = true
      AND am.is_enabled  = true
    ORDER BY amp.is_default DESC, amp.sort_order
    LIMIT 1
"""

_DEFAULT_CREDENTIAL_QUERY = """
    SELECT
        am.name                         AS model_name,
        am.display_name                 AS model_display_name,
        p.name                          AS provider_name,
        amp.provider_model_identifier,
        amp.api_key,
        amp.config,
        amp.max_output_tokens,
        amp.timeout_seconds,
        amp.max_retries
    FROM admin_models_providers amp
    JOIN admin_models am ON am.id = amp.model_id
    JOIN admin_providers p  ON p.id = amp.provider_id
    WHERE amp.is_default = true
      AND amp.is_enabled = true
      AND am.is_enabled  = true
    ORDER BY amp.sort_order
    LIMIT 1
"""


_FETCH_BY_MODEL_ID = """
    SELECT
        am.name                         AS model_name,
        am.display_name                 AS model_display_name,
        p.name                          AS provider_name,
        amp.provider_model_identifier,
        amp.api_key,
        amp.config,
        amp.max_output_tokens,
        amp.timeout_seconds,
        amp.max_retries
    FROM admin_models_providers amp
    JOIN admin_models am ON am.id = amp.model_id
    JOIN admin_providers p  ON p.id = amp.provider_id
    WHERE amp.model_id  = $1
      AND amp.is_enabled = true
      AND am.is_enabled  = true
    ORDER BY amp.is_default DESC, amp.sort_order
    LIMIT 1
"""


async def _fetch_model_row(
    pool: Any,
    model_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Return a merged credential row, or None when no matching row exists."""
    async with pool.acquire() as conn:
        if model_name:
            row = await conn.fetchrow(_CREDENTIAL_QUERY, model_name)
        else:
            row = await conn.fetchrow(_DEFAULT_CREDENTIAL_QUERY)
    return dict(row) if row else None


# ── Credential helpers ───────────────────────────────────────────────────────────────────

def _decode_key(raw: Optional[str]) -> Optional[str]:
    """Base64-decode an API key stored in the DB; returns None when raw is falsy."""
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw  # already plain-text (shouldn't happen, but safe)


def _parse_config(cfg: Any) -> Dict[str, Any]:
    if not cfg:
        return {}
    if isinstance(cfg, str):
        try:
            return json.loads(cfg)
        except Exception:
            return {}
    return dict(cfg)


# ── Chat model factory ─────────────────────────────────────────────────────────────────

def _build_chat_model(row: Dict[str, Any]) -> Any:
    """Instantiate the correct LangChain chat model for a credential row.

    Provider packages are imported lazily so that missing optional dependencies
    (e.g. ``langchain-anthropic``) only raise an error when that provider is
    actually selected.
    """
    provider   = row["provider_name"]               # e.g. "azure_openai"
    identifier = row["provider_model_identifier"]   # deployment or model ID
    api_key    = _decode_key(row.get("api_key"))
    config     = _parse_config(row.get("config"))

    if provider == "azure_openai":
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_deployment=identifier,
            azure_endpoint=config["endpoint"],
            api_version=config.get("apiVersion", "2025-01-01-preview"),
            api_key=api_key,
        )

    if provider in ("openai", "vllm", "remote"):
        from langchain_openai import ChatOpenAI

        kwargs: Dict[str, Any] = {
            "model":   identifier,
            "api_key": api_key or "not-needed",
        }
        base_url = config.get("baseURL") or config.get("base_url")
        if base_url:
            kwargs["base_url"] = base_url
        org = config.get("organizationId") or config.get("organization")
        if org:
            kwargs["organization"] = org
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # optional dep

        return ChatAnthropic(model=identifier, api_key=api_key)

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI  # optional dep

        return ChatGoogleGenerativeAI(model=identifier, google_api_key=api_key)

    if provider == "amazon_bedrock":
        from langchain_aws import ChatBedrock  # optional dep

        return ChatBedrock(
            model_id=identifier,
            region_name=config.get("region", "us-east-1"),
            aws_access_key_id=_decode_key(config.get("accessKeyId")),
            aws_secret_access_key=_decode_key(config.get("secretAccessKey")),
        )

    raise ValueError(
        f"Unsupported provider {provider!r}. "
        "Add a case in _build_chat_model or install the relevant langchain-* package."
    )


# ── Message / response conversion ────────────────────────────────────────────────────

def _to_lc_messages(messages: List[Dict[str, str]]) -> list:
    """Convert OpenAI-style message dicts to LangChain message objects."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    out = []
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content") or ""
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        else:
            out.append(HumanMessage(content=content))
    return out


def _extract_text(content: Any) -> str:
    """Flatten LangChain content (str or list-of-blocks) to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _from_lc_response(ai_msg: Any) -> Dict[str, Any]:
    """Convert a LangChain AIMessage to the OpenAI-style dict the agent expects.

    Output shape::

        {
            "content":       str,
            "finish_reason": str,
            "usage": {              # present when the provider returns usage
                "prompt_tokens":     int | None,
                "completion_tokens": int | None,
                "total_tokens":      int | None,
            },
            "tool_calls": [         # present when the model issued tool calls
                {
                    "id":   str,
                    "type": "function",
                    "function": {"name": str, "arguments": str},  # JSON string
                },
                …
            ],
        }
    """
    result: Dict[str, Any] = {
        "content": _extract_text(ai_msg.content),
        "finish_reason": (
            ai_msg.response_metadata.get("finish_reason")
            or ai_msg.response_metadata.get("stop_reason")
            or "stop"
        ),
    }

    # Usage — LangChain normalises to input_tokens / output_tokens.
    usage = getattr(ai_msg, "usage_metadata", None)
    if usage:
        result["usage"] = {
            "prompt_tokens":     usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens":      usage.get("total_tokens"),
        }

    # Tool calls — LangChain: {id, name, args:dict}  →  OpenAI: {id, type, function}
    lc_tools = getattr(ai_msg, "tool_calls", None)
    if lc_tools:
        result["tool_calls"] = [
            {
                "id":   tc.get("id", ""),
                "type": "function",
                "function": {
                    "name":      tc["name"],
                    "arguments": json.dumps(tc["args"]),
                },
            }
            for tc in lc_tools
        ]

    return result


# ── Service ───────────────────────────────────────────────────────────────────────────────────

class LangChainLlmService:
    """Multi-provider LLM service backed by LangChain chat model abstractions.

    Use the async factory ``from_db()`` to create an instance at startup.

    The service is safe to share across coroutines.  ``set_model()`` is
    protected by an ``asyncio.Lock`` so concurrent model switches are serialised.
    """

    def __init__(self, pool: Any, model_name: str, chat_model: Any, provider_name: str = "") -> None:
        self._pool          = pool
        self._model_name    = model_name
        self._chat_model    = chat_model
        self._provider_name = provider_name
        self._lock          = asyncio.Lock()

    # ── Factories ─────────────────────────────────────────────────────────

    @classmethod
    async def from_db(
        cls,
        pool: Any,
        model_name: Optional[str] = None,
    ) -> "LangChainLlmService":
        """Async factory: loads credentials from DB and builds the service.

        If *model_name* is ``None``, the row flagged ``is_default = true`` in
        ``admin_models_providers`` is used as the starting model.

        Raises ``ValueError`` when no enabled credential row is found.
        """
        row = await _fetch_model_row(pool, model_name)
        if not row:
            raise ValueError(
                f"No enabled credentials found for model {model_name!r}. "
                "Check admin_models / admin_models_providers."
            )
        chat_model  = _build_chat_model(row)
        actual_name = row["model_name"]
        logger.info(
            "llm_service: initialised model=%s provider=%s identifier=%s",
            actual_name,
            row["provider_name"],
            row["provider_model_identifier"],
        )
        return cls(pool, actual_name, chat_model, provider_name=row["provider_name"])

    @classmethod
    def from_env_azure(cls, pool: Any, settings: Any) -> "LangChainLlmService":
        """Fallback factory: build an Azure service from environment variables.

        Used when the DB has no model rows (e.g. a fresh install without seed data).
        """
        from langchain_openai import AzureChatOpenAI

        chat_model = AzureChatOpenAI(
            azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_version=settings.AZURE_OPENAI_API_VERSION,
            api_key=settings.AZURE_OPENAI_API_KEY,
        )
        logger.warning(
            "llm_service: DB credentials unavailable; using env-var Azure creds "
            "(deployment=%s)",
            settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        )
        return cls(pool, settings.AZURE_OPENAI_DEPLOYMENT_NAME, chat_model, provider_name="azure_openai")

    # ── Global model helpers ────────────────────────────────────────────

    def get_default_chat_model(self) -> Any:
        """Return the current global chat model instance."""
        return self._chat_model

    async def build_model_override_for_model_id(self, model_id: int) -> ModelOverride:
        """Build a ModelOverride for a specific ``admin_models.id``.

        Used by ``PromptCache`` to wire per-prompt model assignments.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_FETCH_BY_MODEL_ID, model_id)
        if not row:
            raise ValueError(
                f"No enabled credentials found for model_id={model_id}"
            )
        row_dict = dict(row)
        chat_model = _build_chat_model(row_dict)
        return ModelOverride(chat_model=chat_model, provider_name=row_dict["provider_name"])

    # ── Model switching ───────────────────────────────────────────────────

    def get_deployment(self) -> str:
        """Return the active model name (kept for backward compatibility)."""
        return self._model_name

    async def set_model(self, model_name: str) -> None:
        """Switch to a different model live, reloading credentials from DB.

        Thread-safe: guarded by ``asyncio.Lock``.
        """
        async with self._lock:
            row = await _fetch_model_row(self._pool, model_name)
            if not row:
                raise ValueError(
                    f"No enabled credentials found for model {model_name!r}"
                )
            new_model        = _build_chat_model(row)
            old_name            = self._model_name
            self._chat_model    = new_model
            self._model_name    = model_name
            self._provider_name = row["provider_name"]
            logger.info(
                "llm_service: switched %s → %s (provider=%s identifier=%s)",
                old_name,
                model_name,
                row["provider_name"],
                row["provider_model_identifier"],
            )

    # ── Auto-fallback ───────────────────────────────────────────────────────
    # When the active model's credentials/endpoint fail at call time (e.g. an
    # expired Azure key returning 401), transparently retry on a model that the
    # last health probe confirmed working, so one dead deployment can't take the
    # whole app down. The healthy model is promoted in-memory (not persisted) so
    # subsequent calls skip the dead one; on restart the admin's DB choice stands.

    @staticmethod
    def _provider_token_kwargs(provider_name: str, max_tokens: int) -> Dict[str, Any]:
        if provider_name == "azure_openai":
            return {"max_completion_tokens": max_tokens}
        return {"max_tokens": max_tokens}

    def _bind_model(
        self,
        base: Any,
        provider_name: str,
        model_name: str,
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict]] = None,
    ) -> Any:
        """Apply tools + the provider-correct token/temperature kwargs to a model."""
        model = base
        if tools:
            model = model.bind_tools(tools, tool_choice="auto")
        bind_kw = self._provider_token_kwargs(provider_name, max_tokens)
        if _supports_custom_temperature(base, model_name):
            bind_kw["temperature"] = temperature
        return model.bind(**bind_kw)

    async def _healthy_candidates(self, exclude: str) -> List[str]:
        """Names of models the last probe found healthy, minus *exclude*.

        Falls back to running a probe when no health snapshot is cached yet
        (cheap and cached afterwards).
        """
        from src.agent import llm_health

        cached = llm_health.cached_health()
        if not cached:
            try:
                cached = await llm_health.get_health(self._pool)
            except Exception as exc:  # noqa: BLE001
                logger.warning("llm_service: health probe for fallback failed: %s", exc)
                return []
        return [name for name, h in cached.items() if h.healthy and name != exclude]

    async def _promote(self, name: str, chat_model: Any, provider_name: str) -> None:
        """Swap the in-memory active model (not persisted to the DB)."""
        async with self._lock:
            self._model_name = name
            self._chat_model = chat_model
            self._provider_name = provider_name

    async def _ainvoke_with_fallback(
        self,
        lc_messages: List[Any],
        *,
        base: Any,
        provider_name: str,
        model_name: str,
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict]],
        promote: bool,
    ) -> Any:
        """Invoke *base*; on failure retry on healthy fallback models.

        ``promote`` swaps the service's active model to the first fallback that
        succeeds so later calls don't keep hitting the dead one.
        Raises the original exception when no fallback works.
        """
        model = self._bind_model(base, provider_name, model_name, max_tokens, temperature, tools)
        try:
            return await model.ainvoke(lc_messages)
        except Exception as primary_exc:  # noqa: BLE001
            logger.warning(
                "llm_service: model %r failed (%s); trying healthy fallback(s)",
                model_name or "(override)", primary_exc,
            )
            for cand in await self._healthy_candidates(exclude=model_name):
                try:
                    row = await _fetch_model_row(self._pool, cand)
                    if not row:
                        continue
                    cand_base = _build_chat_model(row)
                    cand_provider = row["provider_name"]
                    cand_model = self._bind_model(
                        cand_base, cand_provider, cand, max_tokens, temperature, tools
                    )
                    ai_msg = await cand_model.ainvoke(lc_messages)
                    logger.warning(
                        "llm_service: fell back %r → %r", model_name or "(override)", cand
                    )
                    if promote:
                        await self._promote(cand, cand_base, cand_provider)
                    return ai_msg
                except Exception as cand_exc:  # noqa: BLE001
                    logger.warning("llm_service: fallback %r also failed (%s)", cand, cand_exc)
                    continue
            reason = classify_llm_error(primary_exc)
            raise LLMUnavailableError(
                f"Model {model_name or '(override)'} failed and no healthy fallback "
                f"was available — {reason}"
            ) from primary_exc

    async def _healthy_streaming_base(self) -> tuple:
        """Return (base, provider, name) for streaming, proactively avoiding a
        model the last probe marked unhealthy when a healthy one exists."""
        from src.agent import llm_health

        cached = llm_health.cached_health()
        current = cached.get(self._model_name)
        if current is not None and not current.healthy:
            for cand in await self._healthy_candidates(exclude=self._model_name):
                try:
                    row = await _fetch_model_row(self._pool, cand)
                    if not row:
                        continue
                    cand_base = _build_chat_model(row)
                    await self._promote(cand, cand_base, row["provider_name"])
                    logger.warning(
                        "llm_service: active %r unhealthy; streaming via %r",
                        current.name, cand,
                    )
                    return cand_base, row["provider_name"], cand
                except Exception:  # noqa: BLE001
                    continue
        return self._chat_model, self._provider_name, self._model_name

    # ── Generation ────────────────────────────────────────────────────────

    def _token_kwargs(self, max_tokens: int) -> Dict[str, Any]:
        """Return the correct token-limit kwarg for the current provider.

        Azure OpenAI (GPT-5.x, O3, …) requires ``max_completion_tokens``.
        All other providers use the standard ``max_tokens``.
        """
        if self._provider_name == "azure_openai":
            return {"max_completion_tokens": max_tokens}
        return {"max_tokens": max_tokens}

    async def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: Optional[List[Dict]] = None,
        model_override: Optional[ModelOverride] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate a non-streaming response.

        Pass *model_override* (a :class:`ModelOverride` built by
        ``PromptCache``) to use a per-prompt model instead of the global
        active model.  When ``None`` the global model is used.
        ``tools`` should be OpenAI-format tool definitions; LangChain converts
        them to the wire format expected by each provider automatically.
        """
        lc_messages = _to_lc_messages(messages)

        if model_override is not None:
            base, provider_name, model_name, promote = (
                model_override.chat_model,
                model_override.provider_name,
                "",
                False,  # don't repoint the global model because of a per-prompt override
            )
        else:
            base, provider_name, model_name, promote = (
                self._chat_model,
                self._provider_name,
                self._model_name,
                True,
            )

        ai_msg = await self._ainvoke_with_fallback(
            lc_messages,
            base=base,
            provider_name=provider_name,
            model_name=model_name,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            promote=promote,
        )
        return _from_lc_response(ai_msg)

    async def generate_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        model_override: Optional[ModelOverride] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Yield raw text chunks (str)."""
        lc_messages = _to_lc_messages(messages)
        if model_override is not None:
            base, provider_name, model_name = (
                model_override.chat_model, model_override.provider_name, "",
            )
        else:
            base, provider_name, model_name = await self._healthy_streaming_base()
        model = self._bind_model(base, provider_name, model_name, max_tokens, temperature)

        async for chunk in model.astream(lc_messages):
            text = _extract_text(chunk.content)
            if text:
                yield text

    async def generate_streaming(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        model_override: Optional[ModelOverride] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield typed event dicts.

        Each event is one of::

            {"type": "delta",   "text":  str}
            {"type": "usage",   "usage": {"prompt_tokens": …, …}}
            {"type": "error",   "error": str}
        """
        lc_messages = _to_lc_messages(messages)
        if model_override is not None:
            base, provider_name, model_name = (
                model_override.chat_model, model_override.provider_name, "",
            )
        else:
            base, provider_name, model_name = await self._healthy_streaming_base()
        model = self._bind_model(base, provider_name, model_name, max_tokens, temperature)

        try:
            async for chunk in model.astream(lc_messages):
                text = _extract_text(chunk.content)
                if text:
                    yield {"type": "delta", "text": text}

                # Usage arrives in the final chunk for providers that support it.
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    yield {
                        "type": "usage",
                        "usage": {
                            "prompt_tokens":     usage.get("input_tokens"),
                            "completion_tokens": usage.get("output_tokens"),
                            "total_tokens":      usage.get("total_tokens"),
                        },
                    }
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "error": classify_llm_error(exc)}
