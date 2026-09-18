"""Grounded narration for ML-result turns (the insights endpoint's ML branch).

Hybrid, per the design:

  * ``findings`` and ``followups`` are built **deterministically** from the
    engine facts by :func:`src.analysis.narration.narrate` — exact numbers,
    entity-referencing follow-ups, no hallucination.
  * ``summary`` is a single **LLM** sentence grounded strictly in those facts,
    produced through the existing ``analysis_narration`` prompt (the LLM writes
    prose only; every number comes from the engine). The LLM's own
    insights/follow-ups are used only as a fallback when the deterministic
    narrator has nothing (e.g. an unregistered skill).

This keeps one source of truth (the engine facts) and adds a new skill's
tailored treatment with one small narrator function, not a bespoke prompt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.analysis.narration import narrate

logger = logging.getLogger(__name__)

_PROMPT_NAME = "analysis_narration"
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_SYSTEM_MESSAGE = (
    "You are a senior data analyst. Respond with valid JSON only - no markdown, "
    "no prose before or after the JSON object."
)
# The summary is short; the prompt also asks for insights/follow-ups we keep as
# a fallback, so leave headroom for the full JSON object.
_MAX_TOKENS = 600


def _disk_template() -> Optional[str]:
    try:
        return (_PROMPTS_DIR / f"{_PROMPT_NAME}.md").read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    for fence in ("```json", "```"):
        idx = text.find(fence)
        if idx != -1:
            after = text[idx + len(fence):]
            close = after.find("```")
            if close != -1:
                text = after[:close].strip()
                break
    start, end = text.find("{"), text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _grounded_summary(
    analysis: Dict[str, Any],
    *,
    question: str,
    row_count: int,
    llm_service: Any,
    prompt_cache: Any = None,
) -> Dict[str, Any]:
    """One LLM call over the engine facts. Returns {summary, insights, followups, prompt}."""
    out: Dict[str, Any] = {"summary": "", "insights": [], "followups": [], "prompt": ""}
    if llm_service is None:
        return out

    template = None
    model_override = None
    if prompt_cache is not None:
        try:
            template = await prompt_cache.get_content(_PROMPT_NAME)
            model_override = await prompt_cache.get_model_override(_PROMPT_NAME)
        except Exception:  # noqa: BLE001
            template = None
    if not template:
        template = _disk_template()
    if not template:
        return out

    # Reuse the inline eval node's fact-shaping so both narration paths agree.
    from src.agent.langgraph_agent.nodes.eval import _narration_prompt_inputs  # noqa: PLC0415

    try:
        inputs = {"question": question or "", "row_count": row_count or 0,
                  **_narration_prompt_inputs(analysis or {})}
        prompt_text = template.format(**inputs)
    except Exception:  # noqa: BLE001 — a template/format mismatch must not break the answer
        logger.debug("ml insights: prompt render failed", exc_info=True)
        return out
    out["prompt"] = prompt_text

    try:
        response = await llm_service.generate(
            messages=[
                {"role": "system", "content": _SYSTEM_MESSAGE},
                {"role": "user", "content": prompt_text},
            ],
            temperature=0.2,
            max_tokens=_MAX_TOKENS,
            model_override=model_override,
        )
    except Exception:  # noqa: BLE001
        logger.exception("ml insights: grounded summary LLM call failed")
        return out

    parsed = _parse_json(response.get("content") or "")
    # summary may be a plain string OR a fragment array — preserve as-is.
    out["summary"] = parsed.get("summary", "")
    if isinstance(parsed.get("insights"), list):
        out["insights"] = [i for i in parsed["insights"] if i]
    fq = parsed.get("follow_up_questions")
    if isinstance(fq, list):
        out["followups"] = [str(q) for q in fq if q]
    return out


async def generate_ml_insights(
    *,
    analysis: Dict[str, Any],
    question: str,
    row_count: int = 0,
    llm_service: Any = None,
    prompt_cache: Any = None,
) -> Dict[str, Any]:
    """Findings + follow-ups (deterministic) and a grounded LLM summary for an ML turn.

    ``analysis`` is the serialized ``ResultEnvelope`` (``skill``/``facts``/
    ``params``/``caveats``/``headline``/``validation``). Returns the same shape
    the insights endpoints already emit: ``{summary, findings, suggestions,
    followups, prompt}``.
    """
    nar = narrate(analysis or {})
    # The narration prompt's row_count is "rows in the result table"; for an ML
    # turn that is an engine fact, not the caller-supplied dataset. Fall back to
    # the caller's count only when the facts carry no size.
    facts = (analysis or {}).get("facts") or {}
    egress = (analysis or {}).get("egress") or {}
    effective_rows = (
        facts.get("n_points") or facts.get("n_rows") or facts.get("n_entities")
        or egress.get("rows_sent_to_model") or row_count or 0
    )
    llm = await _grounded_summary(
        analysis or {}, question=question, row_count=effective_rows,
        llm_service=llm_service, prompt_cache=prompt_cache,
    )

    # Deterministic output is authoritative; the LLM only fills genuine gaps.
    findings = nar.findings or llm.get("insights") or []
    followups = nar.followups or llm.get("followups") or []
    summary = llm.get("summary") or (analysis or {}).get("headline") or "Analysis complete"

    return {
        "summary": summary,
        "findings": findings,
        "suggestions": [],
        "followups": followups,
        "prompt": llm.get("prompt") or "",
    }
