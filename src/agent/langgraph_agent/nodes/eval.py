"""fused_eval_analytics node — evaluates results and generates summary + insights.

Two public factories:

``make_fused_eval_analytics(llm, prompt_loader)``
    For the full text-to-SQL pipeline (``build_graph``).  Reads from
    ``AgentState`` and writes back ``eval_result`` containing
    ``{answers_intent, summary, insights, follow_up_questions}``.

``make_fused_eval_analytics_subgraph(llm_service, prompt_cache)``
    Standalone variant called directly by the insights API endpoint
    (``build_insights_eval_graph``).  Uses ``PromptCache`` (DB-backed)
    and ``InsightsState`` instead of ``AgentState``.

``answers_intent=False`` triggers the feedback_classifier to attempt a retry.
On JSON parse failure the node defaults to ``answers_intent=True`` so it never
blocks a valid result.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService

logger = logging.getLogger(__name__)


def _merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


_SAMPLE_ROWS = 12


def _build_results_block(statistics: str, rows: List[Any], row_count: int) -> str:
    """Compose the data context the eval LLM sees: full-data statistics (when
    available) plus a small verbatim row sample. The statistics carry the
    whole-dataset signal so the model never has to infer totals from the sample."""
    sample = rows[:_SAMPLE_ROWS] if isinstance(rows, list) else []
    try:
        sample_json = json.dumps(sample, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        sample_json = str(sample)[:2000]

    parts: List[str] = []
    if statistics:
        parts.append(
            f"Full-data statistics (computed over all {row_count} rows):\n{statistics}"
        )
    parts.append(f"Sample rows (first {len(sample)} of {row_count}):\n{sample_json}")
    return "\n\n".join(parts)


def _profile_statistics(rows: List[Any], columns: List[str]) -> str:
    """Best-effort full-data statistics for the inline pipeline node, where we
    have the rows but no precomputed statistics."""
    try:
        from src.api.chart_builder import profile_dataset, summarize_profile

        if not columns and rows and isinstance(rows[0], dict):
            columns = list(rows[0].keys())
        return summarize_profile(profile_dataset({"columns": columns, "rows": rows}, scan_cap=100_000))
    except Exception:  # noqa: BLE001
        logger.debug("fused_eval: statistics computation failed", exc_info=True)
        return ""


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


def make_fused_eval_analytics(llm: LangChainLlmService, prompt_loader: PromptLoader):
    """Return an async ``fused_eval_analytics`` node."""

    async def fused_eval_analytics(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        sql = state.get("generated_sql") or ""
        result = state.get("query_result") or {}
        rows = result.get("rows") or []
        row_count = len(rows)

        # Full-data statistics + small sample (so the model reasons over ALL rows,
        # not just the first few).
        statistics = _profile_statistics(rows, result.get("columns") or [])
        results_sample = _build_results_block(statistics, rows, row_count)

        prompt = await prompt_loader.arender(
            "fused_eval_analytics",
            question=question,
            sql=sql,
            results_sample=results_sample,
            row_count=row_count,
        )
        model_override = await prompt_loader.model_override_for("fused_eval_analytics")

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Evaluate the results and respond with JSON."},
            ],
            temperature=0.2,
            max_tokens=600,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        # Default — never block on parse failure
        eval_result: Dict[str, Any] = {
            "answers_intent": True,
            "summary": "",
            "insights": [],
            "follow_up_questions": [],
        }

        try:
            parsed = json.loads(_extract_json(content))
            # Normalise follow-up: new format (list) or legacy (single string)
            fq = parsed.get("follow_up_questions")
            if isinstance(fq, list):
                follow_ups: List[str] = [str(q) for q in fq if q]
            elif parsed.get("follow_up"):
                follow_ups = [str(parsed["follow_up"])]
            else:
                follow_ups = []
            eval_result.update(
                {
                    "answers_intent": bool(parsed.get("answers_intent", True)),
                    # Keep summary as-is: may be a plain string OR a fragment array
                    # [{"t": "...", "hl": "accent|pos|neg|num"}, …].  Calling str()
                    # would turn a list into a Python repr string with single quotes,
                    # which is not valid JSON and breaks the JS renderText() renderer.
                    "summary": parsed.get("summary", ""),
                    "insights": list(parsed.get("insights", [])),
                    "follow_up_questions": follow_ups,
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
            "node_prompts": {**(state.get("node_prompts") or {}), "fused_eval_analytics": prompt},
        }

    return fused_eval_analytics


# ── Standalone subgraph variant ────────────────────────────────────────────────
# Used by ``build_insights_eval_graph`` when the insights API endpoint calls the
# eval node directly (outside the full pipeline).  Uses PromptCache (DB-backed)
# instead of PromptLoader and InsightsState instead of AgentState.

_SYSTEM_MESSAGE = (
    "You are a senior data analyst. "
    "Respond with valid JSON only — no markdown, no prose before or after "
    "the JSON object."
)


def make_fused_eval_analytics_subgraph(
    llm_service: Any,
    prompt_cache: Any,
) -> Callable:
    """Return an async LangGraph node for the standalone insights eval subgraph."""

    async def _node(state: dict) -> dict:
        question   = state.get("question", "")
        sql        = state.get("sql", "")
        results    = state.get("results") or []
        row_count  = state.get("row_count", len(results) if isinstance(results, list) else 0)
        statistics = state.get("statistics") or ""

        try:
            template       = await prompt_cache.get_content("fused_eval_analytics")
            model_override = await prompt_cache.get_model_override("fused_eval_analytics")
        except (KeyError, Exception):  # noqa: BLE001
            template       = _FALLBACK_PROMPT
            model_override = None

        # Data context = full-data statistics (the model's window onto ALL rows)
        # + a small verbatim sample for shape. Folded into results_sample so it
        # rides the existing prompt placeholder regardless of the DB template.
        results_block = _build_results_block(statistics, results, row_count)

        try:
            prompt_text = template.format(
                question       = question,
                sql            = sql or "N/A",
                results_sample = results_block,
                row_count      = row_count,
            )
        except KeyError:
            prompt_text = template

        from src.api.llm_params import QUERY_PARAMS
        try:
            response = await llm_service.generate(
                messages=[
                    {"role": "system", "content": _SYSTEM_MESSAGE},
                    {"role": "user",   "content": prompt_text},
                ],
                temperature    = 0.2,
                max_tokens     = QUERY_PARAMS.max_tokens,
                model_override = model_override,
            )
            raw = (response.get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.exception("eval_subgraph: LLM call failed")
            return {
                "answers_intent":      True,
                "summary":             "",
                "insights":            [],
                "follow_up_questions": [],
                "error":               str(exc),
            }

        parsed = _parse_response(raw)
        fq = parsed.get("follow_up_questions")
        if isinstance(fq, list):
            follow_ups = [str(q) for q in fq if q]
        elif parsed.get("follow_up"):
            follow_ups = [str(parsed["follow_up"])]
        else:
            follow_ups = []

        sug = parsed.get("suggestions")
        action_suggestions = [str(s) for s in sug if s] if isinstance(sug, list) else []

        return {
            "answers_intent":      bool(parsed.get("answers_intent", True)),
            # Preserve list (fragment array) or plain string — do NOT coerce to str().
            "summary":             parsed.get("summary", ""),
            "insights":            list(parsed.get("insights") or []),
            "suggestions":         action_suggestions,
            "follow_up_questions": follow_ups,
            "error":               None,
            "prompt_text":         prompt_text,   # exposed for the developer panel
        }

    return _node


def _parse_response(text: str) -> dict:
    text = text.strip()
    for fence in ("```json", "```"):
        idx = text.find(fence)
        if idx != -1:
            after = text[idx + len(fence):]
            close = after.find("```")
            if close != -1:
                text = after[:close].strip()
                break
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        logger.debug("eval_subgraph: JSON decode failed for: %.200s", text)
        return {}


_FALLBACK_PROMPT = """\
You are a senior data analyst reviewing a query result.

**Original question:** {question}

**SQL executed:**
```sql
{sql}
```

**Row count:** {row_count}

**Sample results (first 5 rows):**
```json
{results_sample}
```

Tasks:
1. Evaluate whether the result genuinely answers the original question.
2. Summarize what the data shows in 1-2 sentences for a business user.
3. Extract 2-3 key insights with specific numbers from the data.
4. Generate 3-5 short follow-up questions the user might want to ask next.

Rules:
- `answers_intent` is false only when results are empty or clearly wrong.
- `summary` must be ≤ 60 words.
- Each `insights` item must be ≤ 30 words and include a specific number.
- Each `follow_up_questions` item must be ≤ 15 words and end with "?".
- Match the language of the original question.

Respond with valid JSON only.

{{
  "answers_intent": true,
  "summary": "...",
  "insights": ["...", "..."],
  "follow_up_questions": ["...?", "...?", "...?"]
}}"""
