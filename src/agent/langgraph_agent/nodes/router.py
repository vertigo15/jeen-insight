"""fused_router node — classifies each question into one route in a single LLM call.

Routes
------
needs_query   The question requires a database query.
from_memory   The question can be answered from conversation history.
out_of_scope  The question is unrelated to the data source.
unsafe        The question requests data mutation or is otherwise blocked.
greeting      Caught by local regex — zero LLM cost, ~0ms.

On JSON parse failure the node defaults to ``needs_query`` so the flow
continues safely rather than aborting.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict

from src.agent.langgraph_agent.nodes.artifacts import build_artifact_manifest
from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService

logger = logging.getLogger(__name__)

_VALID_ROUTES = frozenset({"needs_query", "from_memory", "out_of_scope", "unsafe"})

# ── Greeting short-circuit ────────────────────────────────────────────────────
# Simple inputs that are clearly social/conversational are caught locally before
# any Azure OpenAI round-trip is made, saving ~2-3s per greeting.
_GREETING_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|hiya|howdy|sup|yo"
    r"|good\s*(morning|afternoon|evening|day)"
    r"|hola|ciao|bonjour|salut|hallo|oi|olá"
    r"|thanks|thank\s*you|thx|cheers"
    r"|bye|goodbye|cya|see\s*you"
    r"|ok|okay|alright|got\s*it|sure|cool"
    r")\W*$",
    re.IGNORECASE,
)

_GREETING_ANSWER = (
    "Hello! I'm Jeen Insights, your AI data analyst. "
    "Ask me anything about your data and I'll query it for you."
)

# How many recent turns to surface to the router when no summary exists.
_ROUTER_HISTORY_TURNS = 3


def _format_recent_history(history: Any) -> str:
    """Build a compact ``Q: … / SQL: …`` block from the last few turns.

    Used as a fallback for the router's {conversation_summary} placeholder when
    no condensed memory summary exists yet, so the router can still detect
    follow-up questions ("and for last month?") that depend on prior context.
    """
    if not history:
        return ""
    recent = list(history)[-_ROUTER_HISTORY_TURNS:]
    lines = []
    for qa in recent:
        q = (qa.get("natural_language_query") or "").strip()
        sql = (qa.get("generated_sql") or "").strip()
        if not q:
            continue
        line = f"Q: {q}"
        if sql:
            line += f"\nSQL: {sql}"
        lines.append(line)
    return "\n".join(lines)


def _merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


def make_fused_router(router_llm: LangChainLlmService, prompt_loader: PromptLoader):
    """Return an async ``fused_router`` node."""

    async def fused_router(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")

        # ── Fast local path — zero LLM cost ──────────────────────────────
        if _GREETING_RE.match(question):
            logger.info("fused_router: greeting short-circuit for %r", question[:60])
            return {
                "route": "greeting",
                "route_reason": "local regex match",
                "answer": _GREETING_ANSWER,
            }

        # ── LLM classification ────────────────────────────────────────────
        # Prefer the condensed memory summary; otherwise fall back to a compact
        # block of the most recent turns so follow-ups still have context.
        history = state.get("conversation_history")
        summary = state.get("memory_summary")
        if not summary:
            summary = _format_recent_history(history)
        summary = summary or "No prior conversation."
        # Append a manifest of prior result sets (columns, row counts, small
        # stats) so the router can tell when a question is a follow-up over
        # already-retrieved data vs. one needing a fresh query.
        manifest = build_artifact_manifest(history or [])
        if manifest:
            # Manifest embeds prior question text (user data) → fence it.
            summary = f"{summary}\n\n{fence_untrusted(manifest, label='prior results')}"
        source = state.get("connection_display_name") or "the database"

        system_msg = await prompt_loader.arender(
            "fused_router",
            question=question,
            conversation_summary=summary,
            source_description=source,
        )
        model_override = await prompt_loader.model_override_for("fused_router")

        t0 = time.monotonic()
        response = await router_llm.generate(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=150,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = (response.get("content") or "").strip()
        route = "needs_query"
        reason = ""

        # Strip possible markdown fences before JSON parsing
        json_str = content
        if "```" in content:
            start = content.find("```") + 3
            if content[start:].startswith("json"):
                start += 4
            end = content.find("```", start)
            json_str = content[start:end].strip() if end > start else content

        try:
            parsed = json.loads(json_str)
            candidate = str(parsed.get("route", "needs_query")).strip().lower()
            if candidate in _VALID_ROUTES:
                route = candidate
            else:
                logger.warning(
                    "fused_router: unknown route %r — defaulting to needs_query", candidate
                )
            reason = str(parsed.get("reason", ""))
        except (json.JSONDecodeError, AttributeError, TypeError):
            logger.warning(
                "fused_router: could not parse JSON response %r — defaulting to needs_query",
                content[:200],
            )

        logger.info("fused_router: route=%s | reason=%s", route, reason)

        usage = response.get("usage") or {}
        return {
            "route": route,
            "route_reason": reason,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": _merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "fused_router": system_msg},
        }

    return fused_router
