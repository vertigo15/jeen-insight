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
import re
import time
from typing import Any, Dict, List, Optional

from src.agent.answer_cache import answer_cache
from src.agent.langgraph_agent.nodes.artifacts import latest_result_ref
from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage
from src.tools.sql_tool import RunSqlTool

logger = logging.getLogger(__name__)

# How many cached rows to expose to the memory-answer model so it can recompute
# (sort/filter/aggregate) over already-retrieved data without a new DB query.
_RECOMPUTE_SAMPLE_ROWS = 50
_RECOMPUTE_JSON_CAP = 6000


def _cached_rows_for(user_id: Any, source_key: Any, query_id: Any) -> Optional[Dict[str, Any]]:
    """Best-effort fetch of a prior result's full rows from the result cache."""
    if not query_id:
        return None
    try:
        from src.api.result_cache import result_cache  # lazy: avoids import cycle

        return result_cache.get(user_id=user_id, connection=source_key, query_id=query_id)
    except Exception:  # noqa: BLE001
        return None


def _recompute_block(state: AgentState) -> str:
    """Build a context block of the most recent result's cached rows + stats.

    Lets the model answer follow-ups ("sort by X", "what was the max?", "how many
    over 100?") by computing locally over the already-retrieved rows instead of
    guessing from a tiny preview or issuing a new query.
    """
    ref = latest_result_ref(state.get("conversation_history") or [])
    if not ref:
        return ""
    artifact = ref.get("artifact") or {}
    cached = _cached_rows_for(state.get("user_id"), state.get("source_key"), ref.get("query_id"))
    if not cached or not cached.get("rows"):
        return ""
    rows = cached["rows"][:_RECOMPUTE_SAMPLE_ROWS]
    try:
        rows_json = json.dumps(rows, ensure_ascii=False, default=str)[:_RECOMPUTE_JSON_CAP]
    except Exception:  # noqa: BLE001
        rows_json = str(rows)[:_RECOMPUTE_JSON_CAP]
    total = artifact.get("row_count")
    stats = artifact.get("stats") or {}
    parts = [
        f'Most recent result for "{(ref.get("question") or "").strip()[:80]}" '
        f"({total} rows total, showing up to {len(rows)}):",
        f"columns: {artifact.get('columns')}",
    ]
    if stats:
        parts.append(f"full-data stats: {json.dumps(stats, default=str)[:1500]}")
    parts.append(f"rows: {rows_json}")
    # Result rows are user data → fence against prompt injection.
    return fence_untrusted("\n".join(parts), label="prior query result")


# ── Prior-result replay (identical-repeat questions) ──────────────────────────


def _q_tokens(q: Any) -> set:
    """Word-token set of a question, lowercased (for a cheap similarity signal)."""
    return set(re.findall(r"[a-z0-9]+", str(q or "").lower()))


def _similarity(a: Any, b: Any) -> float:
    """Jaccard token overlap of two questions in [0, 1]."""
    ta, tb = _q_tokens(a), _q_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _reuse_prior_result(state: AgentState, question: str) -> Optional[Dict[str, Any]]:
    """Find a prior turn to replay verbatim for a repeat question.

    Returns ``{query_id, sql, result}`` where ``result`` is ``{columns, rows,
    row_count}`` for the most similar prior turn whose full rows are still in the
    result cache. Returns ``None`` when nothing matches or the rows have been
    evicted (caller then falls back to re-running the query so the table is still
    reproduced instead of silently degrading to prose).
    """
    history = state.get("conversation_history") or []
    if not history:
        return None
    # Pick the most similar prior turn that carries SQL + an id. History arrives
    # oldest-first, so iterating with ``>=`` keeps the most RECENT among ties.
    best: Optional[Dict[str, Any]] = None
    best_sim = -1.0
    for qa in history:
        if not qa.get("id") or not qa.get("generated_sql"):
            continue
        sim = _similarity(question, qa.get("natural_language_query"))
        if sim >= best_sim:
            best_sim = sim
            best = qa
    if not best:
        return None
    cached = _cached_rows_for(state.get("user_id"), state.get("source_key"), best.get("id"))
    if not cached or not cached.get("rows"):
        return None
    rows = cached.get("rows")
    return {
        "query_id": best.get("id"),
        "sql": best.get("generated_sql"),
        "result": {
            "columns": cached.get("columns"),
            "rows": rows,
            "row_count": len(rows),
        },
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

        # Return a previously computed answer for an identical follow-up in this
        # session (bounded TTL) — avoids repeating the LLM call.
        cache_key = answer_cache.key(
            state.get("session_id"), state.get("source_key"), question
        )
        cached_answer = answer_cache.get(cache_key)
        if cached_answer:
            logger.info("memory_answer_generator: served cached answer")
            return {"answer": cached_answer, "route": state.get("route", "from_memory")}

        history_text = "\n".join(
            f"Q: {qa.get('natural_language_query', '')}\n"
            f"SQL: {qa.get('generated_sql', '')}\n"
            f"Result sample: {qa.get('result_preview', '')}"
            for qa in history
        )

        # Fold in the most recent result's cached rows so the model can recompute
        # (sort/filter/aggregate) locally instead of guessing from the preview.
        recompute = _recompute_block(state)
        if recompute:
            history_text = f"{history_text}\n\n{recompute}" if history_text else recompute

        prompt = await prompt_loader.arender(
            "memory_answer",
            question=question,
            conversation_history=history_text or "No prior conversation.",
        )
        model_override = await prompt_loader.model_override_for("memory_answer")

        t0 = time.monotonic()
        response = await llm.generate(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            max_tokens=400,
            model_override=model_override,
            timeout=state.get("llm_timeout_seconds"),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = (response.get("content") or "").strip()
        usage = response.get("usage") or {}
        route = state.get("route", "from_memory")
        answer: Optional[str] = None
        reuse_prior = False

        # Try to detect the JSON control object (needs_query / reuse_prior)
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
            elif parsed.get("reuse_prior"):
                reuse_prior = True
                logger.info("memory_answer_generator: reuse_prior → replay result")
            else:
                answer = content
        except (json.JSONDecodeError, AttributeError):
            answer = content

        # Replay a prior result verbatim so an identical/rephrased repeat renders
        # the SAME table + insights instead of a degraded prose blurb. If the
        # cached rows are gone, fall through to a live re-run (route=needs_query)
        # so the table is still reproduced rather than silently lost.
        base_usage = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "memory_answer_generator": prompt},
        }
        if reuse_prior:
            replay = _reuse_prior_result(state, question)
            if replay:
                logger.info(
                    "memory_answer_generator: replaying query_id=%s (%d rows)",
                    replay["query_id"], replay["result"]["row_count"],
                )
                return {
                    "query_result": replay["result"],
                    "generated_sql": replay["sql"],
                    "answer": None,
                    "route": "from_memory",
                    **base_usage,
                }
            logger.info("memory_answer_generator: reuse_prior but rows evicted → needs_query")
            route = "needs_query"

        logger.info(
            "memory_answer_generator: answer=%s, route=%s",
            bool(answer),
            route,
        )

        # Cache real from-memory answers (not the needs_query escape hatch) so an
        # identical repeated follow-up in this session skips the LLM call.
        if answer and route != "needs_query":
            answer_cache.put(cache_key, answer)

        return {
            "answer": answer,
            "route": route,
            **base_usage,
        }

    return memory_answer_generator
