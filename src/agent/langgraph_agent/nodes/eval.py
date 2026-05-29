"""LangGraph node: fused evaluation + analytics  (nodes/eval.py).

make_fused_eval_analytics(llm_service, prompt_cache)
    Factory that returns an *async* node function ready to be registered
    with a LangGraph ``StateGraph``.  The node:

    1. Loads the ``fused_eval_analytics`` prompt template from PromptCache
       (falls back to the in-module ``_FALLBACK_PROMPT`` if the DB row is
       missing, e.g. during local dev without a seeded DB).
    2. Renders the template with ``{question}``, ``{sql}``,
       ``{results_sample}``, ``{row_count}``.
    3. Calls the LLM and parses the JSON response.
    4. Returns a partial state dict with:
         - ``answers_intent``      – bool
         - ``summary``             – str
         - ``insights``            – List[str]
         - ``follow_up_questions`` – List[str]  ← the key new field
         - ``error``               – Optional[str]

    The function also handles the legacy ``follow_up`` (single string)
    format so the node works against an older prompt row in the DB without
    requiring a manual DB update.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, List

logger = logging.getLogger(__name__)

# System message keeps the LLM focused on JSON-only output.
_SYSTEM_MESSAGE = (
    "You are a senior data analyst. "
    "Respond with valid JSON only — no markdown, no prose before or after "
    "the JSON object."
)


# ── Public factory ─────────────────────────────────────────────────────────────

def make_fused_eval_analytics(
    llm_service: Any,
    prompt_cache: Any,
) -> Callable:
    """Return an async LangGraph node that evaluates query results.

    Parameters
    ----------
    llm_service:
        A ``LangChainLlmService`` instance (or any object with an async
        ``.generate(messages, temperature, max_tokens, model_override)``
        method).
    prompt_cache:
        A ``PromptCache`` instance used to resolve the
        ``fused_eval_analytics`` prompt template and its optional model
        override.
    """

    async def _node(state: dict) -> dict:
        question  = state.get("question", "")
        sql       = state.get("sql", "")
        results   = state.get("results") or []
        row_count = state.get("row_count", len(results) if isinstance(results, list) else 0)

        # ── 1. Load prompt template + optional per-prompt model override ──
        try:
            template       = await prompt_cache.get_content("fused_eval_analytics")
            model_override = await prompt_cache.get_model_override("fused_eval_analytics")
        except (KeyError, Exception):  # noqa: BLE001
            logger.warning(
                "eval_node: fused_eval_analytics prompt not found in cache; "
                "using built-in fallback"
            )
            template       = _FALLBACK_PROMPT
            model_override = None

        # ── 2. Render template ─────────────────────────────────────────────
        sample = results[:5] if isinstance(results, list) else []
        try:
            sample_json = json.dumps(sample, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            sample_json = str(sample)

        try:
            prompt_text = template.format(
                question       = question,
                sql            = sql or "N/A",
                results_sample = sample_json,
                row_count      = row_count,
            )
        except KeyError as exc:
            # Unknown placeholder in a custom prompt — use the template as-is.
            logger.error(
                "eval_node: unknown placeholder %s in fused_eval_analytics prompt; "
                "rendering raw template",
                exc,
            )
            prompt_text = template

        # ── 3. Call LLM ───────────────────────────────────────────────────
        from src.api.llm_params import QUERY_PARAMS  # local import to avoid circulars

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
            logger.exception("eval_node: LLM call failed")
            return {
                "answers_intent":      True,
                "summary":             "",
                "insights":            [],
                "follow_up_questions": [],
                "error":               str(exc),
            }

        # ── 4. Parse JSON response ─────────────────────────────────────────
        parsed = _parse_json(raw)
        if not parsed:
            logger.warning(
                "eval_node: LLM returned unparseable content (len=%d)", len(raw)
            )

        follow_up_questions = _extract_follow_up_questions(parsed)

        return {
            "answers_intent":      bool(parsed.get("answers_intent", True)),
            "summary":             str(parsed.get("summary", "")),
            "insights":            list(parsed.get("insights") or []),
            "follow_up_questions": follow_up_questions,
            "error":               None,
        }

    return _node


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Extract and parse the first JSON object from *text*.

    Handles three common LLM output styles:
    - Bare JSON object
    - JSON wrapped in ```json … ``` fences
    - JSON wrapped in plain ``` … ``` fences
    """
    text = text.strip()

    # Strip markdown code fences
    for fence in ("```json", "```"):
        idx = text.find(fence)
        if idx != -1:
            after = text[idx + len(fence):]
            close = after.find("```")
            if close != -1:
                text = after[:close].strip()
                break

    # Find outermost `{ … }`
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]

    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        logger.debug("eval_node: JSON decode failed for: %.200s", text)
        return {}


def _extract_follow_up_questions(parsed: dict) -> List[str]:
    """Return a normalised list of follow-up questions.

    Handles:
    - ``follow_up_questions`` → List[str]   (new format)
    - ``follow_up``           → str         (legacy single-question format)
    """
    # New format: list field
    fq = parsed.get("follow_up_questions")
    if isinstance(fq, list) and fq:
        return [str(q) for q in fq if q]

    # Legacy format: single string
    legacy = parsed.get("follow_up")
    if legacy and isinstance(legacy, str) and legacy.strip():
        return [legacy.strip()]

    return []


# ── Built-in fallback prompt ───────────────────────────────────────────────────
# Used only when the DB row is absent (e.g. dev environment without a seeded DB).
# All JSON template braces are doubled ( {{ / }} ) so str.format() works correctly.

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

---

Your tasks:
1. Evaluate whether the result genuinely answers the original question.
2. Summarize what the data shows in 1-2 sentences for a business user.
3. Extract 2-3 key insights with specific numbers from the data.
4. Generate 3-5 short follow-up questions the user might want to ask next.

Rules:
- Set `answers_intent` to false ONLY when the result set is empty despite \
expecting data, or when the results clearly do not match what was asked.
- Set `answers_intent` to true for all other cases, including partial results.
- Keep `summary` under 60 words.
- Keep each `insights` item under 30 words and include a specific number.
- Each `follow_up_questions` item must be \u2264 15 words and end with "?".
- `follow_up_questions` may be an empty list if nothing natural comes to mind.
- Match the language of the original question.

Respond with valid JSON only. No text before or after the JSON object.

{{
  "answers_intent": true,
  "summary": "...",
  "insights": ["...", "...", "..."],
  "follow_up_questions": ["...?", "...?", "...?"]
}}"""
