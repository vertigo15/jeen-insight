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

# One short "use when …" clause per skill — the guidance the user asked for
# ("when to use them"). Keyed by skill name; kept in lockstep with the registry
# (a test asserts the key set equals ``SKILLS``).
_SKILL_WHEN: Dict[str, str] = {
    "anomaly_detection": "a metric may have unusual spikes or drops you want flagged",
    "forecast": "you want to project a measure into the future",
    "changepoint": "you need to know when a trend shifted",
    "seasonality": "you want to find recurring cycles or peak periods",
    "correlation": "you want to see whether two measures move together (including at a lag)",
    "contribution": "you need to explain what drove a change between two periods",
    "clustering": "you want to segment entities into natural groups",
    "driver_analysis": "you want to rank what predicts a target",
    "regression": "you want a quantified, interpretable effect of features on a numeric outcome",
    "classification": "you want to predict a yes/no outcome and see its drivers",
    "cohort_retention": "you want to measure retention by signup cohort over time",
    "experiment_test": "you want to compare two A/B arms for a significant difference",
}

# The algorithm behind each skill that has no user-selectable ``method`` field.
# Skills that DO expose a method (forecast, anomaly_detection, clustering,
# driver_analysis) derive their algorithm list from ``method_options`` +
# ``METHOD_LABELS`` instead, so those never drift from the contract.
_SKILL_ALGORITHM: Dict[str, str] = {
    "changepoint": "PELT (ruptures) on the de-seasonalised trend",
    "seasonality": "MSTL / STL decomposition",
    "correlation": "Pearson correlation across lags + Granger (scipy)",
    "contribution": "arithmetic delta decomposition (Adtributor-style)",
    "regression": "OLS linear regression (statsmodels)",
    "classification": "logistic regression (statsmodels Logit)",
    "cohort_retention": "SQL aggregation (no model)",
    "experiment_test": "two-proportion z-test / Welch's t-test (scipy)",
}

# ``family`` is a data-shape concept; these labels are the category we present it
# as (it happens to align with how the skills group for a user).
_CATEGORY_BY_FAMILY: Dict[str, str] = {
    "series": "Time series",
    "contribution": "Change decomposition",
    "entity": "Entity / row-level",
    "cohort": "Cohort",
    "experiment": "Experiment (A/B)",
}
_CATEGORY_ORDER = ["Time series", "Change decomposition", "Cohort", "Experiment (A/B)", "Entity / row-level"]
# Egress tier, shown so the user knows what leaves the database per category.
_TIER_NOTE = {"A": "aggregates only", "B": "row-level, capped"}


def _skill_algorithm(name: str) -> str:
    """The algorithm(s) behind a skill.

    For a skill with a selectable ``method``, the labels come straight from the
    contract (``method_options`` + ``METHOD_LABELS``) so they never drift; for a
    method-less skill they come from ``_SKILL_ALGORITHM``.
    """
    from src.analysis.contracts import METHOD_LABELS, method_options  # noqa: PLC0415

    methods = method_options(name)
    if methods:
        return " / ".join(METHOD_LABELS.get(m, m) for m in methods)
    return _SKILL_ALGORITHM.get(name, "")


def build_skill_catalog() -> str:
    """A markdown catalog grouped by category, one bullet per registered skill
    with its algorithm and when to use it.

    Reads ``SKILLS`` at call time so a newly-registered skill appears here for
    free (single source of truth), and derives selectable methods from the
    contract so the algorithm list cannot drift.
    """
    from src.analysis.contracts import SKILLS  # noqa: PLC0415

    groups: Dict[str, list] = {}
    tier_of: Dict[str, str] = {}
    for name, spec in SKILLS.items():
        category = _CATEGORY_BY_FAMILY.get(spec.family, "Other")
        tier_of.setdefault(category, spec.tier)
        when = _SKILL_WHEN.get(name, spec.description)
        line = f"- **{spec.title}** — algorithm: {_skill_algorithm(name)}. Use when {when}"
        example = _SKILL_EXAMPLES.get(name)
        if example:
            line += f' e.g. "{example}"'
        groups.setdefault(category, []).append(line)

    ordered = [c for c in _CATEGORY_ORDER if c in groups]
    ordered += [c for c in groups if c not in ordered]
    blocks = []
    for category in ordered:
        note = _TIER_NOTE.get(tier_of.get(category, ""), "")
        header = f"**{category}**" + (f" ({note})" if note else "")
        blocks.append(header + "\n" + "\n".join(groups[category]))
    return "\n\n".join(blocks)


def _static_answer(display: str) -> str:
    """Fallback used only if the LLM call fails, so the user still gets help.

    Built from ``build_skill_catalog`` so the enriched, grouped list (algorithm +
    when to use) is shown even when the model call fails.
    """
    return (
        f"I'm Jeen Insights, an AI data analyst for {display}. Ask a data question in "
        "plain language and I'll write and run a read-only SQL query, or run a validated "
        "analytics/ML skill. The skills, by category, with the algorithm behind each and "
        "when to use it:\n\n"
        f"{build_skill_catalog()}\n\n"
        "Before an ML skill runs I show a confirm card where you can change parameters and the "
        "model (for example anomaly detection: auto/seasonal/trend/3-sigma; forecast: "
        "ARIMA/ETS/theta/drift/seasonal-naive) - or just say it, e.g. \"flag anomalies in "
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
