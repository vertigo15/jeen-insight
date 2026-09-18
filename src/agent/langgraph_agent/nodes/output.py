"""Output pipeline nodes.

response_formatter   Pure-Python sync node.  Assembles the final API response
                     dict, preserving the exact key schema expected by the UI.
save_to_memory       Async node.  Persists SQL, token usage, and execution
                     result back to the ConversationHistoryService.
observability_log    Pure-Python sync node.  Emits a structured JSON event so
                     any log aggregator (Azure Monitor, Grafana/Loki, etc.) can
                     parse it.  Grep for QUERY_EVENT to filter these lines.
"""

from __future__ import annotations

import decimal
import logging
import time
from decimal import ROUND_HALF_UP
from datetime import date, datetime, time as dt_time
from typing import Any, Dict, List, Optional

from src.agent.conversation_artifacts import (
    RESULT_KIND_TABLE,
    SNAPSHOT_NOT_APPLICABLE,
    build_result_snapshot,
    coerce_json_safe_rows,
    extract_artifact_fields,
    json_safe_value,
)
from src.agent.conversation_history import ConversationHistoryService, insight_text
from src.agent.langgraph_agent.state import AgentState

logger = logging.getLogger(__name__)


def _coerce_json_safe(rows: List[Any]) -> List[Any]:
    """Convert common DB driver values so rows are JSON-serialisable.

    Different drivers return different Python types for warehouse values
    (Decimal, datetime/date/time, UUID, bytes, etc.). Accepts dict rows and
    positional rows. Kept as a thin alias for existing call sites/tests.
    """
    return coerce_json_safe_rows(rows)


def _json_safe_value(value: Any) -> Any:
    return json_safe_value(value)


# ── Result artifact ────────────────────────────────────────────────────────────

_ARTIFACT_STATS_SCAN_CAP = 2000


def _build_result_artifact(sql: Optional[str], query_result: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact, durable summary of a result set for follow-up detection.

    Shape::

        {
            "columns":      [str, ...],
            "column_types": {col: "int"|"float"|"str"|"bool"|"datetime"|...},
            "row_count":    int,
            "stats":        {col: {"non_null": int, "min": ..., "max": ...}},
            "sql":          str,
            "created_at":   ISO-8601 str,
        }

    Cheap by design: numeric min/max and non-null counts are computed over at
    most ``_ARTIFACT_STATS_SCAN_CAP`` rows so large results don't slow the tail.
    """
    columns: List[str] = list(query_result.get("columns") or [])
    rows: List[Dict[str, Any]] = query_result.get("rows") or []
    row_count = query_result.get("row_count")
    if row_count is None:
        row_count = len(rows)

    if not columns and rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())

    sample = rows[:_ARTIFACT_STATS_SCAN_CAP]
    column_types: Dict[str, str] = {}
    stats: Dict[str, Dict[str, Any]] = {}

    for col in columns:
        non_null = 0
        col_type = None
        vmin: Any = None
        vmax: Any = None
        for row in sample:
            if not isinstance(row, dict):
                continue
            val = row.get(col)
            if val is None:
                continue
            non_null += 1
            if col_type is None:
                col_type = _type_name(val)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                vmin = val if vmin is None or val < vmin else vmin
                vmax = val if vmax is None or val > vmax else vmax
        entry: Dict[str, Any] = {"non_null": non_null}
        if vmin is not None:
            entry["min"] = _json_safe_value(vmin)
            entry["max"] = _json_safe_value(vmax)
        stats[col] = entry
        if col_type:
            column_types[col] = col_type

    return {
        "columns": columns,
        "column_types": column_types,
        "row_count": row_count,
        "stats": stats,
        "sql": sql,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, decimal.Decimal):
        return "float"
    if isinstance(value, (datetime, date, dt_time)):
        return "datetime"
    return "str"


def _format_trivial_value(value: Any) -> str:
    """Format a scalar answer using the same rules as result-table numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
        return str(value)

    try:
        number = decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        return str(value)
    if not number.is_finite():
        return str(value)

    # Result tables group thousands and omit fractional noise at this scale.
    if abs(number) >= 1000:
        rounded = number.quantize(decimal.Decimal("1"), rounding=ROUND_HALF_UP)
        return f"{rounded:,}"
    if number == number.to_integral_value():
        return f"{number:,.0f}"

    # Match the table's four-decimal maximum for smaller values.
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _filter_summary(state: AgentState) -> Optional[Dict[str, Any]]:
    """Provenance of the filters that shaped this answer, for the UI.

    ``resolved`` carries raw → canonical value, target, operator and the
    evidence tier; ``unverified`` the literals no evidence could confirm;
    ``assumptions`` the disclosed decisions (corrections, retargets).
    """
    show_values = str(state.get("filter_value_visibility") or "source_wide") == "source_wide"
    resolved = []
    for item in state.get("resolved_filters") or []:
        if not isinstance(item, dict):
            continue
        chosen = {str(item.get("table") or "").lower() + "." + str(item.get("column") or "").lower()}
        chosen |= {str(c).lower() for c in (item.get("any_of_columns") or [])}
        # Other columns the reverse lookup found the value in (already
        # governance-filtered by the planner): the UI offers them as a switch.
        alternatives: List[Dict[str, Any]] = []
        seen: set = set()
        for hit in item.get("candidate_columns") or []:
            if not isinstance(hit, dict):
                continue
            key = f"{str(hit.get('table') or '').lower()}.{str(hit.get('column') or '').lower()}"
            if key in chosen or key in seen or not hit.get("table") or not hit.get("column"):
                continue
            seen.add(key)
            alternatives.append({
                "table": hit.get("table"), "column": hit.get("column"),
                "value": hit.get("value") if show_values else None,
            })
        resolved.append({
            "table": item.get("table"),
            "column": item.get("column"),
            "op": item.get("op"),
            "value": item.get("value"),
            "raw_value": item.get("raw_value"),
            "evidence": item.get("evidence") or ("typed" if item.get("resolved") else None),
            "tier": item.get("tier"),
            "any_of_columns": item.get("any_of_columns"),
            "alternatives": alternatives[:4],
        })
    unverified = [
        {"table": u.get("target", "").split(".")[0] if isinstance(u.get("target"), str) else None,
         "column": u.get("target", "").split(".")[-1] if isinstance(u.get("target"), str) else None,
         "value": u.get("value"), "reason": u.get("reason")}
        for u in (state.get("unresolved_filters") or [])
        if isinstance(u, dict) and u.get("value") is not None
    ]
    assumptions = [a for a in (state.get("plan_assumptions") or []) if a]
    if not resolved and not unverified and not assumptions:
        return None
    return {"resolved": resolved, "unverified": unverified, "assumptions": assumptions}


# ── response_formatter ────────────────────────────────────────────────────────


def response_formatter(state: AgentState) -> Dict[str, Any]:
    """Assemble the API response dict.

    Preserves the key contract the UI expects:
    question, query_id, session_id, sql, results, answer, prompt, error, metrics.

    Additionally attaches ``insights`` and ``follow_up`` when available.
    """
    route = state.get("route", "needs_query")
    eval_result = state.get("eval_result") or {}
    proposal = state.get("analysis_proposal")
    analysis = state.get("analysis_result")

    answer: Optional[str] = state.get("answer")

    # Override answer based on terminal state
    if proposal:
        # Confirm card / clarification / guard refusal: the message is the answer
        # and the proposal carries the chips, options and guard numbers.
        answer = proposal.get("message") or answer
    elif state.get("analysis_error"):
        answer = state.get("analysis_error")
    elif analysis and eval_result.get("summary"):
        answer = eval_result["summary"]
    elif analysis:
        answer = analysis.get("headline") or answer
    elif state.get("clarification"):
        answer = state["clarification"]
    elif state.get("dlp_blocked"):
        answer = state.get("governance_error") or (
            "This query references governed data and has been blocked."
        )
    elif route == "unsafe":
        answer = "I can only execute read-only SELECT queries. I cannot modify data."
    elif route in ("out_of_scope", "greeting") and not answer:
        # greeting: answer is already set by fused_router; only use fallback if missing.
        # out_of_scope: always use the canned message.
        if route == "out_of_scope":
            display = state.get("connection_display_name") or "the database"
            answer = (
                f"I'm here to assist with data queries for {display}. "
                "This question appears to be outside my scope."
            )
    elif eval_result.get("summary"):
        answer = eval_result["summary"]
    elif state.get("is_trivial") and not answer:
        # Format single-value / small results without an extra LLM call.
        rows = (state.get("query_result") or {}).get("rows") or []
        if rows:
            row = rows[0]
            parts = []
            for col, val in row.items():
                label = col.replace("_", " ").title()
                parts.append(f"{label}: {_format_trivial_value(val)}")
            answer = " | ".join(parts) if parts else None
    elif not state.get("generated_sql") and not answer:
        # Only fall back to clarification / error_context when no answer has been set.
        # (from_memory route already populated `answer` via memory_answer_generator.)
        answer = state.get("clarification") or state.get("error_context")
    elif state.get("sqlglot_error") and not answer:
        answer = state.get("error_context") or state.get("sqlglot_error")

    error = (
        state.get("error")
        or state.get("exec_error")
        or state.get("sqlglot_error")
        or state.get("governance_error")
        or state.get("analysis_error")
    )
    if proposal and not state.get("analysis_error"):
        # A proposal is a result, not an error: it saves to history, appears in
        # the trace and keeps the status strip.
        error = None

    formatted: Dict[str, Any] = {
        "question": state.get("question", ""),
        "query_id": state.get("query_id"),
        "session_id": state.get("session_id"),
        "sql": state.get("generated_sql") if not proposal else None,
        "results": state.get("query_result") if not proposal else None,
        "answer": answer,
        "prompt": state.get("structured_prompt"),
        "error": error,
        "metrics": {
            "input_tokens": (state.get("token_usage") or {}).get("input_tokens"),
            "output_tokens": (state.get("token_usage") or {}).get("output_tokens"),
            "total_tokens": (state.get("token_usage") or {}).get("total_tokens"),
            "llm_latency_ms": state.get("llm_latency_ms"),
            "execution_time_ms": state.get("execution_time_ms"),
            "retry_count": state.get("retry_count", 0),
            "llm_call_count": state.get("llm_call_count", 0),
            "route": route,
        },
        # Why this answer took the path it did — ML skill or text-to-SQL — so
        # the UI can name it and tests can assert it without reading the trace.
        "routing": {
            "route": route,
            "source": state.get("route_source"),
            "reason": state.get("route_reason") or "",
            "skill": state.get("analysis_skill"),
            "path": "ml" if route == "needs_analysis" else "sql" if route == "needs_query" else route,
        },
    }

    # ── ML skills ─────────────────────────────────────────────────────────
    if state.get("analysis_skill"):
        formatted["metrics"]["skill"] = state.get("analysis_skill")
    if state.get("parent_query_id"):
        formatted["parent_query_id"] = state.get("parent_query_id")
    # Filter grounding provenance: what was matched, corrected, retargeted or
    # left unverified, so the UI can show "Filtered by …" and offer a switch,
    # and a structured column/value question when the grounder had to ask.
    filter_summary = _filter_summary(state)
    if filter_summary:
        formatted["filters"] = filter_summary
        if filter_summary.get("assumptions") and answer and not state.get("filter_clarification"):
            note = " ".join(filter_summary["assumptions"])
            if note not in answer:
                formatted["answer"] = f"{answer}\n\nNote: {note}"
    if state.get("filter_clarification"):
        formatted["filter_clarification"] = state["filter_clarification"]
    if state.get("filter_metrics"):
        formatted["metrics"]["filter_grounding"] = state["filter_metrics"]
    if proposal:
        kind = proposal.get("kind")
        formatted["status"] = {"confirm": "confirm", "clarify": "clarify", "guard": "blocked"}.get(kind, kind)
        formatted["proposal"] = proposal
    elif analysis:
        from src.analysis.contracts import ResultEnvelope  # noqa: PLC0415

        try:
            view = ResultEnvelope.model_validate(analysis).artifact_view()
        except Exception:  # noqa: BLE001 — never lose the answer over the view
            view = {k: v for k, v in analysis.items() if k not in ("rows", "columns")}
        if state.get("analysis_dropped_filters"):
            view.setdefault("caveats", []).append(
                "Filters not applied (other tables): " + ", ".join(state["analysis_dropped_filters"])
            )
        formatted["status"] = "completed"
        formatted["analysis"] = view
        formatted["low_confidence"] = bool(state.get("low_confidence") or view.get("low_confidence"))

    # Eval output, named to match GenerateInsightsResponse so the two endpoints
    # that expose this analysis return one shape. These must also be declared on
    # QueryResponse — Pydantic drops any key the model doesn't know about, which
    # is how this whole block used to be thrown away.
    #
    # `insights` is the eval node's name for what the API calls `findings`, and
    # the node emits `follow_up_questions`, not `follow_up`. Findings are meant
    # to be plain strings but occasionally come back as highlight-fragment
    # arrays, so flatten rather than let one stray shape fail the response.
    if eval_result.get("insights"):
        formatted["findings"] = [
            insight_text(f) for f in eval_result["insights"] if f
        ]
    if eval_result.get("suggestions"):
        formatted["suggestions"] = [
            insight_text(s) for s in eval_result["suggestions"] if s
        ]
    if eval_result.get("follow_up_questions"):
        formatted["followups"] = [
            insight_text(q) for q in eval_result["follow_up_questions"] if q
        ]

    # ── Execution trace ───────────────────────────────────────────────────────────────────
    # NOTE: the trace is intentionally NOT attached here. response_formatter
    # runs before the tail nodes (save_to_memory, observability_log), so the
    # trace at this point is incomplete. The agent attaches the COMPLETE,
    # enriched trace from the final graph state once every node has executed
    # (see JeenInsightsAgent.process_question → _enrich_trace).

    # ── Node prompts ──────────────────────────────────────────────────────────────
    # Collected prompts for each LLM node, surfaced to the developer panel.
    node_prompts = state.get("node_prompts") or {}
    if node_prompts:
        formatted["node_prompts"] = node_prompts

    return {"formatted_response": formatted}


def slim_trace(events: list) -> List[Dict[str, Any]]:
    """Reduce an execution trace to the fields that are safe to store.

    The in-memory trace is built for the developer panel: ``_enrich_trace``
    hangs the fully rendered LLM prompt off every node that called a model,
    plus prose ``detail`` lines that can quote the generated SQL. None of that
    belongs in a telemetry column — it carries catalog schema and user data,
    and dwarfs the timings we actually want to aggregate.

    Order is preserved and repeated node names are kept: a second
    ``sql_generator`` event is a repair retry, and collapsing the two would
    hide exactly the pathology this data exists to find.
    """
    slim: List[Dict[str, Any]] = []
    for event in events or []:
        node = event.get("node")
        if not node:
            continue
        try:
            elapsed = int(event.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            elapsed = 0
        slim.append({
            "node": str(node),
            "elapsed_ms": elapsed,
            "type": str(event.get("type") or "logic"),
        })
    return slim


def _enrich_trace(events: list, state: "AgentState") -> None:  # type: ignore[name-defined]
    """Mutate each event in-place with human-readable detail from state."""
    result = state.get("query_result") or {}
    eval_result = state.get("eval_result") or {}
    node_prompts = state.get("node_prompts") or {}

    for ev in events:
        node = ev.get("node", "")

        if node == "memory_shrink_check":
            ev["detail"] = "over budget — summarising" if state.get("is_over_budget") else "within budget"

        elif node == "memory_summarizer":
            s = state.get("memory_summary") or ""
            ev["detail"] = s[:80] + ("…" if len(s) > 80 else "")

        elif node == "fused_router":
            route = state.get("route", "?")
            reason = state.get("route_reason", "")
            ev["detail"] = f"route = {route}" + (f" — {reason[:60]}" if reason else "")
            ev["route"] = route

        elif node == "memory_answer_generator":
            ans = state.get("answer")
            if isinstance(ans, list):
                # Fragment array — render as plain text for the trace detail line
                plain = "".join(f.get("t", "") for f in ans)
                ev["detail"] = plain[:80] + "\u2026" if len(plain) > 80 else plain
            else:
                ev["detail"] = ans[:80] + "\u2026" if (ans and len(ans) > 80) else (ans or "escape hatch \u2192 needs_query")

        elif node == "catalog_lookup":
            known = state.get("known_tables") or []
            src = state.get("catalog_source_used") or "db"
            # Expose the source so the UI can keep MCP catalog loads out of the
            # "DB" filter (MCP is not a database query).
            ev["catalog_source"] = src
            src_label = "MCP" if src == "mcp" else "metadata DB"
            detail = f"{len(known)} tables · via {src_label}"
            cache = state.get("catalog_cache")
            if cache:
                detail += f" (cache {cache.upper()})"
            load_ms = state.get("catalog_load_ms")
            if isinstance(load_ms, int):
                detail += f" · {load_ms}ms"
            ev["detail"] = detail

        elif node == "filter_planner":
            plan = state.get("filter_plan") or {}
            planned = [f for f in (plan.get("filters") or []) if isinstance(f, dict)]
            candidates = state.get("filter_candidates") or []
            columns = {f"{c.get('table')}.{c.get('column')}" for c in candidates if isinstance(c, dict)}
            detail = f"{len(planned)} filter(s) planned"
            if columns:
                detail += f" · reverse lookup hit {len(columns)} column(s): {', '.join(sorted(columns)[:3])}"
            elif not planned:
                detail += " · no predicate cue or captured-value hit"
            ev["detail"] = detail

        elif node == "filter_grounder":
            metrics = state.get("filter_metrics") or {}
            resolved = state.get("resolved_filters") or []
            unresolved = state.get("unresolved_filters") or []
            tiers = metrics.get("tiers") or {}
            parts = [f"{len(resolved)} resolved"]
            if unresolved:
                parts.append(f"{len(unresolved)} unverified")
            if state.get("filter_clarification"):
                parts.append(f"asked ({(state.get('filter_clarification') or {}).get('kind')})")
                ev["status"] = "warn"
            if tiers:
                parts.append("tiers " + " ".join(f"{k}:{v}" for k, v in sorted(tiers.items())))
            probes = metrics.get("source_probes")
            if probes:
                parts.append(f"{probes} source probe(s)")
            if metrics.get("elapsed_ms") is not None:
                parts.append(f"{metrics['elapsed_ms']}ms")
            if state.get("plan_assumptions"):
                parts.append(f"{len(state['plan_assumptions'])} disclosed")
            ev["detail"] = " · ".join(parts)
            # Structured copy for dashboards / observability queries.
            ev["filter_grounding"] = {
                "resolved": len(resolved),
                "unverified": len(unresolved),
                "asked": bool(state.get("filter_clarification")),
                "tiers": tiers,
                "source_probes": probes or 0,
                "metadata_reads": metrics.get("metadata_reads", 0),
                "elapsed_ms": metrics.get("elapsed_ms"),
                "evidence": sorted({str(f.get("evidence") or "") for f in resolved if isinstance(f, dict)}),
            }

        elif node == "prompt_builder":
            sp = state.get("structured_prompt") or {}
            conn = (sp.get("connection") or {}).get("display_name", "")
            ev["detail"] = f"system prompt built ({conn})"

        elif node == "sql_generator":
            sql = state.get("generated_sql")
            clarif = state.get("clarification")
            retry = state.get("retry_count", 0)
            prefix = f"retry #{retry} — " if retry else ""
            if sql:
                short = sql.replace("\n", " ")[:100]
                ev["detail"] = prefix + short + ("…" if len(sql) > 100 else "")
                ev["sql"] = sql
            elif clarif:
                ev["detail"] = prefix + f"clarification: {clarif[:80]}"
            else:
                ev["detail"] = prefix + "no SQL or clarification"
                ev["status"] = "warn"

        elif node == "sqlglot_validate":
            err = state.get("sqlglot_error")
            if err:
                ev["detail"] = err[:100]
                ev["status"] = "error"
            else:
                ev["detail"] = "SQL passed validation"

        elif node == "dlp_check":
            if state.get("dlp_blocked"):
                ev["detail"] = state.get("governance_error", "blocked")[:80]
                ev["status"] = "blocked"
            else:
                ev["detail"] = "passed governance check"

        elif node == "execute_query":
            err = state.get("exec_error")
            if err:
                ev["detail"] = err[:100]
                ev["status"] = "error"
                ev["connector_error_type"] = result.get("error_type")
            else:
                rc = result.get("row_count", len(result.get("rows") or []))
                cols = len(result.get("columns") or [])
                ev["detail"] = f"{rc} rows × {cols} cols"

        elif node == "trivial_result_check":
            is_t = state.get("is_trivial")
            ev["detail"] = "trivial — skipping eval" if is_t else "not trivial — running eval"

        elif node == "fused_eval_analytics":
            intent = eval_result.get("answers_intent", "?")
            summary = eval_result.get("summary", "")
            # summary may be a fragment list — flatten to plain text for the trace
            if isinstance(summary, list):
                summary = "".join(f.get("t", "") for f in summary)
            ev["detail"] = f"answers_intent={intent}" + (f" — {summary[:60]}" if summary else "")
            ev["answers_intent"] = intent

        elif node == "feedback_classifier":
            fb = state.get("feedback_type", "?")
            retry = state.get("retry_count", 0)
            ev["detail"] = f"type={fb}  retry={retry}"
            ev["feedback_type"] = fb
            if fb in ("syntax", "exec", "semantic", "missing_table"):
                ev["status"] = "retry"

        elif node == "analysis_planner":
            skill = state.get("analysis_skill")
            params = state.get("analysis_params") or {}
            series = params.get("series") or {}
            if state.get("analysis_clarification"):
                ev["detail"] = f"clarification: {str(state['analysis_clarification'])[:80]}"
            elif skill:
                ev["detail"] = (
                    f"{skill} · {str(series.get('agg', '')).upper()}({series.get('measure_column')}) "
                    f"by {series.get('grain')} on {series.get('table')}"
                )
            else:
                ev["detail"] = "no skill applies — answering with SQL"
            ev["skill"] = skill

        elif node == "analysis_guard":
            guards = state.get("analysis_guard_results") or []
            failed = [g for g in guards if not g.get("passed")]
            if state.get("analysis_confirm_required"):
                ev["detail"] = f"{len(guards)} guards passed · waiting for confirmation"
            elif failed:
                ev["detail"] = f"guard: {failed[0].get('name')} · {str(failed[0].get('detail'))[:70]}"
                ev["status"] = "blocked"
            else:
                span = state.get("analysis_span") or {}
                ev["detail"] = f"{len(guards)} guards passed · {span.get('n', '?')} rows in span"

        elif node == "analysis_sql":
            sql = state.get("generated_sql")
            if sql and not state.get("analysis_error"):
                short = sql.replace("\n", " ")[:100]
                ev["detail"] = short + ("…" if len(sql) > 100 else "")
                ev["sql"] = sql
            else:
                ev["detail"] = str(state.get("analysis_error") or "no SQL")[:100]
                ev["status"] = "error"

        elif node == "analysis_run":
            analysis = state.get("analysis_result") or {}
            if analysis:
                v = analysis.get("validation") or {}
                metric = f" · {v.get('metric')} {v.get('value'):.3f}" if v.get("value") is not None else ""
                ev["detail"] = (
                    f"{analysis.get('method_used')} · {analysis.get('egress', {}).get('rows_sent_to_model', '?')} "
                    f"rows sent{metric}"
                )
                if state.get("low_confidence"):
                    ev["detail"] += " · low confidence"
            elif state.get("analysis_guard_failure"):
                ev["detail"] = str(state["analysis_guard_failure"].get("message"))[:100]
                ev["status"] = "blocked"
            else:
                ev["detail"] = str(state.get("analysis_error") or "failed")[:100]
                ev["status"] = "error"

        elif node == "response_formatter":
            route = state.get("route", "?")
            ev["detail"] = f"route={route}"

        elif node == "save_to_memory":
            qid = state.get("query_id")
            ev["detail"] = f"query_id={str(qid)[:8]}…" if qid else "skipped (no query_id)"

        elif node == "observability_log":
            ev["detail"] = "QUERY_EVENT logged"

        # Attach captured prompt for LLM nodes so the UI can show full text
        if node in node_prompts:
            ev["prompt"] = node_prompts[node]


# ── save_to_memory ────────────────────────────────────────────────────────────


def make_save_to_memory(history_service: ConversationHistoryService, deployment_name: str):
    """Return an async ``save_to_memory`` node."""

    async def save_to_memory(state: AgentState) -> Dict[str, Any]:
        query_id = state.get("query_id")
        if not query_id:
            return {}

        sql = state.get("generated_sql")
        exec_time_ms = state.get("execution_time_ms")
        query_result = state.get("query_result") or {}
        exec_error = state.get("exec_error")
        token_usage = state.get("token_usage") or {}
        llm_latency_ms = state.get("llm_latency_ms") or 0
        start_time = state.get("start_time")
        graph_time_ms = int((time.monotonic() - start_time) * 1000) if start_time else None
        formatted = state.get("formatted_response") or {}
        # A text-only turn (greeting, memory answer, clarification, refusal)
        # has no SQL and no execution error. It still needs a terminal status so
        # the conversation can be restored; the CHECK constraint only allows
        # success/error/timeout/syntax_error/pending, so it is a 'success' with
        # result_kind='text' on the artifact.
        formatter_error = formatted.get("error") if isinstance(formatted, dict) else None
        # A proposal (confirm / clarify / guard) generated SQL for its probe or
        # even fetched the series, but it answered with a message: store it as
        # a text turn so the conversation restores the card, not an empty table.
        if isinstance(formatted, dict) and formatted.get("proposal"):
            sql = None
        text_only = not sql and not exec_error

        try:
            if sql:
                await history_service.update_llm_response(
                    query_id=query_id,
                    generated_sql=sql,
                    llm_model=deployment_name,
                    llm_latency_ms=llm_latency_ms,
                    tokens_used=token_usage.get("total_tokens", 0),
                )

            if exec_error:
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="error",
                    execution_time_ms=exec_time_ms,
                    row_count=0,
                    result_preview=None,
                    error_message=exec_error,
                    graph_time_ms=graph_time_ms,
                )
            elif sql and formatter_error and not (query_result.get("rows") or []):
                # SQL was generated but never produced rows: validation, DLP or
                # governance stopped it. The user saw an error, so the turn must
                # not be recorded (or restored) as a success.
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="error",
                    execution_time_ms=exec_time_ms,
                    row_count=0,
                    result_preview=None,
                    error_message=str(formatter_error)[:4000],
                    graph_time_ms=graph_time_ms,
                )
            elif sql:
                rows = query_result.get("rows") or []
                safe_preview = _coerce_json_safe(rows[:10]) if rows else None
                # Durable artifact: structured summary used to detect and answer
                # follow-up questions about this result (see conversation_history).
                result_artifact = _build_result_artifact(sql, query_result)
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="success",
                    execution_time_ms=exec_time_ms,
                    row_count=len(rows),
                    result_preview=safe_preview,
                    error_message=None,
                    graph_time_ms=graph_time_ms,
                    result_artifact=result_artifact,
                )
            elif text_only:
                await history_service.update_execution(
                    query_id=query_id,
                    execution_status="error" if formatter_error else "success",
                    execution_time_ms=None,
                    row_count=0,
                    result_preview=None,
                    error_message=formatter_error,
                    graph_time_ms=graph_time_ms,
                )

        except Exception:  # noqa: BLE001
            logger.exception(
                "save_to_memory: failed to update history for query_id=%s", query_id
            )

        await _persist_turn_artifact(
            history_service,
            query_id=query_id,
            formatted=formatted if isinstance(formatted, dict) else {},
            sql=sql,
            exec_error=exec_error,
            query_result=query_result,
        )

        return {}

    return save_to_memory


async def _persist_turn_artifact(
    history_service: ConversationHistoryService,
    *,
    query_id: Any,
    formatted: Dict[str, Any],
    sql: Optional[str],
    exec_error: Optional[str],
    query_result: Dict[str, Any],
) -> None:
    """Store the restore payload for this turn (answer, analytics, snapshot).

    Only an explicit allowlist of ``formatted_response`` is persisted; the
    snapshot is all-or-nothing under the configured caps. Best-effort: never
    affects the answer.
    """
    if getattr(history_service, "persistence_enabled", False) is not True:
        return
    try:
        from src.config import settings

        fields = extract_artifact_fields(formatted, has_sql=bool(sql), exec_error=exec_error)
        snapshot = None
        status = SNAPSHOT_NOT_APPLICABLE
        size: Optional[int] = None
        if fields["result_kind"] == RESULT_KIND_TABLE:
            snapshot, status, size, row_count = build_result_snapshot(
                query_result,
                max_rows=settings.CONVERSATION_SNAPSHOT_MAX_ROWS,
                max_bytes=settings.CONVERSATION_SNAPSHOT_MAX_BYTES,
            )
            if snapshot is None:
                logger.info(
                    "conversation_snapshot_skipped query_id=%s rows=%d bytes=%s",
                    query_id, row_count, size,
                    extra={
                        "event": "conversation_snapshot_skipped",
                        "rows": row_count,
                        "bytes": size,
                    },
                )
        analysis = fields.get("analysis")
        proposal = formatted.get("proposal") if isinstance(formatted, dict) else None
        if analysis is None and isinstance(proposal, dict):
            # A pending proposal restores as its card; the id lets the UI resume it.
            analysis = {"proposal": proposal, "status": formatted.get("status")}
        await history_service.upsert_turn_artifact(
            turn_id=query_id,
            result_kind=fields["result_kind"],
            answer=fields["answer"],
            error=fields["error"],
            metrics=fields["metrics"],
            findings=fields["findings"],
            suggestions=fields["suggestions"],
            followups=fields["followups"],
            result_snapshot=snapshot,
            snapshot_status=status,
            snapshot_bytes=size,
            analysis=analysis,
            low_confidence=bool(fields.get("low_confidence")),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "save_to_memory: failed to persist turn artifact for query_id=%s", query_id
        )


# ── observability_log ─────────────────────────────────────────────────────────


def _grounding_event(state: AgentState, *, rows: int) -> Optional[Dict[str, Any]]:
    """Aggregatable filter-grounding fields for the query_completed event."""
    plan = state.get("filter_plan") or {}
    planned = [f for f in (plan.get("filters") or []) if isinstance(f, dict)]
    metrics = state.get("filter_metrics") or {}
    if not planned and not metrics:
        return None
    resolved = [f for f in (state.get("resolved_filters") or []) if isinstance(f, dict)]
    unresolved = state.get("unresolved_filters") or []
    clarification = state.get("filter_clarification") or {}
    executed_ok = (
        state.get("query_result") is not None
        and not state.get("exec_error")
        and not state.get("sqlglot_error")
        and not state.get("dlp_blocked")
        and bool(state.get("generated_sql"))
    )
    return {
        "planned": len(planned),
        "resolved": len(resolved),
        "unverified": len(unresolved),
        "asked": bool(clarification),
        "asked_kind": clarification.get("kind") if clarification else None,
        "evidence": sorted({str(f.get("evidence") or "") for f in resolved}),
        "tiers": metrics.get("tiers") or {},
        "source_probes": metrics.get("source_probes", 0),
        "metadata_reads": metrics.get("metadata_reads", 0),
        "grounding_ms": metrics.get("elapsed_ms"),
        "disclosed": len(state.get("plan_assumptions") or []),
        "user_choice_applied": bool(state.get("filter_choices")),
        "reground_passes": int(state.get("empty_filter_diagnostics") or 0),
        # Zero rows from a query that actually ran: with every filter verified
        # it is a real "no data"; with an unverified literal it is the failure
        # mode grounding exists to remove. Failed/blocked queries do not count.
        "zero_rows": executed_ok and rows == 0,
        "zero_rows_with_unverified": executed_ok and rows == 0 and bool(unresolved),
    }


def observability_log(state: AgentState) -> Dict[str, Any]:
    """Emit a structured event at the end of every graph run.

    The event is logged with its fields as first-class structured extras (folded
    into the JSON log line by ``JsonLogFormatter``) rather than as a JSON string
    stuffed inside ``message`` — so a log aggregator can index ``route``,
    ``event_outcome``, ``total_tokens`` etc. without re-parsing. A compact
    human-readable summary is kept on the message for console/dev logs, and the
    ``QUERY_EVENT`` marker is retained so existing ``grep QUERY_EVENT`` runbooks
    still match.

    The ``event_category`` / ``event_type`` / ``event_outcome`` classification
    fields use backend-agnostic string values (they map cleanly onto ECS or
    OTel ``event.*`` if the log pipeline chooses to).

    Parsing examples (JSON mode — each line is already a JSON object)::

        # Live tail in the container:
        docker logs jeen-insights-api -f | grep QUERY_EVENT

        # Parse with jq:
        docker logs jeen-insights-api | grep query_completed | jq 'select(.event=="query_completed")'
    """
    start = state.get("start_time") or time.monotonic()
    elapsed_ms = int((time.monotonic() - start) * 1000)
    result = state.get("query_result") or {}
    has_error = bool(state.get("error") or state.get("exec_error"))

    event = {
        "event": "query_completed",
        # Event classification (backend-agnostic; maps onto ECS/OTel event.*).
        "event_category": "database",
        "event_type": "error" if has_error else "access",
        "event_outcome": "failure" if has_error else "success",
        "source_key": state.get("source_key"),
        "database_type": state.get("database_type"),
        "route": state.get("route", "?"),
        "retry_count": state.get("retry_count", 0),
        "llm_call_count": state.get("llm_call_count", 0),
        "total_tokens": (state.get("token_usage") or {}).get("total_tokens", 0),
        "input_tokens": (state.get("token_usage") or {}).get("input_tokens", 0),
        "llm_latency_ms": state.get("llm_latency_ms", 0),
        "execution_time_ms": state.get("execution_time_ms"),
        "elapsed_ms": elapsed_ms,
        "has_error": has_error,
        "connector_error_type": result.get("error_type"),
        "query_id": str(state.get("query_id") or ""),
        # Same shape as the persisted node_trace column, so per-node latency is
        # still recoverable from logs alone if a pipeline is added later. This
        # node runs inside the graph, so the three tail nodes are necessarily
        # absent here; the stored column is the complete record.
        "nodes": slim_trace(state.get("trace") or []),
        # Filter grounding outcome, first-class so a log pipeline can chart
        # clarification rate, verification mix, probe count and tier usage
        # (and zero-result rate split by whether the filters were verified).
        "filter_grounding": _grounding_event(state, rows=len(result.get("rows") or [])),
    }

    # First-class structured fields via ``extra`` + a compact summary on the
    # message (so console/dev logs stay readable and ``grep QUERY_EVENT`` works).
    logger.info(
        "QUERY_EVENT query_completed route=%s outcome=%s elapsed_ms=%s total_tokens=%s",
        event["route"],
        event["event_outcome"],
        elapsed_ms,
        event["total_tokens"],
        extra=event,
    )
    return {}
