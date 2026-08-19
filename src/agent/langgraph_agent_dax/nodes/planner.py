"""dax_query_planner node — the mandatory typed plan before any DAX is written.

This is the single biggest, deliberate divergence from the SQL pipeline. DAX
correctness is dominated by choosing the right measure, grain, relationship path
and date role, so those are made first-class in a typed plan. Semantic retries
can then change ONE decision (e.g. the measure or the date role) instead of
rewriting a whole query, and genuine business ambiguity is surfaced as a
clarification here — before it ever consumes an execution/repair retry.

On JSON-parse failure the node fails *open* with a minimal aggregate plan so a
malformed planner response never dead-ends the flow (the generator + engine
still validate the actual DAX).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage

logger = logging.getLogger(__name__)

_PLANNER_HISTORY_TURNS = 3


def _recent_history(history: Any) -> str:
    if not history:
        return "No prior conversation."
    lines: List[str] = []
    for qa in list(history)[-_PLANNER_HISTORY_TURNS:]:
        q = (qa.get("natural_language_query") or "").strip()
        dax = (qa.get("generated_sql") or "").strip()  # DAX stored in this field
        if not q:
            continue
        line = f"Q: {q}"
        if dax:
            line += f"\nDAX: {dax}"
        lines.append(line)
    return "\n".join(lines) or "No prior conversation."


def _extract_json(content: str) -> str:
    text = (content or "").strip()
    for fence in ("```json", "```"):
        idx = text.find(fence)
        if idx != -1:
            after = text[idx + len(fence):]
            close = after.find("```")
            if close != -1:
                text = after[:close].strip()
                break
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    return text


def make_dax_query_planner(llm: LangChainLlmService, prompt_loader: DaxPromptLoader):
    """Return an async ``dax_query_planner`` node."""

    async def dax_query_planner(state: DaxAgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        bundle = state.get("metadata_bundle") or {}
        # Re-render measures/columns exactly as the prompt builder would so the
        # planner and generator reason over the same catalog view.
        from src.agent.langgraph_agent_dax.nodes.catalog import (
            _format_block,
            _measure_lines,
            _parse_dax_columns,
        )

        columns_text = bundle.get("columns", "")
        measures_block = _format_block(_measure_lines(columns_text)) or "No curated measures."
        (_tc, _kc, _km, _mh, column_lines) = _parse_dax_columns(columns_text)
        columns_block = _format_block(column_lines) or "No columns."

        date_table = state.get("date_table") or "none"

        prompt = await prompt_loader.arender(
            "dax_planner",
            question=question,
            connection_display_name=state.get("connection_display_name", ""),
            measures=measures_block,
            columns=columns_block,
            tables=bundle.get("tables", ""),
            relationships=bundle.get("relationships", ""),
            date_table=date_table,
            business_terms=bundle.get("business_terms", ""),
            knowledge_pairs=bundle.get("knowledge_pairs", ""),
            conversation_summary=_recent_history(state.get("conversation_history")),
        )
        model_override = await prompt_loader.model_override_for("dax_planner")

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Produce the JSON query plan."},
            ],
            temperature=0.0,
            max_tokens=900,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        plan: Dict[str, Any]
        try:
            plan = json.loads(_extract_json(content))
            if not isinstance(plan, dict):
                raise ValueError("plan is not an object")
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("dax_query_planner: JSON parse failed (%s) — using fallback plan", exc)
            plan = {
                "grain": "aggregate",
                "dimensions": [],
                "metrics": [],
                "filters": [],
                "sort": [],
                "date_role": {},
                "relationship_paths": [],
                "row_budget": state.get("limit") or 100,
                "assumptions": ["Planner response could not be parsed; generating a best-effort query."],
                "clarification_required": False,
                "clarification": "",
            }

        clarification_required = bool(plan.get("clarification_required"))
        assumptions = [str(a) for a in (plan.get("assumptions") or []) if a]
        grain = str(plan.get("grain") or "aggregate")

        updates: Dict[str, Any] = {
            "query_plan": plan,
            "plan_grain": grain,
            "expected_grain": grain,
            "plan_assumptions": assumptions,
            "clarification_required": clarification_required,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "dax_query_planner": prompt},
        }

        if clarification_required:
            clar = str(plan.get("clarification") or "").strip() or (
                "Could you clarify which measure or time range you mean?"
            )
            updates["clarification"] = clar
            updates["answer"] = clar
            logger.info("dax_query_planner: clarification required — %s", clar[:80])
        else:
            logger.info(
                "dax_query_planner: grain=%s, %d metric(s), %d assumption(s)",
                grain, len(plan.get("metrics") or []), len(assumptions),
            )

        return updates

    return dax_query_planner


__all__ = ["make_dax_query_planner"]
