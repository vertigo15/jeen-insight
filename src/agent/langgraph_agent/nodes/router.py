"""fused_router node — classifies each question into one route in a single LLM call.

Routes
------
needs_query     The question requires a database query (text-to-SQL). May carry
                ``prior_refs`` when it builds on a prior result ("the top 4
                products from T3 — show their sales").
needs_analysis  The question asks for an ML skill (anomalies, forecast, …).
from_memory     The question is about a prior turn's answer or data and can be
                served from the stored result (replay, recompute, what-if).
history_lookup  A meta-question about past questions ("did I ask about X last
                week?") answered by searching persisted history.
capability      A question about the assistant itself.
out_of_scope    The question is unrelated to the data source.
unsafe          The question requests data mutation or is otherwise blocked.
greeting        Caught by local regex — zero LLM cost, ~0ms.

The router sees the turn ledger (``nodes/context.py``): question, SQL, answer
and result shape of the last N turns, each with a handle ``T1``…``TN``. It
names the turns a question depends on in ``prior_refs``.

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
from datetime import date
from typing import Any, Dict, List, Optional

from src.agent.langgraph_agent.nodes.context import find_turn, ledger_for, render_ledger
from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage

logger = logging.getLogger(__name__)

_VALID_ROUTES = frozenset({
    "needs_query", "needs_analysis", "from_memory", "history_lookup",
    "capability", "catalog_help", "out_of_scope", "unsafe",
})
_MAX_PRIOR_REFS = 3
_MAX_HISTORY_KEYWORDS = 6

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

# ── Capability short-circuit ──────────────────────────────────────────────────
# "which ML models can I use?", "what can you do?" are questions about THIS
# assistant, not the data. The router LLM sometimes misfiles them as
# ``out_of_scope`` (or defaults to ``needs_query`` on a parse failure), which
# yields the canned refusal instead of the capability answer. A strict
# deterministic cue corrects that with no extra LLM call.
#
# Strict on purpose: a direct "what can you do" phrase, OR *both* an
# assistant/listing cue and an ML/analysis qualifier must appear. Bare "model"
# is deliberately NOT a qualifier — in AdventureWorks it is a product attribute,
# so "which models sold best in 2008?" and "sales by product model" stay data
# questions. Skill names (forecast, anomaly, …) are not qualifiers either, so
# "can you forecast revenue?" stays a data/analysis request.
_CAP_DIRECT_RE = re.compile(
    r"\bwhat\s+(can|do)\s+(you|this\s+(app|tool|assistant|bot)|jeen)\b(?:\s+\w+){0,3}?\s+do\b"
    r"|\bwhat\s+are\s+your\s+(capabilit|abilit|feature)"
    r"|מה\s+(אתה|את)\s+(יכול|יכולה)\s+לעשות",
    re.IGNORECASE,
)
_CAP_OWNERSHIP_RE = re.compile(
    r"\byou\b|\bthis\s+(app|tool|assistant|bot)\b|\bjeen\b"
    r"|\bcan\s+(i|we|you|one)\s+(use|run|do|ask|choose|pick|select|try|apply)\b"
    r"|\b(should|shall|do|does|can)\s+(i|we)\b"
    r"|\bdo\s+you\s+(offer|support|have|provide)\b"
    r"|\bavailable\b|\bsupported\b|\bi\s+can\s+use\b"
    r"|\bאפשר\b|\bאתה\b|\bאני\s+יכול|\bנוכל\b|\bאנחנו\b",
    re.IGNORECASE,
)
_CAP_QUALIFIER_RE = re.compile(
    r"\bml\b|\bmachine\s+learning\b|\bai\b|\balgorithms?\b|\bskills?\b"
    r"|\banalys[ie]s\b|\banalytics\b|\bstatistical\b|\bcapabilit"
    r"|אלגוריתמ|ניתוח|יכולות|מודל",
    re.IGNORECASE,
)


def detect_capability_intent(question: str) -> bool:
    """True when the question is about THIS assistant — what it can do or which
    ML skills/algorithms it offers. Deterministic, no LLM.

    Strict: a direct "what can you do" phrase, or *both* an assistant/listing
    cue and an ML/analysis qualifier. See the regexes above for why bare
    "model" and skill names are excluded (they keep data questions data).
    """
    text = (question or "").strip()
    if not text:
        return False
    if _CAP_DIRECT_RE.search(text):
        return True
    return bool(_CAP_OWNERSHIP_RE.search(text) and _CAP_QUALIFIER_RE.search(text))


# ── Catalog-help short-circuit ────────────────────────────────────────────────
# "what measures/dimensions/fields can I ask about?" is a question about the
# CATALOG of the connected data source, not a data query. The router LLM tends
# to send it to needs_query, where the SQL generator answers with unstructured
# prose. A strict deterministic cue routes it to the catalog_help node, which
# answers deterministically from the metadata bundle.
_CATALOG_NOUN_RE = re.compile(
    r"\b(measures?|metrics?|kpis?|dimensions?|columns?|fields?|tables?|attributes?)\b"
    r"|מדד|מדדים|מימד|מימדים|עמודות|שדות|טבלאות|מדדים",
    re.IGNORECASE,
)
_CATALOG_LIST_RE = re.compile(
    r"\b(what|which|list|available|ask\s+about|can\s+i\s+(ask|query|use|see)|do\s+you\s+(have|offer|support)|what\s+can\s+i)\b"
    r"|אילו|רשימה|אפשר\s+לשאול|מה\s+יש",
    re.IGNORECASE,
)
# A concrete data ask ("… by month", "top 5", "total …") is never catalog help,
# even if it mentions the word "measures".
_CATALOG_DATA_RE = re.compile(
    r"\bby\s+(day|week|month|quarter|year|region|product|customer|category|store)\b"
    r"|\bper\s+\w+|\btop\s+\d|\b(sum|avg|average|total|count|max|min|median)\b"
    r"|\blast\s+\d|\bbetween\b|\bin\s+20\d\d\b",
    re.IGNORECASE,
)


def detect_catalog_help_intent(question: str) -> bool:
    """True when the question asks which measures/dimensions/fields/tables are
    available to ask about (catalog listing), not for the data itself.

    Strict: a listing/availability cue AND a catalog noun, and never when the
    question carries a concrete data ask (aggregation, grain, top-N, a year).
    """
    text = (question or "").strip()
    if not text:
        return False
    if _CATALOG_DATA_RE.search(text):
        return False
    return bool(_CATALOG_NOUN_RE.search(text) and _CATALOG_LIST_RE.search(text))


# Where the final ML-vs-SQL decision came from (surfaced as ``routing.source``).
ROUTE_SOURCE_LLM = "router_llm"
ROUTE_SOURCE_CUE = "keyword_cue"
ROUTE_SOURCE_DISABLED = "ml_disabled"
ROUTE_SOURCE_OVERRIDE = "request_override"
ROUTE_SOURCE_GREETING = "greeting"
ROUTE_SOURCE_PLANNER_FALLBACK = "planner_fallback"
ROUTE_SOURCE_UNCERTAIN = "route_uncertain"
ROUTE_SOURCE_CAPABILITY_CUE = "capability_cue"
ROUTE_SOURCE_CATALOG_CUE = "catalog_cue"

# Below this router confidence — with no strong keyword cue — the SQL-vs-ML
# choice is treated as genuinely ambiguous and the user is asked instead of
# guessed at. Kept high enough that clear questions never trigger a prompt.
CLARIFY_CONFIDENCE = 0.6


def _route_clarify_message(confidence: Optional[float]) -> str:
    sure = f" (only about {int(round((confidence or 0) * 100))}% sure either way)" if confidence is not None else ""
    return (
        f"I can take this two ways{sure}: answer it directly with a database query, "
        "or run a statistical/ML analysis (forecast, anomaly check, drivers, segments, …). "
        "Which would you like?"
    )


def resolve_ml_route(
    llm_route: str,
    llm_reason: str,
    question: str,
    *,
    ml_skills_enabled: bool,
    analysis_override: Any,
    confidence: Optional[float] = None,
) -> Dict[str, Any]:
    """Apply the ML gate, the keyword-cue upgrade and the low-confidence clarify
    to the router LLM's answer.

    Returns ``{"route", "reason", "source", "skill_hint", "confidence"}``. This is
    the one place the ML-vs-SQL rule lives; the node and the dry-run endpoint both
    call it. ``route`` may be ``clarify_route`` when the choice is ambiguous.
    """
    from src.agent.analysis_planner import detect_analysis_intent  # noqa: PLC0415

    route, reason = llm_route, llm_reason or ""
    hint = detect_analysis_intent(question) if question else None
    ml_allowed = bool(ml_skills_enabled) and analysis_override is not False

    # 1. Explicit request override — the user already chose ("Answer with SQL
    #    instead" → False, "Run the analysis" → True). It only ever flips between
    #    the two DATA routes; it must NEVER turn a greeting / from_memory /
    #    capability / out_of_scope / unsafe classification into a query or a model.
    if route in ("needs_query", "needs_analysis"):
        if analysis_override is False:
            return {"route": "needs_query", "reason": f"{reason} (answering with SQL as requested)".strip(),
                    "source": ROUTE_SOURCE_OVERRIDE, "skill_hint": hint, "confidence": confidence}
        if analysis_override is True and ml_skills_enabled:
            return {"route": "needs_analysis", "reason": "running the analysis as requested",
                    "source": ROUTE_SOURCE_OVERRIDE, "skill_hint": hint, "confidence": confidence}

    # 2. Router leaned ML but ML is disabled → SQL.
    if route == "needs_analysis" and not ml_allowed:
        return {"route": "needs_query", "reason": f"{reason} (ML skills disabled; answering with SQL)".strip(),
                "source": ROUTE_SOURCE_DISABLED, "skill_hint": hint, "confidence": confidence}

    # 3. Strong keyword cue → ML. A confident signal; never ask.
    if route == "needs_query" and ml_allowed and hint:
        return {"route": "needs_analysis",
                "reason": f"keyword cue for {hint}" + (f"; router said: {reason}" if reason else ""),
                "source": ROUTE_SOURCE_CUE, "skill_hint": hint, "confidence": confidence}

    # 4. Genuinely ambiguous SQL-vs-ML → ask rather than guess. Only when ML is
    #    allowed, there is no strong cue, the router is a data route, and the
    #    model itself reported low confidence. (No extra LLM call — the single
    #    router call already produced the confidence, so this stays cheap and
    #    fires only on real borderline questions.)
    if (ml_allowed and hint is None and route in ("needs_query", "needs_analysis")
            and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            and confidence < CLARIFY_CONFIDENCE):
        return {"route": "clarify_route",
                "reason": f"unsure whether to answer directly or run an analysis (confidence {confidence:.2f})",
                "source": ROUTE_SOURCE_UNCERTAIN, "skill_hint": None, "confidence": confidence}

    return {"route": route, "reason": reason, "source": ROUTE_SOURCE_LLM, "skill_hint": hint, "confidence": confidence}


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
    # A question about the assistant itself is neither SQL nor ML; the node's
    # deterministic capability cue routes it to ``capability`` regardless of the
    # ML gate, so the dry-run prediction must say the same (no drift).
    if detect_capability_intent(q):
        return {"question": q, "would_route": "capability", "source": ROUTE_SOURCE_CAPABILITY_CUE, "skill_hint": None,
                "ml_skills_enabled": bool(ml_skills_enabled), "analysis_override": analysis_override,
                "reason": "capability cue: a question about this assistant's own ML skills, not the data"}
    if detect_catalog_help_intent(q):
        return {"question": q, "would_route": "catalog_help", "source": ROUTE_SOURCE_CATALOG_CUE, "skill_hint": None,
                "ml_skills_enabled": bool(ml_skills_enabled), "analysis_override": analysis_override,
                "reason": "catalog cue: a question about which measures/dimensions/fields are available"}
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


def _parse_prior_refs(raw: Any, ledger: List[Dict[str, Any]]) -> List[str]:
    """Canonical ledger handles from whatever the model returned (list or string)."""
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[,\s]+", raw) if part]
    if not isinstance(raw, list):
        return []
    handles: List[str] = []
    for item in raw:
        turn = find_turn(ledger, item)
        if turn and turn["handle"] not in handles:
            handles.append(turn["handle"])
        if len(handles) >= _MAX_PRIOR_REFS:
            break
    return handles


def _parse_history_query(raw: Any) -> Optional[Dict[str, Any]]:
    """``{keywords: [...], since: str|None, until: str|None}`` or None."""
    if not isinstance(raw, dict):
        return None
    keywords = raw.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
    words = [str(k).strip() for k in (keywords or []) if str(k).strip()][:_MAX_HISTORY_KEYWORDS]
    since = str(raw["since"]).strip() if raw.get("since") else None
    until = str(raw["until"]).strip() if raw.get("until") else None
    if not words and not since and not until:
        return None
    return {"keywords": words, "since": since, "until": until}


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
        # The ledger gives the router each prior turn's question, SQL, answer
        # and result shape under a handle (T1…TN), so it can both detect
        # follow-ups and name the turn a question depends on. It embeds prior
        # question text (user data) → fence it.
        ledger = ledger_for(state)
        rendered = render_ledger(ledger)
        summary = (
            fence_untrusted(rendered, label="conversation ledger")
            if rendered else "No prior conversation."
        )
        source = state.get("connection_display_name") or "the database"

        system_msg = await prompt_loader.arender(
            "fused_router",
            question=question,
            conversation_summary=summary,
            source_description=source,
            today=date.today().isoformat(),
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
        confidence: Any = None
        prior_refs: List[str] = []
        history_query: Optional[Dict[str, Any]] = None

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
            raw_conf = parsed.get("confidence")
            if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
                confidence = max(0.0, min(1.0, float(raw_conf)))
            prior_refs = _parse_prior_refs(parsed.get("prior_refs"), ledger)
            history_query = _parse_history_query(parsed.get("history_query"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            logger.warning(
                "fused_router: could not parse JSON response %r — defaulting to needs_query",
                content[:200],
            )

        # A history question with nothing to search for cannot be answered;
        # treat it as a data question rather than returning an empty list.
        if route == "history_lookup" and history_query is None:
            route, reason = "needs_query", f"{reason} (history_lookup without a query)".strip()

        # Capability backstop: the router sometimes misfiles "which ML models can
        # I use?" as out_of_scope (or defaults to needs_query on a parse failure),
        # so a strict deterministic cue rescues it to the capability answer. Only
        # these two routes are corrected; a data/analysis/memory/unsafe route is
        # never touched. Handled here (not via resolve_ml_route) so the source is
        # reported as capability_cue rather than router_llm.
        if route in ("out_of_scope", "needs_query") and detect_capability_intent(question):
            decision: Dict[str, Any] = {
                "route": "capability",
                "reason": "capability cue: a question about this assistant's own ML skills, not the data",
                "source": ROUTE_SOURCE_CAPABILITY_CUE,
                "skill_hint": None,
                "confidence": confidence,
            }
        elif route in ("out_of_scope", "needs_query") and detect_catalog_help_intent(question):
            decision = {
                "route": "catalog_help",
                "reason": "catalog cue: a question about which measures/dimensions/fields are available",
                "source": ROUTE_SOURCE_CATALOG_CUE,
                "skill_hint": None,
                "confidence": confidence,
            }
        else:
            decision = resolve_ml_route(
                route, reason, question,
                confidence=confidence,
                ml_skills_enabled=ml_skills_enabled,
                analysis_override=state.get("analysis_enabled_override"),
            )
        route, reason = decision["route"], decision["reason"]

        logger.info(
            "fused_router: route=%s | source=%s | prior_refs=%s | reason=%s",
            route, decision["source"], prior_refs, reason,
        )

        usage = response.get("usage") or {}
        result: Dict[str, Any] = {
            "route": route,
            "route_reason": reason,
            "route_source": decision["source"],
            "prior_refs": prior_refs,
            "history_query": history_query,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "fused_router": system_msg},
        }
        # Ambiguous SQL-vs-ML: ask instead of guessing. The message becomes the
        # answer; the UI offers "Run the analysis" / "Answer directly".
        if route == "clarify_route":
            msg = _route_clarify_message(decision.get("confidence"))
            result["answer"] = msg
            result["route_clarification"] = {
                "message": msg,
                "confidence": decision.get("confidence"),
                "skill_hint": decision.get("skill_hint"),
            }
        return result

    return fused_router
