"""fused_eval_analytics node — evaluates results and generates summary + insights.

A single large-model call that:
  • checks whether the result set genuinely answers the user's intent,
  • produces a human-readable summary,
  • extracts 2-3 key insights,
  • suggests an optional follow-up question.

``answers_intent=False`` triggers the feedback_classifier to attempt a retry.
On JSON parse failure the node defaults to ``answers_intent=True`` so it never
blocks a valid result.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import AzureOpenAILlmService

logger = logging.getLogger(__name__)


def _merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


def _extract_json(content: str) -> str:
    """Strip markdown code fences to get the raw JSON string."""
    if "```json" in content:
        start = content.find("```json") + 7
        end = content.find("```", start)
        return content[start:end].strip() if end > start else content
    if "```" in content:
        start = content.find("```") + 3
        end = content.find("```", start)
        return content[start:end].strip() if end > start else content
    return content


def make_fused_eval_analytics(llm: AzureOpenAILlmService, prompt_loader: PromptLoader):
    """Return an async ``fused_eval_analytics`` node."""

    async def fused_eval_analytics(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        sql = state.get("generated_sql") or ""
        result = state.get("query_result") or {}
        rows = result.get("rows") or []
        row_count = len(rows)

        # Serialise a small sample for the prompt (avoid huge payloads)
        sample_rows = rows[:5]
        try:
            results_sample = json.dumps(sample_rows, default=str, indent=2)
        except Exception:  # noqa: BLE001
            results_sample = str(sample_rows)[:1000]

        prompt = prompt_loader.render(
            "fused_eval_analytics",
            question=question,
            sql=sql,
            results_sample=results_sample,
            row_count=row_count,
        )

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Evaluate the results and respond with JSON."},
            ],
            temperature=0.2,
            max_tokens=600,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        # Default — never block on parse failure
        eval_result: Dict[str, Any] = {
            "answers_intent": True,
            "summary": "",
            "insights": [],
            "follow_up": "",
        }

        try:
            parsed = json.loads(_extract_json(content))
            eval_result.update(
                {
                    "answers_intent": bool(parsed.get("answers_intent", True)),
                    "summary": str(parsed.get("summary", "")),
                    "insights": list(parsed.get("insights", [])),
                    "follow_up": str(parsed.get("follow_up", "")),
                }
            )
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.warning(
                "fused_eval_analytics: JSON parse failed (%s) — defaulting to answers_intent=True",
                exc,
            )
            eval_result["summary"] = content[:500] if content else ""

        logger.info(
            "fused_eval_analytics: answers_intent=%s, insights=%d, latency=%dms",
            eval_result["answers_intent"],
            len(eval_result.get("insights") or []),
            latency_ms,
        )

        return {
            "eval_result": eval_result,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": _merge_usage(state.get("token_usage") or {}, usage),
        }

    return fused_eval_analytics
