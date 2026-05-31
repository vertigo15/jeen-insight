"""SQL generation and memory-answer nodes.

sql_generator           Large-model node.  Generates SQL via function-calling or
                        requests clarification.  On retries, injects structured
                        error context from the previous attempt.

make_memory_answer_generator
                        Small-model node.  Tries to answer from conversation history.
                        Sets route="needs_query" as an escape hatch when a live
                        query is still required.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.tools.sql_tool import RunSqlTool

logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _merge_usage(current: Dict[str, int], new: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": current.get("input_tokens", 0) + (new.get("prompt_tokens") or 0),
        "output_tokens": current.get("output_tokens", 0) + (new.get("completion_tokens") or 0),
        "total_tokens": current.get("total_tokens", 0) + (new.get("total_tokens") or 0),
    }


def _extract_sql(response: Dict[str, Any]) -> Optional[str]:
    """Extract SQL text from a tool call or from plain text content.

    Priority:
    1. ``run_sql`` tool call argument.
    2. SQL fenced code block in the content.
    3. Bare SELECT statement in the content.
    """
    # 1. Tool call path
    for tc in response.get("tool_calls") or []:
        if tc.get("function", {}).get("name") == "run_sql":
            try:
                args = json.loads(tc["function"]["arguments"])
            except (KeyError, json.JSONDecodeError):
                continue
            sql = args.get("sql", "").strip()
            if sql:
                return sql

    text = response.get("content") or ""

    # 2. Fenced code block
    lower = text.lower()
    if "```sql" in lower:
        start = lower.find("```sql") + len("```sql")
        end = text.find("```", start)
        if end > start:
            return text[start:end].strip()

    # 3. Bare SELECT
    if "SELECT" in text.upper():
        sql_lines: List[str] = []
        in_sql = False
        for line in text.splitlines():
            if "SELECT" in line.upper():
                in_sql = True
            if in_sql:
                sql_lines.append(line)
                if ";" in line:
                    break
        candidate = "\n".join(sql_lines).strip()
        if candidate:
            return candidate

    return None


# ── sql_generator ─────────────────────────────────────────────────────────────


def make_sql_generator(llm: LangChainLlmService, prompt_loader: PromptLoader):
    """Return an async ``sql_generator`` node."""

    async def sql_generator(state: AgentState) -> Dict[str, Any]:
        from src.api.llm_params import QUERY_PARAMS

        question = state.get("question", "")
        system_prompt = state.get("system_prompt", "")
        history = state.get("conversation_history") or []
        retry_count = state.get("retry_count") or 0
        error_context = state.get("error_context")
        display_name = state.get("connection_display_name", "")
        db_type = state.get("database_type", "")
        temperature = state.get("temperature")

        # Build tool schema — RunSqlTool with None runner (schema only, no execution here)
        sql_tool = RunSqlTool(
            None,  # type: ignore[arg-type]
            connection_display_name=display_name,
            database_type=db_type,
        )
        tools = [sql_tool.get_schema()]

        # Build the message list
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # Prior Q&As as proper tool-call / tool-result pairs.
        # Azure OpenAI requires every assistant message with tool_calls to be
        # immediately followed by a tool message for each tool_call_id.
        for i, qa in enumerate(history):
            if qa.get("natural_language_query") and qa.get("generated_sql"):
                call_id = f"prev_call_{i}"  # unique per turn
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
                                    "name": "run_sql",
                                    "arguments": json.dumps({"sql": qa["generated_sql"]}),
                                },
                            }
                        ],
                    }
                )
                # Required: tool result message for the tool_call above.
                # Azure 400s if this is missing.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": "Query executed successfully.",
                    }
                )

        # Current question — inject error context on retries
        if retry_count > 0 and error_context:
            user_msg = prompt_loader.render(
                "sql_generator",
                question=question,
                error_context=error_context,
                retry_count=retry_count,
            )
        else:
            user_msg = question

        messages.append({"role": "user", "content": user_msg})

        effective_temperature = (
            temperature if temperature is not None else QUERY_PARAMS.temperature
        )

        t0 = time.monotonic()
        response = await llm.generate(
            messages=messages,
            temperature=effective_temperature,
            max_tokens=QUERY_PARAMS.max_tokens,
            tools=tools,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        sql = _extract_sql(response)
        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        # Base update — always reset previous validation / execution state
        updates: Dict[str, Any] = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": _merge_usage(state.get("token_usage") or {}, usage),
            "sqlglot_error": None,
            "exec_error": None,
            "dlp_blocked": False,
            "governance_error": None,
            "query_result": None,
            "is_trivial": False,
            "eval_result": None,
        }

        if sql:
            updates["generated_sql"] = sql
            updates["clarification"] = None
            updates["error_context"] = None
            logger.info("sql_generator: SQL extracted (len=%d, retry=%d)", len(sql), retry_count)
        elif content:
            updates["clarification"] = content
            updates["generated_sql"] = None
            logger.info("sql_generator: clarification returned (retry=%d)", retry_count)
        else:
            updates["clarification"] = (
                "I was unable to generate a response. Please rephrase your question."
            )
            updates["generated_sql"] = None
            logger.warning("sql_generator: empty response (retry=%d)", retry_count)

        # Save the user-facing part of the prompt (system prompt is already in
        # structured_prompt / Query Prompt tab; save the user message here).
        updates["node_prompts"] = {
            **(state.get("node_prompts") or {}),
            "sql_generator": user_msg,
        }

        return updates

    return sql_generator


# ── memory_answer_generator ───────────────────────────────────────────────────


def make_memory_answer_generator(llm: LangChainLlmService, prompt_loader: PromptLoader):
    """Return an async ``memory_answer_generator`` node.

    If the LLM determines that a live query is still needed, it returns
    ``{"needs_query": true}`` in its response, which sets ``route="needs_query"``
    so the graph falls through to catalog_lookup.
    """

    async def memory_answer_generator(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        history = state.get("conversation_history") or []
        history_text = "\n".join(
            f"Q: {qa.get('natural_language_query', '')}\n"
            f"SQL: {qa.get('generated_sql', '')}\n"
            f"Result sample: {qa.get('result_preview', '')}"
            for qa in history
        )

        prompt = prompt_loader.render(
            "memory_answer",
            question=question,
            conversation_history=history_text or "No prior conversation.",
        )

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            max_tokens=400,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}
        route = state.get("route", "from_memory")
        answer: Optional[str] = None

        # Try to detect the "needs_query" escape hatch
        json_str = content
        if "```" in content:
            start = content.find("```") + 3
            if content[start:].startswith("json"):
                start += 4
            end = content.find("```", start)
            json_str = content[start:end].strip() if end > start else content

        try:
            parsed = json.loads(json_str)
            if parsed.get("needs_query"):
                route = "needs_query"
                logger.info("memory_answer_generator: escape hatch → needs_query")
            else:
                answer = content
        except (json.JSONDecodeError, AttributeError):
            answer = content

        logger.info(
            "memory_answer_generator: answer=%s, route=%s",
            bool(answer),
            route,
        )

        return {
            "answer": answer,
            "route": route,
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": _merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "memory_answer_generator": prompt},
        }

    return memory_answer_generator
