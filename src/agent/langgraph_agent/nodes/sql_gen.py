"""SQL generation node.

sql_generator   Large-model node.  Generates SQL via function-calling or requests
                clarification.  Prior turns are replayed from the memory ledger
                as tool-call pairs; on retries the structured error context from
                the previous attempt is injected.

The memory-answer node lives in ``nodes/memory_answer.py``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.agent.langgraph_agent.nodes.context import ledger_for, render_turn_tool_result
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.snapshot_sql import is_memory_sql
from src.agent.token_usage import merge_usage
from src.tools.sql_tool import RunSqlTool

logger = logging.getLogger(__name__)


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
        retry_count = state.get("retry_count") or 0
        error_context = state.get("error_context")
        display_name = state.get("connection_display_name", "")
        db_type = state.get("database_type", "")
        source_key = state.get("source_key", "")
        database = state.get("connection_database") or ""
        catalog = state.get("connection_catalog") or ""
        schema = state.get("connection_schema") or ""
        temperature = state.get("temperature")

        # Build tool schema — RunSqlTool with None runner (schema only, no execution here)
        sql_tool = RunSqlTool(
            None,  # type: ignore[arg-type]
            connection_display_name=display_name,
            database_type=db_type,
            source_key=source_key,
            catalog=catalog,
            schema=schema,
        )
        tools = [sql_tool.get_schema()]

        # Build the message list
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # Prior turns as proper tool-call / tool-result pairs, from the ledger.
        # Azure OpenAI requires every assistant message with tool_calls to be
        # immediately followed by a tool message for each tool_call_id. The tool
        # result carries what the query returned and what was answered, so the
        # model can build on prior turns ("same as T3 but for 2024").
        for i, turn in enumerate(ledger_for(state)):
            if not turn.get("sql"):
                continue
            if is_memory_sql(turn["sql"]):
                # Computed over stored rows in the metadata DB, not runnable at the source:
                # keep the question/answer in context without a run_sql call.
                messages.append({"role": "user", "content": f"[{turn['handle']}] {turn['question']}"})
                messages.append({"role": "assistant", "content": render_turn_tool_result(turn)})
                continue
            call_id = f"prev_call_{i}"  # unique per turn
            messages.append({"role": "user", "content": f"[{turn['handle']}] {turn['question']}"})
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
                                "arguments": json.dumps({"sql": turn["sql"]}),
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
                    "content": render_turn_tool_result(turn),
                }
            )

        # Current question — inject error context on retries
        if retry_count > 0 and error_context:
            user_msg = await prompt_loader.arender(
                "sql_generator",
                question=question,
                error_context=error_context,
                retry_count=retry_count,
                connection_display_name=display_name,
                source_key=source_key,
                database_type=db_type,
                connection_database=database or "not specified",
                connection_catalog=catalog or "not specified",
                connection_schema=schema or "not specified",
            )
        else:
            user_msg = question

        messages.append({"role": "user", "content": user_msg})

        effective_temperature = (
            temperature if temperature is not None else QUERY_PARAMS.temperature
        )

        # Honour a per-prompt model override assigned to the system prompt
        # (the SQL generator's primary prompt place).
        model_override = await prompt_loader.model_override_for("jeen_insights_system")

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

        sql = _extract_sql(response)
        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}

        # Base update — always reset previous validation / execution state
        updates: Dict[str, Any] = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
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
