"""dax_generator node — turns the plan + question into a single DAX query.

Mirrors the SQL ``sql_generator`` (structured tool-call output, error context on
retry) but emits DAX via the ``run_dax`` tool and follows the typed plan produced
upstream. The generated DAX is stored in BOTH ``generated_dax`` (DAX-native) and
``generated_sql`` (so the shared post-data nodes — eval, memory, formatter — read
it unchanged and surface it in the UI's "query" slot).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader
from src.agent.langgraph_agent_dax.state import DaxAgentState
from src.agent.llm_service import LangChainLlmService
from src.tools.dax_tool import RunDaxTool

logger = logging.getLogger(__name__)


def _merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


def _extract_dax(response: Dict[str, Any]) -> Optional[str]:
    """Extract DAX from a ``run_dax`` tool call, a fenced block, or a bare query."""
    for tc in response.get("tool_calls") or []:
        if tc.get("function", {}).get("name") == "run_dax":
            try:
                args = json.loads(tc["function"]["arguments"])
            except (KeyError, json.JSONDecodeError):
                continue
            dax = (args.get("dax") or "").strip()
            if dax:
                return dax

    text = response.get("content") or ""
    lower = text.lower()

    # Fenced ```dax block.
    if "```dax" in lower:
        start = lower.find("```dax") + len("```dax")
        end = text.find("```", start)
        if end > start:
            return text[start:end].strip()
    # Generic fenced block that starts with EVALUATE/DEFINE.
    if "```" in text:
        start = text.find("```") + 3
        # skip an optional language tag on the same line
        nl = text.find("\n", start)
        body_start = nl + 1 if nl != -1 else start
        end = text.find("```", body_start)
        if end > body_start:
            candidate = text[body_start:end].strip()
            up = candidate.upper()
            if up.startswith("EVALUATE") or up.startswith("DEFINE"):
                return candidate

    # Bare EVALUATE / DEFINE statement in the content.
    up = text.upper()
    for kw in ("DEFINE", "EVALUATE"):
        idx = up.find(kw)
        if idx != -1:
            candidate = text[idx:].strip()
            if candidate:
                return candidate
    return None


def make_dax_generator(llm: LangChainLlmService, prompt_loader: DaxPromptLoader):
    """Return an async ``dax_generator`` node."""

    async def dax_generator(state: DaxAgentState) -> Dict[str, Any]:
        from src.api.llm_params import QUERY_PARAMS

        question = state.get("question", "")
        system_prompt = state.get("system_prompt", "")
        history = state.get("conversation_history") or []
        retry_count = state.get("retry_count") or 0
        error_context = state.get("error_context")
        display_name = state.get("connection_display_name", "")
        dataset_id = state.get("dataset_id") or ""
        workspace_id = state.get("workspace_id") or ""
        temperature = state.get("temperature")
        plan = state.get("query_plan")
        plan_text = json.dumps(plan, indent=2, default=str) if plan else "No plan."

        dax_tool = RunDaxTool(
            display_name, dataset_id=dataset_id, workspace_id=workspace_id
        )
        tools = [dax_tool.get_schema()]

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # Prior Q&As as run_dax tool-call / tool-result pairs (Azure requires a
        # tool result for every assistant tool_call).
        for i, qa in enumerate(history):
            if qa.get("natural_language_query") and qa.get("generated_sql"):
                call_id = f"prev_dax_call_{i}"
                messages.append({"role": "user", "content": qa["natural_language_query"]})
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "run_dax",
                                    "arguments": json.dumps({"dax": qa["generated_sql"]}),
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": "Query executed successfully.",
                    }
                )

        if retry_count > 0 and error_context:
            user_msg = await prompt_loader.arender(
                "dax_generator",
                question=question,
                error_context=error_context,
                retry_count=retry_count,
                connection_display_name=display_name,
                dataset_id=dataset_id or "not specified",
                plan=plan_text,
            )
        else:
            user_msg = question
        messages.append({"role": "user", "content": user_msg})

        effective_temperature = (
            temperature if temperature is not None else QUERY_PARAMS.temperature
        )
        model_override = await prompt_loader.model_override_for("jeen_insights_system_dax")

        t0 = time.monotonic()
        response = await llm.generate(
            messages=messages,
            temperature=effective_temperature,
            max_tokens=QUERY_PARAMS.max_tokens,
            tools=tools,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        dax = _extract_dax(response)
        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        updates: Dict[str, Any] = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": _merge_usage(state.get("token_usage") or {}, usage),
            # reset downstream validation/execution state for this attempt
            "dax_lint_errors": [],
            "dax_validation_error": None,
            "dax_repairable_error": None,
            "exec_error": None,
            "dlp_blocked": False,
            "governance_error": None,
            "query_result": None,
            "is_trivial": False,
            "eval_result": None,
            "is_partial": False,
            "is_empty": False,
            "pbi_error_code": None,
            "pbi_error_message": None,
            "http_status": None,
        }

        if dax:
            updates["generated_dax"] = dax
            updates["generated_sql"] = dax  # shared nodes read generated_sql
            updates["clarification"] = None
            updates["error_context"] = None
            # Track hashes so repeated identical DAX can be detected by the router.
            hashes = list(state.get("previous_dax_hashes") or [])
            hashes.append(str(hash(dax)))
            updates["previous_dax_hashes"] = hashes
            logger.info("dax_generator: DAX extracted (len=%d, retry=%d)", len(dax), retry_count)
        elif content:
            updates["clarification"] = content
            updates["generated_dax"] = None
            updates["generated_sql"] = None
            logger.info("dax_generator: clarification returned (retry=%d)", retry_count)
        else:
            updates["clarification"] = (
                "I was unable to generate a DAX query. Please rephrase your question."
            )
            updates["generated_dax"] = None
            updates["generated_sql"] = None
            logger.warning("dax_generator: empty response (retry=%d)", retry_count)

        updates["node_prompts"] = {
            **(state.get("node_prompts") or {}),
            "dax_generator": user_msg,
        }
        return updates

    return dax_generator


__all__ = ["make_dax_generator", "_extract_dax"]
