"""ML-skills branch of the graph: planner → guard → sql → (validate/dlp/execute) → run.

analysis_planner   [llm]   One call binds the question to a registered skill and
                           fills its parameters from the catalog. Ambiguity → a
                           clarification proposal; failure → fall back to SQL.
analysis_guard     [db]    Pre-SQL guards (catalog types + a one-row span probe),
                           fills the analysis window, then either stops for the
                           first-run confirm card, refuses with executable exits,
                           or lets the branch proceed.
analysis_sql       [logic] Deterministic aggregation SQL via the sqlglot builder.
analysis_run       [ml]    Hands the fetched aggregate rows to the AnalysisRunner
                           (post-SQL guards + engine run inside it) and turns the
                           envelope back into an ordinary query_result.

Nothing here computes a statistic; everything numeric happens inside
``src.analysis`` behind the runner boundary.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pandas as pd

from src.agent.analysis_planner import (
    catalog_candidates,
    column_types_map,
    default_window_periods,
    detect_analysis_intent,
    plan_analysis,
)
from src.agent.conversation_artifacts import coerce_json_safe_rows
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent.state import AgentState
from src.agent.llm_service import LangChainLlmService
from src.agent.token_usage import merge_usage
from src.analysis.contracts import (
    AnalysisProposal,
    CohortRequest,
    EntityRequest,
    ExperimentRequest,
    FilterSpec,
    GuardExit,
    GuardResult,
    ParamChip,
    SeriesRequest,
    get_skill,
    method_options,
    parse_params,
)
from src.analysis.guards import max_horizon, series_length
from src.analysis.series import SeriesFrame, floor_to_grain, pandas_freq
from src.analysis.sql_builder import (
    UnsupportedFilter,
    build_cohort_sql,
    build_contribution_sql,
    build_entity_sql,
    build_experiment_sql,
    build_series_sql,
    build_span_probe_sql,
    describe_filters,
)
from src.connectors import SqlRunner

logger = logging.getLogger(__name__)

_SKILL_PHRASE = {
    "anomaly_detection": "an anomaly check", "forecast": "a forecast", "changepoint": "a changepoint search",
    "seasonality": "a seasonality decomposition", "correlation": "a correlation", "contribution": "a contribution analysis",
    "clustering": "a segmentation", "driver_analysis": "a driver analysis", "regression": "a regression",
    "classification": "a classification", "cohort_retention": "a cohort retention analysis",
    "experiment_test": "an A/B test",
}
_GRAIN_ADJ = {"day": "daily", "week": "weekly", "month": "monthly"}


def on_analysis_branch(state: Dict[str, Any]) -> bool:
    return state.get("route") == "needs_analysis" and bool(state.get("analysis_skill"))


# ── analysis_planner ──────────────────────────────────────────────────────────


def make_analysis_planner(router_llm: LangChainLlmService, prompt_loader: PromptLoader, store: Any = None):
    async def analysis_planner(state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        bundle = state.get("metadata_bundle") or {}
        outcome = await plan_analysis(
            question=question,
            columns_text=bundle.get("columns", ""),
            resolved_filters=state.get("resolved_filters") or [],
            llm=router_llm,
            prompt_loader=prompt_loader,
            connection_schema=state.get("connection_schema"),
            connection_catalog=state.get("connection_catalog"),
            skill_hint=detect_analysis_intent(question),
            timeout=state.get("llm_timeout_seconds"),
        )
        updates: Dict[str, Any] = {
            "llm_call_count": (state.get("llm_call_count") or 0) + (1 if outcome.prompt else 0),
            "llm_latency_ms": (state.get("llm_latency_ms") or 0) + outcome.latency_ms,
            "token_usage": merge_usage(state.get("token_usage") or {}, outcome.usage),
            "node_prompts": {**(state.get("node_prompts") or {}), "analysis_planner": outcome.prompt},
            "route_reason": " · ".join(
                part for part in (state.get("route_reason", ""), f"planner: {outcome.reason}" if outcome.reason else "")
                if part
            ),
        }
        if outcome.kind == "params":
            updates.update({
                "analysis_skill": outcome.skill,
                "analysis_params": outcome.params,
                "analysis_clarification": None,
                "analysis_dropped_filters": outcome.dropped_filters,
            })
            logger.info("analysis_planner: skill=%s params=%s", outcome.skill, outcome.params)
        elif outcome.kind == "clarify":
            # Persisted like every other stop: /api/analysis/run resolves the
            # pick server-side from the stored plan, never from client params.
            stored_params = {
                "_plan": outcome.plan,
                "_filters": list(state.get("resolved_filters") or []),
            }
            proposal = AnalysisProposal(
                proposal_id="",
                kind="clarify",
                skill=outcome.skill or "",
                title=get_skill(outcome.skill).title if outcome.skill else "Analysis",
                message=outcome.message,
                params={},
                options=outcome.options,
                tier=get_skill(outcome.skill).tier if outcome.skill else "A",
            )
            if store is not None and outcome.skill:
                pid = await store.create_proposal(
                    user_id=str(state.get("user_id") or ""),
                    source_key=str(state.get("source_key") or ""),
                    session_id=state.get("session_id"),
                    parent_query_id=state.get("query_id"),
                    kind="clarify",
                    skill=outcome.skill,
                    params=stored_params,
                    question=state.get("question"),
                    proposal=proposal.model_dump(mode="json"),
                )
                proposal.proposal_id = pid or ""
                proposal.expires_at = store.expires_at_for_new() if hasattr(store, "expires_at_for_new") else None
            updates.update({
                "analysis_skill": outcome.skill,
                "analysis_clarification": outcome.message,
                "analysis_proposal": proposal.model_dump(mode="json"),
                "answer": outcome.message,
            })
            logger.info("analysis_planner: clarification — %s", outcome.message)
        else:
            # Fall back to the ordinary SQL path; the question is still answerable.
            updates.update({"route": "needs_query", "route_source": "planner_fallback",
                            "analysis_skill": None, "analysis_params": None})
            logger.info("analysis_planner: fallback to needs_query (%s)", outcome.reason)
        return updates

    return analysis_planner


# ── analysis_guard ────────────────────────────────────────────────────────────


def _period_count(start: date, end: date, grain: str) -> int:
    """Number of whole periods in ``[start, end)``."""
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) - pd.Timedelta(days=1), freq=pandas_freq(grain))
    return int(len(idx))


def _shift_periods(ts: pd.Timestamp, grain: str, periods: int) -> pd.Timestamp:
    return ts + periods * pd.tseries.frequencies.to_offset(pandas_freq(grain))


def _estimated_frame(req: SeriesRequest, n: int) -> SeriesFrame:
    """A calendar-only frame so the history guards can speak in numbers before SQL."""
    start = pd.Timestamp(req.start or date.today())
    idx = pd.date_range(start, periods=max(n, 0), freq=pandas_freq(req.grain), name="ts")
    frame = pd.DataFrame({"y": [float("nan")] * len(idx), "observed": [False] * len(idx)}, index=idx)
    return SeriesFrame(frame=frame, grain=req.grain, freq=pandas_freq(req.grain), request=req)


def _chips(skill: str, params: Dict[str, Any], cands) -> List[ParamChip]:
    spec = get_skill(skill)
    if spec.family == "entity":
        entity = params.get("entity") or {}
        tc = cands.get(str(entity.get("table", "")).lower())
        chips = [
            ParamChip(key="entity_key", label="entity", value=entity.get("entity_key"),
                      options=((tc.text_columns + tc.numeric_columns)[:12] if tc else [])),
            ParamChip(key="features", label="features", value=", ".join(entity.get("features") or []), options=[]),
            ParamChip(key="row_cap", label="row cap", value=entity.get("row_cap", 50000), options=[1000, 10000, 50000, 100000]),
        ]
        if skill == "clustering":
            chips.append(ParamChip(key="k", label="segments", value=params.get("k") or "auto", options=["auto", 2, 3, 4, 5, 6, 7, 8]))
            chips.append(ParamChip(key="method", label="model", value=params.get("method", "kmeans"), options=method_options(skill)))
        else:
            chips.append(ParamChip(key="target", label="target", value=entity.get("target"),
                                   options=(tc.numeric_columns[:12] if tc else [])))
            chips.append(ParamChip(key="holdout", label="holdout", value=params.get("holdout", 0.2), options=[0.1, 0.2, 0.3]))
            if skill == "driver_analysis":
                chips.append(ParamChip(key="method", label="model", value=params.get("method", "hgb"),
                                       options=method_options(skill)))
        return chips

    if spec.family == "cohort":
        cohort = params.get("cohort") or {}
        tc = cands.get(str(cohort.get("table", "")).lower())
        return [
            ParamChip(key="entity_key", label="entity", value=cohort.get("entity_key"),
                      options=((tc.text_columns + tc.numeric_columns)[:12] if tc else [])),
            ParamChip(key="cohort_date", label="signup date", value=cohort.get("cohort_date"),
                      options=(tc.date_columns if tc else [])),
            ParamChip(key="activity_date", label="activity date", value=cohort.get("activity_date"),
                      options=(tc.date_columns if tc else [])),
            ParamChip(key="grain", label="grain", value=cohort.get("grain", "month"), options=["day", "week", "month"]),
            ParamChip(key="max_periods", label="periods", value=cohort.get("max_periods", 12), options=[6, 12, 18, 24]),
        ]

    if spec.family == "experiment":
        experiment = params.get("experiment") or {}
        tc = cands.get(str(experiment.get("table", "")).lower())
        return [
            ParamChip(key="group_column", label="arm", value=experiment.get("group_column"),
                      options=(tc.text_columns[:12] if tc else [])),
            ParamChip(key="outcome_column", label="outcome", value=experiment.get("outcome_column"),
                      options=(tc.numeric_columns[:12] if tc else [])),
            ParamChip(key="outcome_type", label="type", value=experiment.get("outcome_type", "binary"),
                      options=["binary", "continuous"]),
            ParamChip(key="control", label="control", value=experiment.get("control") or "auto", options=[]),
            ParamChip(key="confidence", label="confidence", value=params.get("confidence", 0.95),
                      options=[0.90, 0.95, 0.99]),
        ]

    series = params.get("series") or {}
    tc = cands.get(str(series.get("table", "")).lower())
    chips = [
        ParamChip(key="measure_column", label="measure", value=series.get("measure_column"),
                  options=(tc.numeric_columns[:12] if tc else [])),
        ParamChip(key="agg", label="aggregate", value=series.get("agg"), options=["sum", "count", "avg", "min", "max"]),
        ParamChip(key="date_column", label="date", value=series.get("date_column"),
                  options=(tc.date_columns if tc else [])),
    ]
    if spec.family == "contribution":
        chips += [
            ParamChip(key="dimensions", label="dimensions", value=", ".join(params.get("dimensions") or []),
                      options=(tc.text_columns[:12] if tc else [])),
            ParamChip(key="before_start", label="before from", value=params.get("before_start"), options=[]),
            ParamChip(key="before_end", label="before to", value=params.get("before_end"), options=[]),
            ParamChip(key="after_start", label="after from", value=params.get("after_start"), options=[]),
            ParamChip(key="after_end", label="after to", value=params.get("after_end"), options=[]),
            ParamChip(key="top_n", label="top slices", value=params.get("top_n", 10), options=[5, 10, 20]),
        ]
        return chips
    chips += [
        ParamChip(key="grain", label="grain", value=series.get("grain"), options=["day", "week", "month"]),
        ParamChip(key="window", label="window", value=params.get("window"), options=[]),
    ]
    if skill == "anomaly_detection":
        chips.append(ParamChip(key="sensitivity", label="sensitivity", value=params.get("sensitivity", 0.95),
                               options=[0.8, 0.9, 0.95, 0.99]))
        chips.append(ParamChip(key="method", label="model", value=params.get("method", "auto"), options=method_options(skill)))
    elif skill == "forecast":
        chips.append(ParamChip(key="horizon", label="horizon", value=params.get("horizon", 8), options=[]))
        chips.append(ParamChip(key="interval", label="interval", value=params.get("interval", 0.8),
                               options=[0.5, 0.8, 0.9, 0.95]))
        chips.append(ParamChip(key="method", label="model", value=params.get("method", "auto"), options=method_options(skill)))
    elif skill == "changepoint":
        chips.append(ParamChip(key="max_changepoints", label="max breaks", value=params.get("max_changepoints", 5), options=[1, 3, 5, 10]))
    elif skill == "correlation":
        chips.append(ParamChip(key="other_measure_column", label="with", value=params.get("other_measure_column"),
                               options=(tc.numeric_columns[:12] if tc else [])))
        chips.append(ParamChip(key="max_lag", label="max lag", value=params.get("max_lag", 8), options=[0, 4, 8, 13, 26]))
    if series.get("group_by") is not None or (tc and tc.text_columns):
        chips.append(ParamChip(key="group_by", label="split by", value=series.get("group_by") or "none",
                               options=["none"] + (tc.text_columns[:10] if tc else [])))
    return chips


def _egress_summary(skill: str, params: Dict[str, Any], n_est: int, display: str) -> str:
    spec = get_skill(skill)
    if spec.family == "entity":
        entity = params.get("entity") or {}
        cols = [entity.get("entity_key"), *(entity.get("features") or [])] + ([entity.get("target")] if entity.get("target") else [])
        return (
            f"Row-level: reads up to {int(entity.get('row_cap', 50000)):,} {entity.get('table')} rows with "
            f"{len(cols)} columns ({', '.join(str(c) for c in cols)}) on {display}. Those rows are sent to the analysis "
            f"service and the run is audited with this column list."
        )
    if spec.family == "cohort":
        cohort = params.get("cohort") or {}
        return (
            f"SQL counts distinct {cohort.get('entity_key')} per signup cohort and activity {cohort.get('grain', 'month')} "
            f"on {display} (about {n_est} cohort×period totals). Only those counts are sent to the analysis service; "
            f"no {cohort.get('table')} rows are read."
        )
    if spec.family == "experiment":
        experiment = params.get("experiment") or {}
        return (
            f"SQL summarises {experiment.get('outcome_column')} per arm of {experiment.get('group_column')} on {display} "
            f"(one count + two moments per arm). Only those per-arm summaries are sent to the analysis service; "
            f"no {experiment.get('table')} rows are read."
        )
    series = params.get("series") or {}
    label = f"{str(series.get('agg', 'sum')).upper()}({series.get('measure_column')})"
    if spec.family == "contribution":
        return (
            f"SQL totals {label} per slice of {', '.join(params.get('dimensions') or [])} for the two periods on {display} "
            f"(about {n_est} slice totals). Only those totals are sent to the analysis service; no {series.get('table')} rows are read."
        )
    grain = series.get("grain", "week")
    split = f" per {series['group_by']}" if series.get("group_by") else ""
    extra = f" and {str(params.get('other_agg', 'sum')).upper()}({params['other_measure_column']})" if params.get("other_measure_column") else ""
    return (
        f"SQL rolls {label}{extra} up to about {n_est} {_GRAIN_ADJ.get(grain, grain)} totals{split} on {display}. "
        f"Only those rows are sent to the analysis service; no {series.get('table')} rows are read."
    )


def canonicalize_identifiers(skill: str, params: Dict[str, Any], cands: Dict[str, Any], *,
                             connection_schema: Optional[str], connection_catalog: Optional[str]) -> Dict[str, Any]:
    """Rewrite every identifier in ``params`` to the catalog's exact spelling and
    take schema/catalog from the connection.

    The catalog checks below are case-insensitive, but the SQL builder emits
    quoted, case-preserving identifiers — so ``"FACTINTERNETSALES"`` would pass
    the check and still name a different object in Postgres. After this step
    the SQL can only reference objects exactly as the catalog registers them;
    names the catalog does not know are left as-is for the guards to refuse.
    """
    spec = get_skill(skill)
    key = {"entity": "entity", "cohort": "cohort", "experiment": "experiment"}.get(spec.family, "series")
    out = dict(params)
    req = dict(out.get(key) or {})
    tc = cands.get(str(req.get("table") or "").lower())
    if tc is None:
        return out
    col = lambda name: (tc.canonical_column(name) or name) if name and name != "*" else name  # noqa: E731
    req["table"] = tc.name
    req["schema_name"] = tc.schema or connection_schema or None
    req["catalog"] = connection_catalog or None
    for field_name in ("date_column", "measure_column", "group_by", "entity_key", "target",
                       "cohort_date", "activity_date", "group_column", "outcome_column"):
        if req.get(field_name):
            req[field_name] = col(req[field_name])
    if isinstance(req.get("features"), list):
        req["features"] = [col(f) for f in req["features"]]
    if isinstance(req.get("filters"), list):
        # Filters on this table are canonicalised the same way; filters the
        # planner resolved on other tables are dropped by the SQL builder.
        req["filters"] = [
            {**f, "table": tc.name, "column": col(f.get("column"))}
            if isinstance(f, dict) and str(f.get("table") or "").lower() in ("", tc.name.lower()) else f
            for f in req["filters"]
        ]
    out[key] = req
    if out.get("other_measure_column"):
        out["other_measure_column"] = col(out["other_measure_column"])
    if isinstance(out.get("dimensions"), list):
        out["dimensions"] = [col(d) for d in out["dimensions"]]
    return out


def make_analysis_guard(
    sql_runner: SqlRunner,
    store: Any,
    *,
    limiter: Optional[Callable[[str], Awaitable[bool]]] = None,
    max_entity_rows: int = 50_000,
):
    """Return the async ``analysis_guard`` node.

    ``store`` persists proposals and consent (``AnalysisStore`` or the in-memory
    double). ``limiter`` is an awaitable ``user_id -> allowed`` budget check.
    ``max_entity_rows`` clamps a tier-B request's ``row_cap``.
    """

    async def _stop(state: AgentState, proposal: AnalysisProposal, *, kind: str,
                    params: Dict[str, Any], guard_results: List[GuardResult]) -> Dict[str, Any]:
        pid = await store.create_proposal(
            user_id=str(state.get("user_id") or ""),
            source_key=str(state.get("source_key") or ""),
            session_id=state.get("session_id"),
            parent_query_id=state.get("query_id"),
            kind=kind,
            skill=proposal.skill,
            params=params,
            question=state.get("question"),
            proposal=proposal.model_dump(mode="json"),
        ) if store is not None else None
        proposal.proposal_id = pid or ""
        if store is not None and hasattr(store, "expires_at_for_new"):
            proposal.expires_at = store.expires_at_for_new()
        dump = proposal.model_dump(mode="json")
        updates: Dict[str, Any] = {
            "analysis_proposal": dump,
            "analysis_params": params,
            "analysis_guard_results": [g.model_dump(mode="json") for g in guard_results],
            "answer": proposal.message,
        }
        if kind == "guard":
            updates["analysis_guard_failure"] = {
                "message": proposal.message,
                "guard_results": [g.model_dump(mode="json") for g in guard_results if not g.passed],
            }
        else:
            updates["analysis_confirm_required"] = True
        return updates

    async def analysis_guard(state: AgentState) -> Dict[str, Any]:
        skill = state.get("analysis_skill") or ""
        params: Dict[str, Any] = dict(state.get("analysis_params") or {})
        spec = get_skill(skill)
        bundle = state.get("metadata_bundle") or {}
        cands = catalog_candidates(bundle.get("columns", ""))
        column_types = column_types_map(cands)
        display = state.get("connection_display_name") or state.get("source_key") or "the connection"
        override = bool(state.get("analysis_override_guards"))
        results: List[GuardResult] = []

        params = canonicalize_identifiers(
            skill, params, cands,
            connection_schema=state.get("connection_schema"), connection_catalog=state.get("connection_catalog"),
        )
        if spec.family == "entity" and isinstance(params.get("entity"), dict):
            requested = int(params["entity"].get("row_cap") or max_entity_rows)
            params["entity"] = {**params["entity"], "row_cap": max(100, min(requested, max_entity_rows))}
        try:
            typed = parse_params(skill, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("analysis_guard: invalid parameters for %s: %s", skill, exc)
            msg = "The analysis parameters are not valid for this skill; ask the question again."
            return {"analysis_error": msg, "error": msg}

        if spec.family == "entity":
            return await _guard_entity(state, skill, spec, typed, params, cands, display, override)
        if spec.family == "cohort":
            return await _guard_cohort(state, skill, spec, typed, params, cands, display, override, column_types)
        if spec.family == "experiment":
            return await _guard_experiment(state, skill, spec, typed, params, cands, display, override)

        series = typed.series
        tc = cands.get(series.table.lower())

        # ── Pre-SQL catalog guards ────────────────────────────────────────
        date_ok = bool(tc and any(c.lower() == series.date_column.lower() for c in tc.date_columns))
        results.append(GuardResult(
            name="date_column", passed=date_ok, overridable=False,
            detail=(f"{series.table}.{series.date_column} is a date column" if date_ok
                    else f"{series.table}.{series.date_column} is not a registered date column"),
            exits=[GuardExit(kind="patch", label=c, params_patch={"series": {"date_column": c}}, recommended=(i == 0))
                   for i, c in enumerate((tc.date_columns if tc else [])[:6])],
        ))
        measure_ok = series.measure_column == "*" or bool(
            tc and any(c.lower() == series.measure_column.lower() for c in tc.numeric_columns)
        )
        results.append(GuardResult(
            name="measure_numeric", passed=measure_ok, overridable=False,
            detail=(f"{series.measure_label} is numeric" if measure_ok
                    else f"{series.table}.{series.measure_column} is not a registered numeric column"),
            exits=[GuardExit(kind="patch", label=c, params_patch={"series": {"measure_column": c}}, recommended=(i == 0))
                   for i, c in enumerate((tc.numeric_columns if tc else [])[:6])],
        ))
        all_columns = {c.lower() for c in ((tc.date_columns + tc.numeric_columns + tc.text_columns) if tc else [])}
        if skill == "correlation":
            other_ok = bool(tc and any(c.lower() == typed.other_measure_column.lower() for c in tc.numeric_columns))
            results.append(GuardResult(
                name="other_measure_numeric", passed=other_ok, overridable=False,
                detail=(f"{typed.other_label} is numeric" if other_ok
                        else f"{series.table}.{typed.other_measure_column} is not a registered numeric column"),
                exits=[GuardExit(kind="patch", label=c, params_patch={"other_measure_column": c}, recommended=(i == 0))
                       for i, c in enumerate((tc.numeric_columns if tc else [])[:6]) if c.lower() != series.measure_column.lower()],
            ))
        if series.group_by:
            group_ok = series.group_by.lower() in all_columns
            results.append(GuardResult(
                name="group_by_column", passed=group_ok, overridable=False,
                detail=(f"one series per {series.group_by}" if group_ok
                        else f"{series.table}.{series.group_by} is not a registered column"),
                exits=[GuardExit(kind="patch", label="Analyse the total instead", params_patch={"series": {"group_by": None}}, recommended=True)],
            ))
        if spec.family == "contribution":
            missing = [d for d in typed.dimensions if d.lower() not in all_columns]
            results.append(GuardResult(
                name="dimensions", passed=not missing, overridable=False,
                detail=(f"slicing by {', '.join(typed.dimensions)}" if not missing
                        else f"not registered on {series.table}: {', '.join(missing)}"),
                exits=[GuardExit(kind="patch", label=c, params_patch={"dimensions": [c]}, recommended=(i == 0))
                       for i, c in enumerate((tc.text_columns if tc else [])[:6])],
            ))
        failed = [g for g in results if not g.passed]
        if failed:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title,
                message=failed[0].detail + ".",
                params=params, options=failed[0].exits + [GuardExit(kind="answer_with_sql", label="Answer with SQL instead")],
                guard_results=results, tier=spec.tier,
            )
            return await _stop(state, proposal, kind="guard", params=params, guard_results=results)

        # ── Span probe (one row) ──────────────────────────────────────────
        try:
            probe_sql = build_span_probe_sql(
                series, state.get("database_type"),
                connection_schema=state.get("connection_schema"), connection_catalog=state.get("connection_catalog"),
                column_types=column_types,
            )
        except UnsupportedFilter as exc:
            return {"analysis_error": str(exc), "error": f"Cannot express a filter for this analysis: {exc}"}
        t0 = time.monotonic()
        probe = await sql_runner.run_sql(
            probe_sql, limit=1, max_rows=1,
            statement_timeout_ms=state.get("statement_timeout_ms") or 30000,
        )
        probe_ms = int((time.monotonic() - t0) * 1000)
        if probe.get("error"):
            # The driver's message is logged for operators; the user gets a
            # fixed sentence rather than connection or schema internals.
            logger.warning("analysis_guard: span probe failed on %s.%s: %s", series.table, series.date_column, probe["error"])
        row = (probe.get("rows") or [None])[0] if not probe.get("error") else None
        row = dict(row) if row is not None and not isinstance(row, dict) and hasattr(row, "keys") else row
        min_ts = row.get("min_ts") if isinstance(row, dict) else None
        max_ts = row.get("max_ts") if isinstance(row, dict) else None
        n_rows = int(row.get("n") or 0) if isinstance(row, dict) else 0
        span_ok = bool(min_ts and max_ts and n_rows > 0) and not probe.get("error")
        if span_ok:
            span_detail = f"{n_rows:,} rows from {pd.Timestamp(min_ts).date()} to {pd.Timestamp(max_ts).date()}"
        elif probe.get("error"):
            span_detail = f"couldn't inspect {series.table}.{series.date_column} to size the analysis (the probe query failed)"
        else:
            span_detail = f"0 rows of {series.table} match the requested filters"
        results.append(GuardResult(
            name="span", passed=span_ok, overridable=False,
            detail=span_detail,
            exits=[GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True)],
        ))
        span = {"min_ts": str(min_ts) if min_ts else None, "max_ts": str(max_ts) if max_ts else None, "n": n_rows}
        if not span_ok:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title,
                message=results[-1].detail + ".", params=params,
                options=results[-1].exits, guard_results=results, tier=spec.tier,
            )
            updates = await _stop(state, proposal, kind="guard", params=params, guard_results=results)
            updates.update({"analysis_span": span, "execution_time_ms": (state.get("execution_time_ms") or 0) + probe_ms})
            return updates

        grain = series.grain
        first = floor_to_grain(pd.Series([pd.Timestamp(min_ts)]), grain, series.week_start).iloc[0]
        last = floor_to_grain(pd.Series([pd.Timestamp(max_ts)]), grain, series.week_start).iloc[0]

        if spec.family == "contribution":
            # ── Two periods: given, or the last quarter vs the one before ──
            span_end = pd.Timestamp(max_ts).normalize() + pd.Timedelta(days=1)
            if not (typed.after_start and typed.after_end):
                params["after_end"] = span_end.date().isoformat()
                params["after_start"] = (span_end - pd.DateOffset(months=3)).date().isoformat()
            if not (typed.before_start and typed.before_end):
                a_start = pd.Timestamp(params["after_start"])
                params["before_end"] = a_start.date().isoformat()
                params["before_start"] = (a_start - pd.DateOffset(months=3)).date().isoformat()
            n_est = 2 * (typed.top_n * len(typed.dimensions))
            message = (
                f"Reading this as {_SKILL_PHRASE[skill]} of {series.measure_label}: {params['before_start']} → {params['before_end']} "
                f"versus {params['after_start']} → {params['after_end']}, sliced by {', '.join(typed.dimensions)}. "
                "Confirm or adjust before I run it."
            )
            return await _finish(state, skill, spec, params, results, cands, display, span, probe_ms,
                                 low_confidence=False, n_est=n_est, message=message, override=override)

        # ── Fill the analysis window from the probe ───────────────────────
        end_ts = pd.Timestamp(series.end) if series.end else _shift_periods(last, grain, 1)
        window = int(params.get("window") or default_window_periods(grain))
        if series.start:
            start_ts = pd.Timestamp(series.start)
        else:
            start_ts = _shift_periods(end_ts, grain, -window)
            start_ts = max(start_ts, first)
        params["series"] = {**params["series"], "start": start_ts.date().isoformat(), "end": end_ts.date().isoformat()}
        if "window" in spec.params_model.model_fields:
            params["window"] = params.get("window") or window
        n_est = _period_count(start_ts.date(), end_ts.date(), grain)
        req = SeriesRequest(**params["series"])
        est = _estimated_frame(req, n_est)

        # ── Pre-SQL history guards (estimated from the span) ──────────────
        results.append(series_length(est))
        if skill == "forecast":
            results.append(max_horizon(est, int(params.get("horizon", 8))))
        failed = [g for g in results if not g.passed]
        if failed and not override:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title,
                message=_guard_sentence(failed[0], grain),
                params=params, options=failed[0].exits, guard_results=results, tier=spec.tier,
            )
            updates = await _stop(state, proposal, kind="guard", params=params, guard_results=results)
            updates.update({"analysis_span": span, "execution_time_ms": (state.get("execution_time_ms") or 0) + probe_ms})
            return updates
        split = f", one series per {series.group_by}" if series.group_by else ""
        message = (
            f"Reading this as {_SKILL_PHRASE.get(skill, skill)} of {req.measure_label} by {grain} "
            f"over the last {n_est} {grain}s{split}. Confirm or adjust before I run it."
        )
        return await _finish(state, skill, spec, params, results, cands, display, span, probe_ms,
                             low_confidence=bool(failed) and override, n_est=n_est, message=message, override=override)

    async def _finish(state, skill, spec, params, results, cands, display, span, probe_ms, *,
                      low_confidence: bool, n_est: int, message: str, override: bool = False) -> Dict[str, Any]:
        """Shared tail: first-run confirm, budget, then hand over to analysis_sql.

        The budget is charged here and only here — right before an execution —
        so a stop for confirmation or a refused guard never costs a run.
        """
        confirmed = bool(state.get("analysis_confirmed"))
        if not confirmed and store is not None:
            remembered = await store.has_skill_pref(
                user_id=str(state.get("user_id") or ""), source_key=str(state.get("source_key") or ""), skill=skill,
            )
            if not remembered:
                # A guard override chosen before this card must survive the card.
                stored = {**params, "_override_guards": True} if override else params
                proposal = AnalysisProposal(
                    proposal_id="", kind="confirm", skill=skill, title=spec.title, message=message,
                    params=params, chips=_chips(skill, params, cands),
                    guard_results=results, egress_summary=_egress_summary(skill, params, n_est, display),
                    tier=spec.tier, estimated_seconds=spec.estimated_seconds,
                )
                updates = await _stop(state, proposal, kind="confirm", params=stored, guard_results=results)
                updates["analysis_params"] = params
                updates.update({"analysis_span": span, "execution_time_ms": (state.get("execution_time_ms") or 0) + probe_ms})
                return updates

        if limiter is not None:
            allowed = await limiter(str(state.get("user_id") or ""))
            if not allowed:
                msg = "You have reached the hourly limit for analyses. Try again later."
                return {"analysis_error": msg, "error": msg, "analysis_span": span}

        return {
            "analysis_params": params,
            "analysis_guard_results": [g.model_dump(mode="json") for g in results],
            "analysis_span": span,
            "low_confidence": low_confidence,
            "execution_time_ms": (state.get("execution_time_ms") or 0) + probe_ms,
        }

    async def _guard_entity(state, skill, spec, typed, params, cands, display, override) -> Dict[str, Any]:
        """Tier B pre-SQL guards: the key, features and target exist and are typed;
        no span probe (there is no time axis), the row cap bounds the read."""
        entity: EntityRequest = typed.entity
        tc = cands.get(entity.table.lower())
        results: List[GuardResult] = []
        all_columns = {c.lower() for c in ((tc.date_columns + tc.numeric_columns + tc.text_columns) if tc else [])}
        key_ok = entity.entity_key.lower() in all_columns
        results.append(GuardResult(
            name="entity_key", passed=key_ok, overridable=False,
            detail=(f"{entity.table}.{entity.entity_key} identifies the entity" if key_ok
                    else f"{entity.table}.{entity.entity_key} is not a registered column"),
            exits=[GuardExit(kind="patch", label=c, params_patch={"entity": {"entity_key": c}}, recommended=(i == 0))
                   for i, c in enumerate(((tc.text_columns + tc.numeric_columns) if tc else [])[:6])],
        ))
        numeric = {c.lower() for c in (tc.numeric_columns if tc else [])}
        usable = [f for f in entity.features if f.lower() in numeric]
        results.append(GuardResult(
            name="feature_count", passed=len(usable) >= 2, overridable=False,
            detail=f"{len(usable)} numeric features of {len(entity.features)} requested"
                   + (f" (not numeric: {', '.join(f for f in entity.features if f not in usable)})" if len(usable) < len(entity.features) else ""),
            observed=float(len(usable)), required=2.0,
            exits=[GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True)],
        ))
        if skill in ("driver_analysis", "regression", "classification"):
            target_ok = bool(entity.target) and entity.target.lower() in numeric
            results.append(GuardResult(
                name="target_numeric", passed=target_ok, overridable=False,
                detail=(f"{entity.target} is a numeric target" if target_ok
                        else f"{entity.table}.{entity.target} is not a registered numeric column"),
                exits=[GuardExit(kind="patch", label=c, params_patch={"entity": {"target": c}}, recommended=(i == 0))
                       for i, c in enumerate((tc.numeric_columns if tc else [])[:6]) if c not in usable],
            ))
        failed = [g for g in results if not g.passed]
        if failed:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title, message=failed[0].detail + ".",
                params=params, options=failed[0].exits + [GuardExit(kind="answer_with_sql", label="Answer with SQL instead")],
                guard_results=results, tier=spec.tier,
            )
            return await _stop(state, proposal, kind="guard", params=params, guard_results=results)
        params["entity"] = {**params["entity"], "features": usable}
        message = (
            f"Reading this as {_SKILL_PHRASE[skill]} of {entity.table} by {entity.entity_key} on "
            f"{', '.join(usable)}{f' to explain {entity.target}' if entity.target else ''}. This reads row-level data "
            f"(up to {entity.row_cap:,} rows). Confirm or adjust before I run it."
        )
        return await _finish(state, skill, spec, params, results, cands, display,
                             {"min_ts": None, "max_ts": None, "n": None}, 0,
                             low_confidence=False, n_est=entity.row_cap, message=message, override=override)

    async def _guard_cohort(state, skill, spec, typed, params, cands, display, override, column_types) -> Dict[str, Any]:
        """Tier A cohort guards: the key and both date columns exist and are
        typed, and a one-row probe confirms the activity axis has data. The
        cohort-size and history guards run post-SQL inside the runner."""
        cohort: CohortRequest = typed.cohort
        tc = cands.get(cohort.table.lower())
        results: List[GuardResult] = []
        all_columns = {c.lower() for c in ((tc.date_columns + tc.numeric_columns + tc.text_columns) if tc else [])}
        date_cols = {c.lower() for c in (tc.date_columns if tc else [])}
        key_ok = cohort.entity_key.lower() in all_columns
        results.append(GuardResult(
            name="entity_key", passed=key_ok, overridable=False,
            detail=(f"{cohort.table}.{cohort.entity_key} identifies the entity" if key_ok
                    else f"{cohort.table}.{cohort.entity_key} is not a registered column"),
            exits=[GuardExit(kind="patch", label=c, params_patch={"cohort": {"entity_key": c}}, recommended=(i == 0))
                   for i, c in enumerate(((tc.text_columns + tc.numeric_columns) if tc else [])[:6])],
        ))
        for field_name, label in (("cohort_date", "signup"), ("activity_date", "activity")):
            value = getattr(cohort, field_name)
            ok = value.lower() in date_cols
            results.append(GuardResult(
                name=field_name, passed=ok, overridable=False,
                detail=(f"{cohort.table}.{value} is the {label} date" if ok
                        else f"{cohort.table}.{value} is not a registered date column"),
                exits=[GuardExit(kind="patch", label=c, params_patch={"cohort": {field_name: c}}, recommended=(i == 0))
                       for i, c in enumerate((tc.date_columns if tc else [])[:6])],
            ))
        failed = [g for g in results if not g.passed]
        if failed:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title, message=failed[0].detail + ".",
                params=params, options=failed[0].exits + [GuardExit(kind="answer_with_sql", label="Answer with SQL instead")],
                guard_results=results, tier=spec.tier,
            )
            return await _stop(state, proposal, kind="guard", params=params, guard_results=results)

        # ── Span probe (one row) over the activity axis ───────────────────
        probe_req = SeriesRequest(
            table=cohort.table, schema_name=cohort.schema_name, catalog=cohort.catalog,
            date_column=cohort.activity_date, measure_column="*", agg="count",
            grain=cohort.grain, filters=cohort.filters,
        )
        try:
            probe_sql = build_span_probe_sql(
                probe_req, state.get("database_type"),
                connection_schema=state.get("connection_schema"), connection_catalog=state.get("connection_catalog"),
                column_types=column_types,
            )
        except UnsupportedFilter as exc:
            return {"analysis_error": str(exc), "error": f"Cannot express a filter for this analysis: {exc}"}
        t0 = time.monotonic()
        probe = await sql_runner.run_sql(
            probe_sql, limit=1, max_rows=1, statement_timeout_ms=state.get("statement_timeout_ms") or 30000,
        )
        probe_ms = int((time.monotonic() - t0) * 1000)
        if probe.get("error"):
            logger.warning("analysis_guard: cohort span probe failed on %s.%s: %s",
                           cohort.table, cohort.activity_date, probe["error"])
        row = (probe.get("rows") or [None])[0] if not probe.get("error") else None
        row = dict(row) if row is not None and not isinstance(row, dict) and hasattr(row, "keys") else row
        min_ts = row.get("min_ts") if isinstance(row, dict) else None
        max_ts = row.get("max_ts") if isinstance(row, dict) else None
        n_rows = int(row.get("n") or 0) if isinstance(row, dict) else 0
        span_ok = bool(min_ts and max_ts and n_rows > 0) and not probe.get("error")
        results.append(GuardResult(
            name="span", passed=span_ok, overridable=False,
            detail=(f"{n_rows:,} rows from {pd.Timestamp(min_ts).date()} to {pd.Timestamp(max_ts).date()}" if span_ok
                    else f"0 rows of {cohort.table} match the requested filters"),
            exits=[GuardExit(kind="answer_with_sql", label="Answer with SQL instead", recommended=True)],
        ))
        span = {"min_ts": str(min_ts) if min_ts else None, "max_ts": str(max_ts) if max_ts else None, "n": n_rows}
        if not span_ok:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title,
                message=results[-1].detail + ".", params=params,
                options=results[-1].exits, guard_results=results, tier=spec.tier,
            )
            updates = await _stop(state, proposal, kind="guard", params=params, guard_results=results)
            updates.update({"analysis_span": span, "execution_time_ms": (state.get("execution_time_ms") or 0) + probe_ms})
            return updates

        n_cohorts_est = max(1, _period_count(pd.Timestamp(min_ts).date(), pd.Timestamp(max_ts).date(), cohort.grain))
        n_est = n_cohorts_est * min(cohort.max_periods, n_cohorts_est)
        message = (
            f"Reading this as {_SKILL_PHRASE[skill]} of {cohort.table}: distinct {cohort.entity_key} by signup "
            f"{cohort.grain} tracked for up to {cohort.max_periods} {cohort.grain}s. Confirm or adjust before I run it."
        )
        return await _finish(state, skill, spec, params, results, cands, display, span, probe_ms,
                             low_confidence=False, n_est=n_est, message=message, override=override)

    async def _guard_experiment(state, skill, spec, typed, params, cands, display, override) -> Dict[str, Any]:
        """Tier A A/B test guards: the arm column exists and is categorical and the
        outcome column exists and is numeric. Arm count and per-arm size are
        checked post-SQL inside the runner (they need the aggregated counts)."""
        exp_req: ExperimentRequest = typed.experiment
        tc = cands.get(exp_req.table.lower())
        results: List[GuardResult] = []
        all_columns = {c.lower() for c in ((tc.date_columns + tc.numeric_columns + tc.text_columns) if tc else [])}
        numeric = {c.lower() for c in (tc.numeric_columns if tc else [])}
        group_ok = exp_req.group_column.lower() in all_columns
        results.append(GuardResult(
            name="group_column", passed=group_ok, overridable=False,
            detail=(f"{exp_req.table}.{exp_req.group_column} splits the arms" if group_ok
                    else f"{exp_req.table}.{exp_req.group_column} is not a registered column"),
            exits=[GuardExit(kind="patch", label=c, params_patch={"experiment": {"group_column": c}}, recommended=(i == 0))
                   for i, c in enumerate((tc.text_columns if tc else [])[:6])],
        ))
        outcome_ok = exp_req.outcome_column.lower() in numeric
        results.append(GuardResult(
            name="outcome_numeric", passed=outcome_ok, overridable=False,
            detail=(f"{exp_req.outcome_column} is a numeric outcome" if outcome_ok
                    else f"{exp_req.table}.{exp_req.outcome_column} is not a registered numeric column"),
            exits=[GuardExit(kind="patch", label=c, params_patch={"experiment": {"outcome_column": c}}, recommended=(i == 0))
                   for i, c in enumerate((tc.numeric_columns if tc else [])[:6]) if c.lower() != exp_req.group_column.lower()],
        ))
        failed = [g for g in results if not g.passed]
        if failed:
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title, message=failed[0].detail + ".",
                params=params, options=failed[0].exits + [GuardExit(kind="answer_with_sql", label="Answer with SQL instead")],
                guard_results=results, tier=spec.tier,
            )
            return await _stop(state, proposal, kind="guard", params=params, guard_results=results)
        kind_word = "conversion rate" if exp_req.outcome_type == "binary" else "average"
        message = (
            f"Reading this as {_SKILL_PHRASE[skill]}: comparing the {kind_word} of {exp_req.outcome_column} "
            f"across arms of {exp_req.group_column} on {exp_req.table}"
            f"{f' (control: {exp_req.control})' if exp_req.control else ''}. Confirm or adjust before I run it."
        )
        return await _finish(state, skill, spec, params, results, cands, display,
                             {"min_ts": None, "max_ts": None, "n": None}, 0,
                             low_confidence=False, n_est=2, message=message, override=override)

    return analysis_guard


def _guard_sentence(guard: GuardResult, grain: str) -> str:
    return f"{guard.detail[0].upper()}{guard.detail[1:]}."


# ── analysis_sql ──────────────────────────────────────────────────────────────


def redact_filter_values(params: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``params`` with every filter's ``value`` replaced by its shape
    (type and, for lists, count). Column and operator stay: they are schema,
    not data. Used for the audit trail and for what leaves for the sandbox."""
    out = json.loads(json.dumps(params, default=str))
    for key in ("series", "entity", "cohort", "experiment"):
        req = out.get(key)
        if not isinstance(req, dict):
            continue
        redacted = []
        for f in req.get("filters") or []:
            if not isinstance(f, dict):
                continue
            value = f.get("value")
            shape = f"<{len(value)} values>" if isinstance(value, (list, tuple)) else f"<{type(value).__name__}>"
            redacted.append({**f, "value": shape})
        req["filters"] = redacted
    return out


def analysis_row_cap(skill: str, params: Dict[str, Any], *, max_series_rows: int, max_entity_rows: int) -> int:
    """Rows an analysis may read: the tier-A aggregate cap, or the entity
    request's own ``row_cap`` clamped to the tier-B deployment limit."""
    if get_skill(skill).family == "entity":
        requested = int((params.get("entity") or {}).get("row_cap") or max_entity_rows)
        return max(1, min(requested, max_entity_rows))
    return max(1, int(max_series_rows))


def make_analysis_sql(*, max_series_rows: int = 1500, max_entity_rows: int = 50_000):
    """Return the ``analysis_sql`` node.

    Besides the SQL it sets the fetch ``limit`` for ``execute_query`` to
    ``cap + 1``: the shared executor otherwise defaults to 100 rows, which
    would silently truncate every series, and the extra row lets
    ``analysis_run`` tell a capped result from a complete one.
    """

    def analysis_sql(state: AgentState) -> Dict[str, Any]:
        params = state.get("analysis_params") or {}
        skill = state.get("analysis_skill") or ""
        try:
            sql = _build_analysis_sql(state, skill, params)
        except UnsupportedFilter as exc:
            return {"analysis_error": str(exc), "error": f"Cannot express a filter for this analysis: {exc}"}
        except Exception:  # noqa: BLE001
            logger.exception("analysis_sql: builder failed")
            msg = "Could not build the aggregation query for this analysis."
            return {"analysis_error": msg, "error": msg}
        cap = analysis_row_cap(skill, params, max_series_rows=max_series_rows, max_entity_rows=max_entity_rows)
        logger.info("analysis_sql (cap %d): %s", cap, sql)
        return {"generated_sql": sql, "retry_count": 0, "error_context": None,
                "limit": cap + 1, "max_result_rows": cap + 1}

    return analysis_sql


def _build_analysis_sql(state: AgentState, skill: str, params: Dict[str, Any]) -> str:
    spec = get_skill(skill)
    bundle = state.get("metadata_bundle") or {}
    column_types = column_types_map(catalog_candidates(bundle.get("columns", "")))
    common = dict(
        database_type=state.get("database_type"),
        connection_schema=state.get("connection_schema"),
        connection_catalog=state.get("connection_catalog"),
        column_types=column_types,
    )
    if spec.family == "entity":
        return build_entity_sql(EntityRequest(**(params.get("entity") or {})), **common)
    if spec.family == "cohort":
        return build_cohort_sql(CohortRequest(**(params.get("cohort") or {})), **common)
    if spec.family == "experiment":
        return build_experiment_sql(ExperimentRequest(**(params.get("experiment") or {})), **common)
    if spec.family == "contribution":
        req = SeriesRequest(**(params.get("series") or {}))
        return build_contribution_sql(
            req, list(params.get("dimensions") or []),
            before=(params["before_start"], params["before_end"]),
            after=(params["after_start"], params["after_end"]),
            **common,
        )
    req = SeriesRequest(**(params.get("series") or {}))
    extra = None
    if skill == "correlation":
        extra = [(str(params.get("other_agg") or "sum"), str(params["other_measure_column"]), "value2")]
    return build_series_sql(req, state.get("database_type"),
                            connection_schema=common["connection_schema"],
                            connection_catalog=common["connection_catalog"],
                            column_types=column_types, extra_measures=extra)


# ── analysis_run ──────────────────────────────────────────────────────────────


def make_analysis_run(
    runner_provider: Callable[[], Any],
    store: Any,
    *,
    audit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_series_rows: int = 1500,
    max_entity_rows: int = 50_000,
):
    """Return the async ``analysis_run`` node.

    ``runner_provider`` returns the configured ``AnalysisRunner`` (or None when
    skills are disabled for this deployment); ``audit`` receives one dict per
    run (skill, redacted params, rows sent, columns, engine, outcome — never
    data values, including filter values).
    """

    async def analysis_run(state: AgentState) -> Dict[str, Any]:
        skill = state.get("analysis_skill") or ""
        params = dict(state.get("analysis_params") or {})
        runner = runner_provider()
        if runner is None:
            msg = "ML skills are not available on this deployment (no analysis runner is configured)."
            return {"analysis_error": msg, "error": msg}
        result = state.get("query_result") or {}
        rows = coerce_json_safe_rows(result.get("rows") or [])
        columns = [str(c) for c in (result.get("columns") or [])] or (list(rows[0].keys()) if rows and isinstance(rows[0], dict) else [])
        spec = get_skill(skill)
        row_limit = analysis_row_cap(skill, params, max_series_rows=max_series_rows, max_entity_rows=max_entity_rows)
        # analysis_sql fetched cap + 1, so either signal means the read was incomplete;
        # an engine must never see a silently truncated input.
        if len(rows) > row_limit or result.get("truncated"):
            msg = (f"The query returned more than {row_limit:,} rows (the limit for this analysis). "
                   + ("Use a coarser grain or a shorter window." if spec.family != "entity" else "Add a filter or lower the row cap."))
            return {"analysis_error": msg, "error": msg}

        filters = (params.get("series") or params.get("entity") or params.get("cohort")
                   or params.get("experiment") or {}).get("filters") or []
        context = {
            "sql": state.get("generated_sql"),
            "query_ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "filters_summary": describe_filters(FilterSpec(**f) for f in filters),
            "low_confidence": bool(state.get("low_confidence")),
            "runner": getattr(runner, "name", "unknown"),
            # The newest source timestamp from the span probe: lets the series
            # preparation prove, not guess, that the last period is incomplete.
            "data_end": (state.get("analysis_span") or {}).get("max_ts"),
            # Only used by the sandbox client to mint its token; never sent onward.
            "user_id": str(state.get("user_id") or ""),
        }
        # SQL has already applied the filters; the engine needs only their shape.
        engine_params = redact_filter_values(params)
        t0 = time.monotonic()
        outcome = await runner.run(
            skill, engine_params, {"columns": columns, "rows": rows},
            override_guards=bool(state.get("analysis_override_guards")), context=context,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        pre_results = list(state.get("analysis_guard_results") or [])

        audit_event: Dict[str, Any] = {
            "skill": skill, "params": engine_params, "rows_sent": len(rows), "columns": columns,
            "runner": context["runner"], "outcome": outcome.status, "elapsed_ms": elapsed,
            "user_id": state.get("user_id"), "source_key": state.get("source_key"), "query_id": str(state.get("query_id") or ""),
        }

        if outcome.status == "guard_failed":
            failed = [g for g in outcome.guard_results if not g.passed]
            proposal = AnalysisProposal(
                proposal_id="", kind="guard", skill=skill, title=spec.title,
                message=_guard_sentence(failed[0], "") if failed else "The data did not pass its guards.",
                params=params, options=(failed[0].exits if failed else []),
                guard_results=outcome.guard_results, tier=spec.tier,
            )
            pid = await store.create_proposal(
                user_id=str(state.get("user_id") or ""), source_key=str(state.get("source_key") or ""),
                session_id=state.get("session_id"), parent_query_id=state.get("query_id"),
                kind="guard", skill=skill, params=params, question=state.get("question"),
                proposal=proposal.model_dump(mode="json"),
            ) if store is not None else None
            proposal.proposal_id = pid or ""
            if audit:
                await audit(audit_event)
            return {
                "analysis_proposal": proposal.model_dump(mode="json"),
                "analysis_guard_failure": {"message": proposal.message,
                                           "guard_results": [g.model_dump(mode="json") for g in failed]},
                "analysis_guard_results": pre_results + [g.model_dump(mode="json") for g in outcome.guard_results],
                "answer": proposal.message,
                # The aggregate rows are not a result for this turn.
                "query_result": {"columns": [], "rows": [], "row_count": 0},
                "execution_time_ms": (state.get("execution_time_ms") or 0) + elapsed,
            }
        if outcome.status != "ok" or outcome.envelope is None:
            msg = outcome.error or "analysis failed"
            audit_event["error"] = msg[:300]
            if audit:
                await audit(audit_event)
            return {"analysis_error": msg, "error": msg,
                    "execution_time_ms": (state.get("execution_time_ms") or 0) + elapsed}

        env = outcome.envelope
        env.guard_results = [GuardResult.model_validate(g) for g in pre_results] + env.guard_results
        audit_event.update({"engine": env.engine.model_dump(), "method_used": env.method_used,
                            "low_confidence": env.low_confidence})
        if audit:
            await audit(audit_event)
        logger.info("analysis_run: %s ok — %s (%dms, %d rows)", skill, env.method_used, elapsed, len(env.rows))
        return {
            "analysis_result": env.model_dump(mode="json"),
            "analysis_definition": _definition(state, skill, params, env.egress.rows_sent_to_model),
            "analysis_guard_results": [g.model_dump(mode="json") for g in env.guard_results],
            "low_confidence": env.low_confidence,
            "query_result": {"columns": env.columns, "rows": env.rows, "row_count": len(env.rows)},
            "answer": env.headline,
            "execution_time_ms": (state.get("execution_time_ms") or 0) + elapsed,
        }

    return analysis_run


def _definition(state: AgentState, skill: str, params: Dict[str, Any], rows_sent: int) -> Optional[Dict[str, Any]]:
    """The finished run's setup as the confirm card would show it.

    The same chips (measure, date column, grain, window, method, …) with the
    values that actually ran, so the answer pane can offer "Edit setup" and
    re-run with a patch. Best effort: a missing catalog costs the card, never
    the result.
    """
    try:
        bundle = state.get("metadata_bundle") or {}
        cands = catalog_candidates(bundle.get("columns", ""))
        chips = _chips(skill, params, cands)
        display = str(state.get("connection_display_name") or state.get("source_key") or "")
        return {
            "chips": [c.model_dump(mode="json") for c in chips],
            "egress_summary": _egress_summary(skill, params, int(rows_sent or 0), display),
        }
    except Exception:  # noqa: BLE001
        logger.debug("analysis_run: could not build the setup card", exc_info=True)
        return None
