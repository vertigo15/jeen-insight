"""prior_data_binder — carry values from a prior result into a new live query.

Route ``needs_query`` with ``prior_refs``: "take the 4 most expensive products
from the previous answer and show me their sales". The new SQL needs *values*
from a stored result, so this node:

1. recovers the referenced turn's rows (``PriorResultStore``),
2. asks the small model for one SELECT over that snapshot that yields the
   needed values (``SELECT product_id FROM t3 ORDER BY price DESC LIMIT 4``) and
   the live catalog column they filter (``dimproduct.productkey``),
3. runs the SELECT in the metadata Postgres and emits the values as a **resolved filter**
   (``op: in``), appended to ``filter_plan`` / ``resolved_filters``.

From there the existing machinery does the rest: ``prompt_builder`` puts the
filter in the runtime contract, ``sql_generator`` writes ``WHERE … IN (…)`` and
``sqlglot_validate`` refuses SQL that drops or widens the verified set. Too many
values (``MEMORY_MAX_BOUND_VALUES``) is a question back to the user rather than
an unbounded IN list. When the rows cannot be recovered the node steps aside:
the SQL generator still sees the prior turn's SQL in the ledger and may nest it
as a subquery.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.agent.langgraph_agent.nodes.context import find_turn, ledger_for, render_ledger
from src.agent.langgraph_agent.nodes.safety_text import fence_untrusted
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.prior_results import PriorResultStore
from src.agent.snapshot_sql import SnapshotSqlEngine, schema_and_sample, snapshot_table_name
from src.agent.token_usage import merge_usage

logger = logging.getLogger(__name__)

BINDING_SOURCE = "prior_result"
_MAX_REFS = 2


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


def _merge_binding(plan: Dict[str, Any], resolved: List[Dict[str, Any]], binding: Dict[str, Any]) -> None:
    """Append *binding* unless an identical target/value set is already planned."""
    key = (binding["target"], json.dumps(binding["value"], sort_keys=True, default=str))
    for item in list(plan.get("filters") or []):
        if (item.get("target"), json.dumps(item.get("value"), sort_keys=True, default=str)) == key:
            return
    plan.setdefault("filters", []).append(binding)
    resolved.append(binding)


def build_binding(
    *, handle: str, table: str, column: str, values: List[Any], data_type: str = ""
) -> Dict[str, Any]:
    """A resolved filter in the shape ``filter_grounder`` produces."""
    unique: List[Any] = list(dict.fromkeys(values))
    single = len(unique) == 1
    return {
        "table": table.lower(),
        "column": column.lower(),
        "target": f"{table.lower()}.{column.lower()}",
        "op": "equals" if single else "in",
        "value": unique[0] if single else unique,
        "raw_value": None,
        "data_type": data_type,
        "resolved": True,
        "lookup_source": BINDING_SOURCE,
        "ref": handle,
    }


def make_prior_data_binder(
    llm: LangChainLlmService,
    prompt_loader: PromptLoader,
    *,
    store: Optional[PriorResultStore] = None,
    engine: Optional[SnapshotSqlEngine] = None,
    sample_rows: Optional[int] = None,
    max_values: Optional[int] = None,
    max_rows: Optional[int] = None,
):
    """Return the async ``prior_data_binder`` node."""
    from src.config import settings  # noqa: PLC0415

    store = store or PriorResultStore()
    engine = engine or SnapshotSqlEngine(None)
    sample_n = int(sample_rows if sample_rows is not None else settings.MEMORY_SAMPLE_ROWS)
    cap = int(max_values if max_values is not None else settings.MEMORY_MAX_BOUND_VALUES)
    max_n = int(max_rows if max_rows is not None else settings.MEMORY_COMPUTE_MAX_ROWS)

    async def prior_data_binder(state: AgentState) -> Dict[str, Any]:
        plan = dict(state.get("filter_plan") or {"filters": [], "invalid_filters": []})
        plan["filters"] = list(plan.get("filters") or [])
        resolved = list(state.get("resolved_filters") or [])
        telemetry = dict(state.get("memory_telemetry") or {})

        # Re-entry (catalog refresh, filter reground): keep the bindings already
        # made without another model call.
        existing = list(state.get("prior_bindings") or [])
        if existing:
            for binding in existing:
                _merge_binding(plan, resolved, binding)
            return {"filter_plan": plan, "resolved_filters": resolved}

        ledger = ledger_for(state)
        refs = []
        for handle in state.get("prior_refs") or []:
            turn = find_turn(ledger, handle)
            if turn and turn not in refs:
                refs.append(turn)
        refs = refs[:_MAX_REFS]
        if not refs:
            return {}

        tables: Dict[str, Dict[str, Any]] = {}
        sources: Dict[str, str] = {}

        async def load(turns: List[Dict[str, Any]], *, allow_rerun: bool) -> None:
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

        # Cheap tiers only before the model has said which values it needs.
        await load(refs, allow_rerun=False)
        telemetry.update({"binder_refs": [t["handle"] for t in refs], "binder_data_sources": sources})

        data_lines = []
        queryable: List[str] = []
        for turn in refs:
            tname = snapshot_table_name(turn["handle"])
            data = tables.get(tname)
            if data:
                columns, sample = schema_and_sample(data, sample_rows=sample_n)
                data_lines.append(
                    f"{turn['handle']} → table {tname}: {data['row_count']} rows; "
                    f"columns: {', '.join(columns)}\nsample rows: {json.dumps(sample, ensure_ascii=False, default=str)}"
                )
                queryable.append(tname)
            elif turn.get("columns") and turn["data_status"] != "none":
                data_lines.append(
                    f"{turn['handle']} → table {tname}: {turn.get('row_count') or '?'} rows "
                    f"(loaded on demand); columns: {', '.join(turn['columns'])}"
                )
                queryable.append(tname)
        if not queryable:
            logger.info("prior_data_binder: no data for %s — leaving composition to the SQL model", sources)
            return {"memory_telemetry": telemetry}

        bundle = state.get("metadata_bundle") or {}
        prompt = await prompt_loader.arender(
            "prior_data_binder",
            question=state.get("question", ""),
            conversation_history=fence_untrusted(render_ledger(ledger), label="conversation ledger"),
            prior_data=fence_untrusted("\n\n".join(data_lines), label="prior result data"),
            tables=", ".join(sorted(queryable)),
            columns=bundle.get("columns", ""),
        )
        model_override = await prompt_loader.model_override_for("prior_data_binder")
        t0 = time.monotonic()
        response = await llm.generate(
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": state.get("question", "")}],
            temperature=0.0, max_tokens=400, model_override=model_override, timeout=state.get("llm_timeout_seconds"),
        )
        usage = {
            "llm_call_count": (state.get("llm_call_count") or 0) + 1,
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + int((time.monotonic() - t0) * 1000),
            "token_usage": merge_usage(state.get("token_usage") or {}, response.get("usage") or {}),
            "node_prompts": {**(state.get("node_prompts") or {}), "prior_data_binder": prompt},
            "memory_telemetry": telemetry,
        }
        parsed = _extract_json(response.get("content") or "") or {}
        if parsed.get("skip") or not parsed.get("extract_sql"):
            logger.info("prior_data_binder: model chose not to bind (%s)", parsed.get("reason"))
            return usage

        # The model wants values → now the rows are worth a source re-run.
        await load(refs, allow_rerun=True)
        telemetry["binder_data_sources"] = sources
        if not tables:
            logger.info("prior_data_binder: rows unrecoverable — leaving composition to the SQL model")
            return usage

        result = await engine.run(tables, str(parsed["extract_sql"]), max_rows=max_n)
        if result.get("error") or not result.get("rows"):
            logger.info("prior_data_binder: extraction failed (%s)", result.get("error") or "no rows")
            return usage
        first_col = result["columns"][0]
        values = [r.get(first_col) for r in result["rows"] if r.get(first_col) is not None]
        values = list(dict.fromkeys(values))
        if not values:
            return usage
        if len(values) > cap:
            message = (
                f"That would carry {len(values)} values from {refs[0]['handle']} into the new query "
                f"(the limit is {cap}). Please narrow it down — for example the top {cap} — and ask again."
            )
            return {**usage, "filter_clarification_required": True, "clarification": message, "answer": message}

        bind = parsed.get("bind") or {}
        table = str(bind.get("table") or "").strip().lower()
        column = str(bind.get("column") or "").strip().lower()
        table_columns = state.get("table_columns") or {}
        known = {t.lower(): {c.lower() for c in cols} for t, cols in table_columns.items()}
        if table not in known or column not in known[table]:
            logger.info("prior_data_binder: bind target %s.%s not in catalog — leaving composition to the SQL model", table, column)
            return usage

        binding = build_binding(handle=refs[0]["handle"], table=table, column=column, values=values)
        _merge_binding(plan, resolved, binding)
        telemetry.update({"binder_values": len(values), "binder_target": binding["target"]})
        logger.info("prior_data_binder: bound %d value(s) from %s to %s", len(values), refs[0]["handle"], binding["target"])
        return {
            **usage,
            "filter_plan": plan,
            "resolved_filters": resolved,
            "prior_bindings": [binding],
            "memory_telemetry": telemetry,
        }

    return prior_data_binder
