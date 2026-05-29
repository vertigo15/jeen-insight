"""Memory management nodes.

memory_shrink_check   — pure-Python node; flags when history exceeds token budget.
memory_summarizer     — small-model LLM node; condenses history into a short summary.

Both are returned from factory functions so they close over their configuration
without global state.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import AzureOpenAILlmService

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (no tokenizer needed)."""
    return max(1, len(text) // 4)


def _merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


# ── Node factories ────────────────────────────────────────────────────────────


def make_memory_shrink_check(max_history_tokens: int):
    """Return a sync node that sets ``is_over_budget`` in state."""

    def memory_shrink_check(state: AgentState) -> Dict[str, Any]:
        history = state.get("conversation_history") or []
        total = sum(_estimate_tokens(str(qa)) for qa in history)
        over = total > max_history_tokens
        logger.info(
            "memory_shrink_check: ~%d estimated tokens (budget=%d, over_budget=%s)",
            total,
            max_history_tokens,
            over,
        )
        return {"is_over_budget": over}

    return memory_shrink_check


def make_memory_summarizer(llm: AzureOpenAILlmService, prompt_loader: PromptLoader):
    """Return an async node that summarises conversation history with a small model call."""

    async def memory_summarizer(state: AgentState) -> Dict[str, Any]:
        history = state.get("conversation_history") or []
        history_text = "\n".join(
            f"Q: {qa.get('natural_language_query', '')}\n"
            f"SQL: {qa.get('generated_sql', '')}"
            for qa in history
        )

        system_msg = prompt_loader.render(
            "memory_summarizer",
            conversation_history=history_text or "(empty)",
        )

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": "Summarize the conversation above concisely."},
            ],
            temperature=0.1,
            max_tokens=300,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        summary = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        logger.info("memory_summarizer: produced %d-char summary in %dms", len(summary), latency_ms)

        return {
            "memory_summary": summary,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": _merge_usage(state.get("token_usage") or {}, usage),
        }

    return memory_summarizer
