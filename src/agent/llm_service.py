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
from typing import Any, AsyncGenerator, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)


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
        p.litellm_prefix,
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
        p.litellm_prefix,
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
        p.litellm_prefix,
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
            model = model_override.chat_model
            token_kw = (
                {"max_completion_tokens": max_tokens}
                if model_override.provider_name == "azure_openai"
                else {"max_tokens": max_tokens}
            )
        else:
            model = self._chat_model
            token_kw = self._token_kwargs(max_tokens)

        if tools:
            model = model.bind_tools(tools)
        model = model.bind(temperature=temperature, **token_kw)

        ai_msg = await model.ainvoke(lc_messages)
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
            base = model_override.chat_model
            token_kw = (
                {"max_completion_tokens": max_tokens}
                if model_override.provider_name == "azure_openai"
                else {"max_tokens": max_tokens}
            )
        else:
            base = self._chat_model
            token_kw = self._token_kwargs(max_tokens)
        model = base.bind(temperature=temperature, **token_kw)

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
            base = model_override.chat_model
            token_kw = (
                {"max_completion_tokens": max_tokens}
                if model_override.provider_name == "azure_openai"
                else {"max_tokens": max_tokens}
            )
        else:
            base = self._chat_model
            token_kw = self._token_kwargs(max_tokens)
        model = base.bind(temperature=temperature, **token_kw)

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
            yield {"type": "error", "error": str(exc)}
