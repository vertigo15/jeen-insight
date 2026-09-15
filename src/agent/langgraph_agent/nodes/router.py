"""fused_router node — classifies each question into one route in a single LLM call.

Routes
------
needs_query     The question requires a database query (text-to-SQL).
needs_analysis  The question asks for an ML skill (anomalies, forecast, …).
from_memory     The question can be answered from conversation history.
out_of_scope    The question is unrelated to the data source.
unsafe          The question requests data mutation or is otherwise blocked.
greeting        Caught by local regex — zero LLM cost, ~0ms.

On JSON parse failure the node defaults to ``needs_query`` so the flow
continues safely rather than aborting.

ML vs SQL — the decision, in order (see :func:`resolve_ml_route` and
:func:`explain_routing`, which share one code path so the dry-run endpoint
``GET /api/analysis/routing`` cannot drift from what the node does):

1. ``ML_SKILLS_ENABLED`` off, or the request carries ``analysis: false``
   ("Answer with SQL instead") → always SQL.        source = ``ml_disabled`` /
   ``request_override``
2. Greeting regex → greeting, no LLM.                source = ``greeting``
3. Router LLM says ``needs_analysis``.                source = ``router_llm``
4. Router LLM says ``needs_query`` but the question carries a strong keyword
   cue (``detect_analysis_intent``: "forecast", "anomal…", "why did … drop",
   "segment", …) → upgraded to ML.                   source = ``keyword_cue``
5. Later, the planner may find no skill fits the catalog and hand the question
   back to SQL.                                       source = ``planner_fallback``
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
from src.agent.token_usage import merge_usage

logger = logging.getLogger(__name__)

_VALID_ROUTES = frozenset({"needs_query", "needs_analysis", "from_memory", "out_of_scope", "unsafe"})

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

# Where the final ML-vs-SQL decision came from (surfaced as ``routing.source``).
ROUTE_SOURCE_LLM = "router_llm"
ROUTE_SOURCE_CUE = "keyword_cue"
ROUTE_SOURCE_DISABLED = "ml_disabled"
ROUTE_SOURCE_OVERRIDE = "request_override"
ROUTE_SOURCE_GREETING = "greeting"
ROUTE_SOURCE_PLANNER_FALLBACK = "planner_fallback"


def resolve_ml_route(
    llm_route: str,
    llm_reason: str,
    question: str,
    *,
    ml_skills_enabled: bool,
    analysis_override: Any,
) -> Dict[str, Any]:
    """Apply the ML gate and the keyword-cue upgrade to the router LLM's answer.

    Returns ``{"route", "reason", "source", "skill_hint"}``. This is the one
    place the ML-vs-SQL rule lives; the node and the dry-run endpoint both call it.
    """
    from src.agent.analysis_planner import detect_analysis_intent  # noqa: PLC0415

    route, reason = llm_route, llm_reason or ""
    ml_allowed = bool(ml_skills_enabled) and analysis_override is not False
    hint = detect_analysis_intent(question) if question else None
    if route == "needs_analysis" and not ml_allowed:
        source = ROUTE_SOURCE_OVERRIDE if analysis_override is False else ROUTE_SOURCE_DISABLED
        suffix = "answering with SQL as requested" if analysis_override is False else "ML skills disabled; answering with SQL"
        return {"route": "needs_query", "reason": f"{reason} ({suffix})".strip(), "source": source, "skill_hint": hint}
    if route == "needs_query" and ml_allowed and hint:
        return {
            "route": "needs_analysis",
            "reason": f"keyword cue for {hint}" + (f"; router said: {reason}" if reason else ""),
            "source": ROUTE_SOURCE_CUE,
            "skill_hint": hint,
        }
    return {"route": route, "reason": reason, "source": ROUTE_SOURCE_LLM, "skill_hint": hint}


def explain_routing(question: str, *, ml_skills_enabled: bool, analysis_override: Any = None) -> Dict[str, Any]:
    """Predict, without any LLM call, how a question will be routed.

    The deterministic rules decide most cases outright; when they do not, the
    prediction is ``router_decides`` and the router LLM picks between SQL and
    ML at run time. ``would_route`` therefore is one of ``needs_query``,
    ``needs_analysis``, ``greeting`` or ``router_decides``.
    """
    q = question or ""
    if _GREETING_RE.match(q):
        return {"question": q, "would_route": "greeting", "source": ROUTE_SOURCE_GREETING, "skill_hint": None,
                "ml_skills_enabled": bool(ml_skills_enabled), "analysis_override": analysis_override,
                "reason": "greeting regex matched; no LLM call, no SQL"}
    if not ml_skills_enabled:
        return {"question": q, "would_route": "needs_query", "source": ROUTE_SOURCE_DISABLED, "skill_hint": None,
                "ml_skills_enabled": False, "analysis_override": analysis_override,
                "reason": "ML_SKILLS_ENABLED is off on this deployment; every data question is text-to-SQL"}
    if analysis_override is False:
        return {"question": q, "would_route": "needs_query", "source": ROUTE_SOURCE_OVERRIDE, "skill_hint": None,
                "ml_skills_enabled": True, "analysis_override": False,
                "reason": "the request carries analysis=false (\"Answer with SQL instead\")"}
    # Simulate the two router answers: a keyword cue makes the outcome ML either way.
    as_query = resolve_ml_route("needs_query", "", q, ml_skills_enabled=True, analysis_override=analysis_override)
    if as_query["route"] == "needs_analysis":
        return {"question": q, "would_route": "needs_analysis", "source": ROUTE_SOURCE_CUE,
                "skill_hint": as_query["skill_hint"], "ml_skills_enabled": True, "analysis_override": analysis_override,
                "reason": f"strong keyword cue for {as_query['skill_hint']}: routed to ML even if the router LLM says SQL "
                          "(the planner may still hand it back to SQL when no catalog table fits)"}
    return {"question": q, "would_route": "router_decides", "source": ROUTE_SOURCE_LLM, "skill_hint": None,
            "ml_skills_enabled": True, "analysis_override": analysis_override,
            "reason": "no keyword cue; the router LLM decides between text-to-SQL and an ML skill from the question's intent"}


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


def make_fused_router(
    router_llm: LangChainLlmService,
    prompt_loader: PromptLoader,
    *,
    ml_skills_enabled: bool = False,
):
    """Return an async ``fused_router`` node.

    ``ml_skills_enabled`` gates the ``needs_analysis`` route: when off, the LLM's
    ``needs_analysis`` collapses to ``needs_query`` so the graph never enters the
    analysis branch. When on, a strong local keyword cue ("forecast", "anomal…")
    upgrades a ``needs_query`` classification, because a router that has never
    seen the new route tends to under-use it.
    """

    async def fused_router(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")

        # ── Fast local path — zero LLM cost ──────────────────────────────
        if _GREETING_RE.match(question):
            logger.info("fused_router: greeting short-circuit for %r", question[:60])
            return {
                "route": "greeting",
                "route_reason": "local regex match",
                "route_source": ROUTE_SOURCE_GREETING,
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

        decision = resolve_ml_route(
            route, reason, question,
            ml_skills_enabled=ml_skills_enabled,
            analysis_override=state.get("analysis_enabled_override"),
        )
        route, reason = decision["route"], decision["reason"]

        logger.info("fused_router: route=%s | source=%s | reason=%s", route, decision["source"], reason)

        usage = response.get("usage") or {}
        return {
            "route": route,
            "route_reason": reason,
            "route_source": decision["source"],
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "fused_router": system_msg},
        }

    return fused_router
