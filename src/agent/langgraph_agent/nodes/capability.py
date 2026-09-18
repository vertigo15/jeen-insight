"""capability_answer node — answers questions about the assistant itself.

Handles the ``capability`` route: "what does this app do?", "what analyses can
I run and how do I trigger them?", "can I change the ML model (e.g. 3-sigma)?".
These are not data questions, so no SQL runs; the answer is generated from a
prompt whose skill catalog is built from the ``SKILLS`` registry (so it never
drifts from the skills that actually exist).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage

logger = logging.getLogger(__name__)

# One natural-language example per skill, used only to make the catalog concrete
# in the prompt. The skill list + titles + descriptions come from the registry.
_SKILL_EXAMPLES: Dict[str, str] = {
    "anomaly_detection": "flag unusual spikes or drops in daily sales",
    "forecast": "forecast revenue for the next 6 months",
    "changepoint": "when did the trend in orders shift?",
    "seasonality": "is there a seasonal pattern in sales?",
    "correlation": "is ad spend correlated with revenue?",
    "contribution": "what drove the change in profit last quarter?",
    "clustering": "segment customers into groups",
    "driver_analysis": "what drives customer churn?",
    "regression": "what explains store revenue?",
    "classification": "predict which customers are likely to churn",
    "cohort_retention": "show retention by signup cohort",
    "experiment_test": "did variant B beat variant A?",
}


def build_skill_catalog() -> str:
    """A markdown bullet per registered skill: title, description, an example.

    Reads ``SKILLS`` at call time so a newly-registered skill appears here for
    free (single source of truth).
    """
    from src.analysis.contracts import SKILLS  # noqa: PLC0415

    lines = []
    for name, spec in SKILLS.items():
        line = f"- **{spec.title}** - {spec.description}"
        example = _SKILL_EXAMPLES.get(name)
        if example:
            line += f' e.g. "{example}"'
        lines.append(line)
    return "\n".join(lines)


def _static_answer(display: str) -> str:
    """Fallback used only if the LLM call fails, so the user still gets help."""
    return (
        f"I'm Jeen Insights, an AI data analyst for {display}. Ask a data question in "
        "plain language and I'll write and run a read-only SQL query, or run a validated "
        "analytics/ML skill (anomaly detection, forecast, changepoint, seasonality, "
        "correlation, contribution, clustering, driver analysis, regression, classification, "
        "cohort retention, A/B test). Before an ML skill runs I show a confirm card where you "
        "can change parameters and the model (for example anomaly detection: auto or 3-sigma; "
        "forecast: ARIMA/ETS/theta/drift/seasonal-naive) - or just say it, e.g. \"flag anomalies in "
        "profit using 3-sigma\". A finished analysis has Edit setup in its status strip to reopen "
        "that card and re-run with a different model or other parameters."
    )


def make_capability_answer(
    llm: LangChainLlmService,
    prompt_loader: PromptLoader,
    *,
    fallback: Optional[Callable[[str], str]] = None,
):
    """Return an async ``capability_answer`` node that explains the assistant.

    The prompt is ``capability_answer`` from the loader — the DAX loader serves
    its own Power BI version under the same name. ``fallback`` builds the static
    answer used when the model call fails (defaults to the SQL/ML text).
    """
    static = fallback or _static_answer

    async def capability_answer(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        display = state.get("connection_display_name") or "this database"

        prompt = await prompt_loader.arender(
            "capability_answer",
            question=question,
            connection_display_name=display,
            skill_catalog=build_skill_catalog(),
        )
        model_override = await prompt_loader.model_override_for("capability_answer")

        t0 = time.monotonic()
        try:
            response = await llm.generate(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": question or "What can you do?"},
                ],
                temperature=0.3,
                max_tokens=600,
                model_override=model_override,
                timeout=state.get("llm_timeout_seconds"),
            )
            answer = (response.get("content") or "").strip() or static(display)
            usage = response.get("usage") or {}
        except Exception:  # noqa: BLE001 — a help answer must never hard-fail the turn
            logger.exception("capability_answer: LLM call failed; using static answer")
            answer, usage = static(display), {}

        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "answer": answer,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "capability_answer": prompt},
        }

    return capability_answer
