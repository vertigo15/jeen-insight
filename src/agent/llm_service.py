"""Azure OpenAI LLM service for Jeen Insights."""

from openai import AzureOpenAI
from typing import Dict, Any, List, Optional, AsyncGenerator
import asyncio
import json


class AzureOpenAILlmService:
    """
    Azure OpenAI LLM service for Jeen Insights.
    Provides text generation capabilities using Azure OpenAI.
    """
    
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        deployment: str,
        api_version: str = "2025-01-01-preview",
        timeout_seconds: int = 0,
    ):
        self.client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint
        )
        self.deployment = deployment
        self.timeout_seconds = timeout_seconds  # 0 = no timeout

    async def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: Optional[List[Dict]] = None,
        timeout: Optional[int] = None,  # per-call override of self.timeout_seconds
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate response from Azure OpenAI.

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            tools: Optional tool definitions for function calling

        Returns:
            Response dict with 'content', 'tool_calls', etc.

        Raises:
            asyncio.TimeoutError: When timeout_seconds > 0 and the call exceeds it.
        """
        loop = asyncio.get_event_loop()

        params = {
            "model": self.deployment,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        coro = loop.run_in_executor(
            None,
            lambda: self.client.chat.completions.create(**params)
        )

        # Per-call timeout override (from request) takes priority over instance default.
        effective_timeout = timeout if timeout is not None else self.timeout_seconds
        if effective_timeout > 0:
            response = await asyncio.wait_for(coro, timeout=effective_timeout)
        else:
            response = await coro
        
        choice = response.choices[0]
        result = {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason,
        }

        # Token usage (input + output). Azure occasionally omits this on
        # streaming or older API versions; surface what we got, default to None.
        usage = getattr(response, "usage", None)
        if usage is not None:
            result["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }

        # Handle tool calls if present
        if choice.message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in choice.message.tool_calls
            ]
        
        return result
    
    async def generate_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Stream response from Azure OpenAI (text-only).

        Yields raw content chunks. For streaming + usage, use
        ``generate_streaming`` which yields typed events.
        """
        loop = asyncio.get_event_loop()

        # Create stream in executor
        stream = await loop.run_in_executor(
            None,
            lambda: self.client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                temperature=temperature,
                max_completion_tokens=max_tokens,
                stream=True
            )
        )

        # Yield chunks
        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def generate_streaming(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream typed events from Azure OpenAI.

        Yields one of:
        - ``{"type": "delta",   "text": str}``  for each non-empty content chunk
        - ``{"type": "usage",   "usage": {prompt_tokens, completion_tokens, total_tokens}}``
          (Azure returns usage as a final separate chunk when ``stream_options.include_usage`` is set)
        - ``{"type": "error",   "error": str}`` if the upstream call fails

        Implementation note: the underlying ``openai`` client is sync, so we
        drive it on a worker thread and bridge events to the event loop via
        an ``asyncio.Queue``. Yielding exits cleanly when the producer thread
        signals end-of-stream.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def _put(item: Any) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(item), loop)

        def _producer() -> None:
            try:
                stream = self.client.chat.completions.create(
                    model=self.deployment,
                    messages=messages,
                    temperature=temperature,
                    max_completion_tokens=max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                for chunk in stream:
                    # `usage` chunks have empty `choices` (Azure/OpenAI convention).
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        _put({
                            "type": "usage",
                            "usage": {
                                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                                "completion_tokens": getattr(usage, "completion_tokens", None),
                                "total_tokens": getattr(usage, "total_tokens", None),
                            },
                        })
                    choices = getattr(chunk, "choices", None) or []
                    if choices:
                        delta = getattr(choices[0], "delta", None)
                        text = getattr(delta, "content", None) if delta else None
                        if text:
                            _put({"type": "delta", "text": text})
            except Exception as e:  # noqa: BLE001
                _put({"type": "error", "error": str(e)})
            finally:
                _put(_SENTINEL)

        loop.run_in_executor(None, _producer)

        while True:
            item = await queue.get()
            if item is _SENTINEL:
                return
            yield item
