"""memory_answer_generator — follow-ups about a prior turn's answer or data.

Route ``from_memory``. The router has named the prior turn(s) the question
refers to (``prior_refs``, ledger handles such as ``T3``); when it named none,
the most recent turn with data is assumed ("show that again").

One small-model call sees the ledger plus, for each referenced turn, the column
schema and a few sample rows — never the full data — and picks an action:

replay       Re-display the referenced result as it was, with its answer.
compute      A SELECT over the stored rows (exposed to the metadata Postgres as
             ``jsonb_to_recordset`` CTEs, one per turn: ``insights_mem_t3``)
             for max / sort / filter / what-if.
             Validated by sqlglot, executed by the database; the result is an
             ordinary ``query_result`` that flows through trivial-check → eval
             like a live query.
answer       Prose from the ledger alone ("what did you find about March?").
needs_query  New data is required → the graph falls through to the SQL path.

Rows are recovered by ``PriorResultStore`` (cache → stored snapshot → re-run),
so the prompt cost of memory is independent of result size.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.agent.answer_cache import answer_cache
from src.agent.langgraph_agent.nodes.context import (
    find_turn,
    latest_turn_with_data,
    ledger_for,
    render_ledger,
)
from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.prior_results import PriorResultStore
from src.agent.snapshot_sql import (
    MEMORY_SQL_MARKER,
    SnapshotSqlEngine,
    schema_and_sample,
    snapshot_table_name,
)
from src.agent.token_usage import merge_usage

logger = logging.getLogger(__name__)

ACTION_REPLAY = "replay"
ACTION_COMPUTE = "compute"
ACTION_ANSWER = "answer"
ACTION_NEEDS_QUERY = "needs_query"
_ACTIONS = frozenset({ACTION_REPLAY, ACTION_COMPUTE, ACTION_ANSWER, ACTION_NEEDS_QUERY})

_MAX_REFS = 3
_COMPUTE_RETRIES = 1


def on_memory_branch(state: AgentState) -> bool:
    """True when this request produced its table from stored rows (replay or
    compute). Such a result must never enter the SQL/DAX repair loops."""
    return (
        state.get("route") == "from_memory"
        and state.get("memory_action") in (ACTION_REPLAY, ACTION_COMPUTE)
        and bool(state.get("query_result"))
    )


def memory_needs_eval(state: AgentState) -> bool:
    """A computed table gets the same narration as a live result; a replay
    already carries the answer it was given the first time."""
    return on_memory_branch(state) and state.get("memory_action") == ACTION_COMPUTE


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    text = (content or "").strip()
    if "```" in text:
        start = text.find("```") + 3
        if text[start:].startswith("json"):
            start += 4
        end = text.find("```", start)
        text = text[start:end].strip() if end > start else text
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decide(parsed: Optional[Dict[str, Any]], content: str) -> Dict[str, Any]:
    """Normalise the model reply to ``{action, ref, sql, answer}``.

    Accepts the legacy control objects (``{"needs_query": true}``,
    ``{"reuse_prior": true}``) and treats a non-JSON reply as a prose answer,
    so a DB-overridden older prompt still works.
    """
    if parsed is None:
        return {"action": ACTION_ANSWER, "ref": None, "sql": None, "answer": content.strip()}
    if parsed.get("needs_query"):
        return {"action": ACTION_NEEDS_QUERY, "ref": None, "sql": None, "answer": None}
    if parsed.get("reuse_prior"):
        return {"action": ACTION_REPLAY, "ref": parsed.get("ref"), "sql": None, "answer": None}
    action = str(parsed.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        action = ACTION_COMPUTE if parsed.get("sql") else ACTION_ANSWER
    return {
        "action": action,
        "ref": parsed.get("ref"),
        "sql": str(parsed.get("sql") or "").strip() or None,
        "answer": str(parsed.get("answer") or "").strip() or None,
    }


def make_memory_answer_generator(
    llm: LangChainLlmService,
    prompt_loader: PromptLoader,
    *,
    store: Optional[PriorResultStore] = None,
    engine: Optional[SnapshotSqlEngine] = None,
    sample_rows: Optional[int] = None,
    max_rows: Optional[int] = None,
):
    """Return the async ``memory_answer_generator`` node.

    ``store`` recovers prior rows (defaults to the in-process cache only, which
    is what unit tests and the standalone node need). ``engine`` runs the
    ``compute`` SELECTs (defaults to an engine without a database, so compute
    falls through to a live query). ``sample_rows`` / ``max_rows`` default to
    ``MEMORY_SAMPLE_ROWS`` / ``MEMORY_COMPUTE_MAX_ROWS``.
    """
    from src.config import settings  # noqa: PLC0415 — keep import light for tests

    store = store or PriorResultStore()
    engine = engine or SnapshotSqlEngine(None)
    sample_n = int(sample_rows if sample_rows is not None else settings.MEMORY_SAMPLE_ROWS)
    max_n = int(max_rows if max_rows is not None else settings.MEMORY_COMPUTE_MAX_ROWS)

    async def memory_answer_generator(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        ledger = ledger_for(state)
        usage_base: Dict[str, Any] = {
            "llm_call_count": state.get("llm_call_count") or 0,
            "llm_latency_ms": state.get("llm_latency_ms") or 0,
            "token_usage": state.get("token_usage") or {},
        }
        telemetry = dict(state.get("memory_telemetry") or {})

        def finish(updates: Dict[str, Any], *, action: str, refs: List[str], sources: Dict[str, str],
                   rows_used: int = 0, compute_ms: int = 0) -> Dict[str, Any]:
            telemetry.update({
                "action": action, "refs": refs, "data_sources": sources,
                "rows_used": rows_used, "compute_ms": compute_ms,
            })
            return {**usage_base, **updates, "memory_action": action, "memory_telemetry": telemetry}

        if not ledger:
            logger.info("memory_answer_generator: no prior turns → needs_query")
            return finish({"route": ACTION_NEEDS_QUERY}, action=ACTION_NEEDS_QUERY, refs=[], sources={})

        # ── Resolve the referenced turns and recover their rows ──────────
        refs: List[Dict[str, Any]] = []
        for handle in state.get("prior_refs") or []:
            turn = find_turn(ledger, handle)
            if turn and turn not in refs:
                refs.append(turn)
        if not refs:
            latest = latest_turn_with_data(ledger)
            if latest:
                refs.append(latest)
        refs = refs[:_MAX_REFS]

        tables: Dict[str, Dict[str, Any]] = {}
        sources: Dict[str, str] = {}

        async def load(turns: List[Dict[str, Any]], *, allow_rerun: bool) -> None:
            """Fill ``tables`` for *turns* not loaded yet. Cheap tiers only before
            the model has chosen; a source re-run only once rows are needed."""
            for turn in turns:
                tname = snapshot_table_name(turn["handle"])
                if tname in tables or not turn.get("query_id"):
                    continue
                data = await store.rows(
                    user_id=state.get("user_id"), connection=state.get("source_key"),
                    query_id=turn["query_id"], session_id=state.get("session_id"),
                    sql=turn.get("sql"), allow_rerun=allow_rerun,
                )
                if data:
                    tables[tname] = data
                    sources[turn["handle"]] = data["source"]
                else:
                    sources[turn["handle"]] = "unavailable" if allow_rerun else "on_demand"

        await load(refs, allow_rerun=False)
        ref_handles = [t["handle"] for t in refs]

        # ── Answer cache: identical follow-up in this session → same prose ─
        cache_key = answer_cache.key(state.get("session_id"), state.get("source_key"), question)
        cached_answer = answer_cache.get(cache_key)
        if cached_answer:
            logger.info("memory_answer_generator: served cached answer")
            return finish({"answer": cached_answer, "route": "from_memory"},
                          action=ACTION_ANSWER, refs=ref_handles, sources=sources)

        # ── Prompt: ledger + schema/sample of the referenced data ─────────
        # A turn whose rows are not in a cheap tier is still queryable: its
        # columns are known from the artifact, and the rows are re-run from the
        # source only if the model actually chooses replay/compute.
        data_lines: List[str] = []
        queryable: List[str] = []
        for turn in refs:
            tname = snapshot_table_name(turn["handle"])
            data = tables.get(tname)
            if data:
                columns, sample = schema_and_sample(data, sample_rows=sample_n)
                data_lines.append(
                    f"{turn['handle']} → table {tname}: {data['row_count']} rows"
                    f"{' (truncated)' if data.get('truncated') else ''}; columns: {', '.join(columns)}\n"
                    f"sample rows: {json.dumps(sample, ensure_ascii=False, default=str)}"
                )
                queryable.append(tname)
            elif turn.get("columns") and (turn.get("sql") or turn["data_status"] != "none"):
                data_lines.append(
                    f"{turn['handle']} → table {tname}: {turn.get('row_count') or '?'} rows "
                    f"(loaded on demand); columns: {', '.join(turn['columns'])}"
                )
                queryable.append(tname)
            else:
                data_lines.append(f"{turn['handle']}: data not available (only the ledger line above)")
        prior_data = fence_untrusted("\n\n".join(data_lines), label="prior result data") if data_lines else \
            "(no prior turn could be resolved)"
        ledger_text = fence_untrusted(render_ledger(ledger), label="conversation ledger")

        prompt = await prompt_loader.arender(
            "memory_answer",
            question=question,
            conversation_history=ledger_text,
            prior_data=prior_data,
            tables=", ".join(sorted(queryable)) or "none",
        )
        model_override = await prompt_loader.model_override_for("memory_answer")
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ]

        async def ask() -> Dict[str, Any]:
            t0 = time.monotonic()
            response = await llm.generate(
                messages=messages, temperature=0.0, max_tokens=500,
                model_override=model_override, timeout=state.get("llm_timeout_seconds"),
            )
            usage_base["llm_call_count"] += 1
            usage_base["llm_latency_ms"] += int((time.monotonic() - t0) * 1000)
            usage_base["token_usage"] = merge_usage(usage_base["token_usage"], response.get("usage") or {})
            return response

        response = await ask()
        content = (response.get("content") or "").strip()
        decision = _decide(_extract_json(content), content)
        node_prompts = {**(state.get("node_prompts") or {}), "memory_answer_generator": prompt}
        common = {"node_prompts": node_prompts, "route": "from_memory"}

        target = find_turn(ledger, decision.get("ref")) if decision.get("ref") else None
        if target is None and refs:
            target = refs[0]

        # ── Act ──────────────────────────────────────────────────────────
        if decision["action"] == ACTION_NEEDS_QUERY:
            logger.info("memory_answer_generator: escape hatch → needs_query")
            return finish({**common, "route": ACTION_NEEDS_QUERY},
                          action=ACTION_NEEDS_QUERY, refs=ref_handles, sources=sources)

        if decision["action"] == ACTION_ANSWER:
            answer = decision.get("answer") or content
            if answer:
                answer_cache.put(cache_key, answer)
            return finish({**common, "answer": answer or None},
                          action=ACTION_ANSWER, refs=ref_handles, sources=sources)

        # Rows are needed now → allow the source re-run for what is still missing.
        if decision["action"] == ACTION_REPLAY and target:
            await load([target], allow_rerun=True)
        elif decision["action"] == ACTION_COMPUTE:
            await load(refs, allow_rerun=True)

        if decision["action"] == ACTION_REPLAY:
            data = tables.get(snapshot_table_name(target["handle"])) if target else None
            if not data:
                # Rows are gone on every tier → re-run the question live so the
                # table is still reproduced instead of degrading to prose.
                logger.info("memory_answer_generator: replay requested but no rows → needs_query")
                return finish({**common, "route": ACTION_NEEDS_QUERY},
                              action=ACTION_NEEDS_QUERY, refs=ref_handles, sources=sources)
            marker = f"{MEMORY_SQL_MARKER} replay of {target['handle']} (query_id {target.get('query_id')})"
            return finish(
                {
                    **common,
                    "query_result": {"columns": data["columns"], "rows": data["rows"],
                                     "row_count": data["row_count"], "truncated": data.get("truncated", False)},
                    "generated_sql": f"{marker}\n{target.get('sql') or ''}".rstrip(),
                    "answer": target.get("answer") or None,
                },
                action=ACTION_REPLAY, refs=ref_handles, sources=sources, rows_used=data["row_count"],
            )

        # compute
        if not tables:
            logger.info("memory_answer_generator: compute requested but no rows → needs_query")
            return finish({**common, "route": ACTION_NEEDS_QUERY},
                          action=ACTION_NEEDS_QUERY, refs=ref_handles, sources=sources)
        sql = decision.get("sql")
        t0 = time.monotonic()
        result = await engine.run(tables, sql or "", max_rows=max_n)
        attempts = 0
        while result.get("error") and attempts < _COMPUTE_RETRIES:
            attempts += 1
            logger.info("memory_answer_generator: compute failed (%s) — retrying", result["error"])
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": (
                f"That SQL failed: {result['error']} Return the corrected JSON only; "
                f"use only the tables {', '.join(sorted(tables))} and the exact column names shown."
            )})
            response = await ask()
            content = (response.get("content") or "").strip()
            decision = _decide(_extract_json(content), content)
            sql = decision.get("sql")
            if decision["action"] != ACTION_COMPUTE or not sql:
                break
            result = await engine.run(tables, sql, max_rows=max_n)
        compute_ms = int((time.monotonic() - t0) * 1000)

        if result.get("error") or not sql:
            logger.info("memory_answer_generator: compute gave up (%s) → needs_query", result.get("error"))
            return finish({**common, "route": ACTION_NEEDS_QUERY},
                          action=ACTION_NEEDS_QUERY, refs=ref_handles, sources=sources, compute_ms=compute_ms)

        rows_used = sum(t["row_count"] for t in tables.values())
        marker = f"{MEMORY_SQL_MARKER} computed over {', '.join(sorted(tables))} (stored rows of {', '.join(ref_handles)})"
        logger.info("memory_answer_generator: compute ok — %d rows in %dms", result["row_count"], compute_ms)
        return finish(
            {
                **common,
                "query_result": {"columns": result["columns"], "rows": result["rows"],
                                 "row_count": result["row_count"], "truncated": result.get("truncated", False)},
                "generated_sql": f"{marker}\n{sql}",
                "answer": None,
                "is_trivial": False,
                "eval_result": None,
            },
            action=ACTION_COMPUTE, refs=ref_handles, sources=sources, rows_used=rows_used, compute_ms=compute_ms,
        )

    return memory_answer_generator
