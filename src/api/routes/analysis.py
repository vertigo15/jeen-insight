"""ML skills endpoints: resume a proposal, re-run a turn, manage consent.

``POST /api/analysis/run`` is the only way a confirm card, a clarification pick
or a guard exit turns into an analysis. The proposal id is the single source
of ``{skill, params}``; the browser adds an allowlisted parameter patch. The
run re-enters the same compiled graph with ``analysis_confirmed`` and creates
a child turn (``parent_query_id``) in the same conversation.

``POST /api/analysis/rerun`` re-runs a completed ML turn with a structured
patch or a natural-language instruction, appending a child turn. It never
mutates the parent.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError

from src.api import state as api_state
from src.api.concurrency import ConcurrencyLimitExceeded, query_limiter
from src.api.dependencies import get_history_service, get_principal, resolve_agent
from src.api.models import (
    AnalysisChartRequest,
    AnalysisRerunRequest,
    AnalysisRunRequest,
    GenerateChartResponse,
    QueryResponse,
    SkillPrefPatch,
)
from src.api.result_cache import result_cache
from src.analysis.contracts import CONTRACT_VERSION, SKILLS, ChartSpec, get_skill, merge_params_patch
from src.config import settings
from src.security.internal_auth import Principal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _store():
    store = getattr(api_state, "analysis_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="ML skills are not initialised")
    return store


def _require_enabled() -> None:
    if not settings.ML_SKILLS_ENABLED:
        raise HTTPException(status_code=404, detail="ML skills are disabled on this deployment")


def _strip_private(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop server-only bookkeeping keys (``_plan``, ``_override_guards`` …) before validation."""
    return {k: v for k, v in (params or {}).items() if not str(k).startswith("_")}


def _param_error(exc: ValidationError) -> Dict[str, Any]:
    """A 422 body the setup card can place on the offending field.

    ``field`` is the chip key — the last element of the pydantic location, so a
    nested ``('series', 'grain')`` becomes ``grain``. Cross-field validators
    have no usable location and leave ``field`` null; the card then shows the
    message at card level.
    """
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(part) for part in (first.get("loc") or ())]
    field = loc[-1] if loc and not loc[-1].isdigit() else (loc[-2] if len(loc) > 1 else None)
    return {"message": f"Invalid parameter: {first.get('msg')}", "field": field, "loc": loc}


def _reset_range_if_window_changed(params: Dict[str, Any], patch: Dict[str, Any]) -> None:
    """A changed window or grain needs a fresh history range from the span probe.

    The guard keeps an existing ``series.start``/``end`` (a stored confirm card
    already carries the range it was shown with), so without this a wider
    window typed on a card, or a "Look back N periods" guard exit, would leave
    the range exactly where it was.
    """
    patch = patch or {}
    changed = bool(patch.get("grain") or (patch.get("series") or {}).get("grain") or "window" in patch)
    if changed and isinstance(params.get("series"), dict):
        params["series"]["start"] = None
        params["series"]["end"] = None


async def _resolve_clarification(agent: Any, proposal: Dict[str, Any], patch: Dict[str, Any], connection: str) -> Dict[str, Any]:
    """Turn a clarification pick into full params by re-validating the stored
    plan with the pick applied. Nothing from the client becomes a parameter
    unless the catalog says it exists."""
    from src.agent.analysis_planner import apply_clarification, build_params_from_plan, catalog_candidates  # noqa: PLC0415

    stored = proposal.get("params") or {}
    plan = apply_clarification(stored.get("_plan") or {}, patch or {})
    plan.setdefault("skill", proposal["skill"])
    try:
        bundle = await agent.metadata_loader.load_all(connection)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="The catalog is unavailable; try again shortly.")
    cands = catalog_candidates((bundle or {}).get("columns", ""))
    outcome = build_params_from_plan(
        plan, cands, resolved_filters=list(stored.get("_filters") or []),
        connection_schema=getattr(agent.connection, "db_schema", None),
        connection_catalog=getattr(agent.connection, "connection_catalog", None),
    )
    if outcome.kind != "params" or not outcome.params:
        detail = outcome.message or outcome.reason or "the pick did not resolve the question"
        raise HTTPException(status_code=409, detail=f"Still ambiguous — {detail}. Ask the question again.")
    return {k: v for k, v in outcome.params.items() if not str(k).startswith("_")}


async def _run_confirmed(
    *,
    agent: Any,
    principal: Principal,
    connection: str,
    question: str,
    skill: str,
    params: Dict[str, Any],
    session_id: Optional[UUID],
    parent_query_id: Optional[UUID],
    override_guards: bool,
    eval_analytics: Optional[bool],
    llm_timeout: Optional[int],
    confirmed: bool = True,
) -> Dict[str, Any]:
    user_id = principal.user_id
    try:
        await query_limiter.acquire(user_id)
    except ConcurrencyLimitExceeded:
        raise HTTPException(status_code=429, detail="Too many concurrent queries. Please wait for the current one to finish.")
    try:
        result = await agent.process_confirmed_analysis(
            question=question,
            skill=skill,
            params=params,
            session_id=session_id,
            user_context={"user_id": user_id},
            parent_query_id=parent_query_id,
            override_guards=override_guards,
            confirmed=confirmed,
            eval_analytics=eval_analytics,
            llm_timeout=llm_timeout,
        )
        try:
            result_cache.put(user_id=user_id, connection=connection, query_id=result.get("query_id"),
                             dataset=result.get("results"))
        except Exception:  # noqa: BLE001
            logger.debug("result_cache put failed", exc_info=True)
        return result
    finally:
        await query_limiter.release(user_id)


@router.post("/run", response_model=QueryResponse)
async def run_analysis(request: AnalysisRunRequest, principal: Principal = Depends(get_principal)):
    """Resume a persisted proposal with an allowlisted parameter patch."""
    _require_enabled()
    store = _store()
    user_id = principal.user_id

    if request.idempotency_key:
        prior = await store.find_by_idempotency_key(request.idempotency_key, user_id=user_id, source_key=request.connection)
        if prior and prior.get("query_id"):
            raise HTTPException(
                status_code=409,
                detail={"message": "This run was already executed.", "query_id": prior["query_id"]},
            )

    proposal = await store.get_proposal(request.proposal_id, user_id=user_id, source_key=request.connection)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found for this user and connection")
    if proposal["expired"]:
        raise HTTPException(status_code=410, detail="This proposal has expired; ask the question again.")
    if proposal["consumed"]:
        raise HTTPException(
            status_code=409,
            detail={"message": "This proposal was already run.", "query_id": proposal.get("consumed_query_id")},
        )
    if proposal.get("contract_version") != CONTRACT_VERSION:
        raise HTTPException(status_code=409, detail="The analysis contract changed; ask the question again.")

    skill = proposal["skill"]
    try:
        spec = get_skill(skill)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown skill {skill!r}")

    session_id = request.session_id or proposal.get("session_id")
    if session_id and request.session_id and proposal.get("session_id") and request.session_id != proposal["session_id"]:
        raise HTTPException(status_code=409, detail="Proposal belongs to a different conversation")
    if session_id:
        history = get_history_service()
        if not await history.conversation_belongs_to_user(session_id=session_id, user_id=user_id, source_key=request.connection):
            raise HTTPException(status_code=404, detail="Session not found for this user")

    agent = await resolve_agent(request.connection)

    stored_params = dict(proposal.get("params") or {})
    kind = proposal.get("kind")
    if kind == "clarify":
        # The pick completes the stored plan; a completed plan is not yet consented.
        params = await _resolve_clarification(agent, proposal, request.params_patch or {}, request.connection)
        confirmed = False
    else:
        base_params = _strip_private(stored_params)
        if not (base_params.get("series") or base_params.get("entity")):
            raise HTTPException(status_code=409, detail="This proposal carries no runnable parameters; ask the question again.")
        try:
            typed = merge_params_patch(skill, base_params, request.params_patch or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_param_error(exc))
        params = typed.model_dump(mode="json")
        _reset_range_if_window_changed(params, request.params_patch or {})
        # A guard exit re-runs the guard and then still asks for consent on a
        # first run; the confirm card itself is the consent.
        confirmed = kind == "confirm"
    # An override chosen on a guard card before the confirm card survives it.
    override_guards = bool(request.override_guards or stored_params.get("_override_guards"))

    # Single execution slot: claim before running so a duplicate click or a
    # replayed request can never start a second analysis.
    claim = await store.claim_proposal(request.proposal_id, user_id=user_id, idempotency_key=request.idempotency_key)
    if claim == "consumed":
        raise HTTPException(status_code=409, detail={"message": "This proposal was already run.", "query_id": proposal.get("consumed_query_id")})
    if claim == "expired":
        raise HTTPException(status_code=410, detail="This proposal has expired; ask the question again.")
    if claim == "duplicate_key":
        raise HTTPException(status_code=409, detail={"message": "This run was already executed."})
    if claim != "claimed":
        raise HTTPException(status_code=404, detail="Proposal not found for this user and connection")

    # "Don't ask again" is only meaningful on the card that showed the egress notice.
    if request.remember and kind == "confirm":
        await store.set_skill_pref(user_id=user_id, source_key=request.connection, skill=skill, remember=True)

    try:
        result = await _run_confirmed(
            agent=agent, principal=principal, connection=request.connection,
            question=proposal.get("question") or spec.title,
            skill=skill, params=params, session_id=session_id,
            parent_query_id=proposal.get("parent_query_id"),
            override_guards=override_guards, confirmed=confirmed,
            eval_analytics=request.eval_analytics, llm_timeout=request.llm_timeout,
        )
    except Exception:
        await store.release_proposal(request.proposal_id, user_id=user_id)
        raise
    await store.record_result(request.proposal_id, user_id=user_id, query_id=result.get("query_id"))
    return QueryResponse(**result)


@router.post("/rerun", response_model=QueryResponse)
async def rerun_analysis(request: AnalysisRerunRequest, principal: Principal = Depends(get_principal)):
    """Re-run a completed ML turn with adjusted parameters as a new child turn."""
    rerun_started = time.monotonic()
    _require_enabled()
    store = _store()
    user_id = principal.user_id
    history = get_history_service()

    if request.idempotency_key:
        prior = await store.find_by_idempotency_key(request.idempotency_key, user_id=user_id, source_key=request.connection)
        if prior and prior.get("query_id"):
            raise HTTPException(status_code=409, detail={"message": "This run was already executed.", "query_id": prior["query_id"]})

    parent = await history.get_turn_analysis(turn_id=request.parent_query_id, user_id=user_id, source_key=request.connection)
    if parent is None or not parent.get("analysis"):
        raise HTTPException(status_code=404, detail="No analysis found on that turn for this user and connection")
    if parent.get("session_id") and parent["session_id"] != request.session_id:
        raise HTTPException(status_code=409, detail="Turn belongs to a different conversation")

    analysis = parent["analysis"]
    skill = str(analysis.get("skill") or "")
    base_params = dict(analysis.get("params") or {})
    if skill not in SKILLS or not (base_params.get("series") or base_params.get("entity")):
        raise HTTPException(status_code=409, detail="That turn is not a re-runnable analysis")

    patch: Dict[str, Any] = dict(request.params_patch or {})
    instruction_ms = 0
    if request.instruction and not patch:
        agent = await resolve_agent(request.connection)
        instruction_started = time.monotonic()
        patch = await _patch_from_instruction(agent, skill, base_params, request.instruction, parent.get("question") or "")
        instruction_ms = int((time.monotonic() - instruction_started) * 1000)
        if not patch:
            raise HTTPException(status_code=422, detail="I couldn't turn that instruction into a parameter change. Try the chips instead.")
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change: provide params_patch or an instruction")
    try:
        typed = merge_params_patch(skill, base_params, patch)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_param_error(exc))
    params = typed.model_dump(mode="json")
    _reset_range_if_window_changed(params, patch)

    agent = await resolve_agent(request.connection)
    from src.agent.analysis_planner import diff_params  # noqa: PLC0415

    question = parent.get("question") or get_skill(skill).title
    if request.instruction:
        question = f"{question} — {request.instruction.strip()}"

    # The re-run record is created already consumed and carries the idempotency
    # key, so a replayed request collides here before anything executes.
    proposal_id = await store.create_proposal(
        user_id=user_id, source_key=request.connection, session_id=request.session_id,
        parent_query_id=request.parent_query_id, kind="rerun", skill=skill, params=params,
        question=question, proposal={"kind": "rerun", "diff": diff_params(base_params, params)},
        idempotency_key=request.idempotency_key, consumed=True,
    )
    if proposal_id is None and request.idempotency_key and getattr(store, "schema_ready", True):
        raise HTTPException(status_code=409, detail={"message": "This run was already executed."})
    try:
        analysis_started = time.monotonic()
        result = await _run_confirmed(
            agent=agent, principal=principal, connection=request.connection, question=question,
            skill=skill, params=params, session_id=request.session_id, parent_query_id=request.parent_query_id,
            override_guards=request.override_guards, eval_analytics=request.eval_analytics, llm_timeout=request.llm_timeout,
        )
        analysis_ms = int((time.monotonic() - analysis_started) * 1000)
    except Exception:
        if proposal_id:
            await store.release_proposal(UUID(proposal_id), user_id=user_id)
        raise
    if proposal_id:
        await store.record_result(UUID(proposal_id), user_id=user_id, query_id=result.get("query_id"))
    if isinstance(result.get("analysis"), dict):
        result["analysis"]["param_diff"] = diff_params(base_params, params)
    trace = result.get("trace") if isinstance(result.get("trace"), list) else []
    stage_nodes = {
        "guard_ms": {"analysis_guard"},
        "query_build_ms": {"analysis_sql"},
        "data_extraction_ms": {"execute_query"},
        "ml_execution_ms": {"analysis_run"},
        "narration_ms": {"fused_eval_analytics"},
    }
    stages_ms = {
        stage: sum(
            int(event.get("elapsed_ms") or 0)
            for event in trace
            if isinstance(event, dict) and event.get("node") in nodes
        )
        for stage, nodes in stage_nodes.items()
    }
    metrics = dict(result.get("metrics") or {})
    metrics["analysis_rerun"] = {
        "instruction_ms": instruction_ms,
        "analysis_ms": analysis_ms,
        "total_ms": int((time.monotonic() - rerun_started) * 1000),
        "structured_patch": bool(request.params_patch),
        "stages_ms": stages_ms,
    }
    result["metrics"] = metrics
    return QueryResponse(**result)


async def _patch_from_instruction(agent: Any, skill: str, base_params: Dict[str, Any], instruction: str, question: str) -> Dict[str, Any]:
    """Ask the planner to re-plan with the instruction and diff the result into a patch."""
    from src.agent.analysis_planner import diff_params, plan_analysis  # noqa: PLC0415

    registry = getattr(api_state, "agent_registry", None)
    llm = getattr(registry, "router_llm", None) or getattr(agent, "llm", None)
    prompt_loader = getattr(registry, "prompt_loader", None)
    if llm is None or prompt_loader is None:
        return {}
    bundle = await agent.metadata_loader.load_all(agent.source_key)
    entity = base_params.get("entity") or {}
    series = base_params.get("series") or {}
    if entity:
        target = entity.get("target")
        target_clause = f" to explain {target}" if target else ""
        current = (
            f"Current analysis: {skill} on {entity.get('table')} by {entity.get('entity_key')} "
            f"using features {', '.join(entity.get('features') or [])}"
            f"{target_clause}; "
            f"parameters {{{', '.join(f'{k}={v}' for k, v in base_params.items() if k != 'entity')}}}."
        )
    else:
        current = (
            f"Current analysis: {skill} of {str(series.get('agg', 'sum')).upper()}({series.get('measure_column')}) "
            f"on {series.get('table')} by {series.get('grain')} using {series.get('date_column')}; "
            f"parameters {{{', '.join(f'{k}={v}' for k, v in base_params.items() if k != 'series')}}}."
        )
    src_req = entity or series
    outcome = await plan_analysis(
        question=f"{question}\n{current}\nAdjust: {instruction}",
        columns_text=(bundle or {}).get("columns", ""),
        resolved_filters=[{"table": src_req.get("table"), "column": f.get("column"), "op": f.get("op"), "value": f.get("value")}
                          for f in (src_req.get("filters") or [])],
        llm=llm, prompt_loader=prompt_loader,
        connection_schema=src_req.get("schema_name"), connection_catalog=src_req.get("catalog"),
        skill_hint=skill,
    )
    if outcome.kind != "params" or outcome.skill != skill or not outcome.params:
        return {}
    new_params = {k: v for k, v in outcome.params.items() if not k.startswith("_")}
    diff = diff_params(base_params, new_params)
    patch: Dict[str, Any] = {}
    series_fields = {"table", "date_column", "measure_column", "agg", "grain", "start", "end"}
    entity_fields = {"entity_key", "features", "target", "row_cap"}
    for key, change in diff.items():
        if key in ("filters", "schema_name", "catalog", "start", "end", "table"):
            continue
        if key in series_fields:
            patch.setdefault("series", {})[key] = change["to"]
        elif key in entity_fields:
            patch.setdefault("entity", {})[key] = change["to"]
        else:
            patch[key] = change["to"]
    return patch


@router.post("/chart", response_model=GenerateChartResponse)
async def analysis_chart(
    request: AnalysisChartRequest,
    response: Response,
    principal: Principal = Depends(get_principal),
):
    """Build the role-based band chart for an ML result deterministically.

    Same persistence as ``/generate-chart`` (the chart baseline is stored on the
    turn so a restored conversation renders it without recomputation), but no
    model call: the envelope already says what to draw.
    """
    chart_started = time.monotonic()
    _require_enabled()
    from src.api.chart_builder import build_band_option  # noqa: PLC0415
    from src.api.routes.charts import (  # noqa: PLC0415
        _chart_request_watermark,
        _persist_chart_baseline,
        _verify_query_owner,
    )

    user_id = principal.user_id
    await _verify_query_owner(query_id=request.query_id, user_id=user_id, connection=request.connection)
    request_started_at = (
        await _chart_request_watermark() if request.query_id else None
    )
    try:
        spec = ChartSpec.model_validate(request.chart_spec).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid chart spec: {exc.errors()[0].get('msg') if exc.errors() else exc}")

    dataset = result_cache.get(user_id=user_id, connection=request.connection, query_id=request.query_id)
    if dataset is None:
        dataset = request.results
    if not dataset or not (dataset.get("rows") or dataset.get("data")):
        raise HTTPException(status_code=409, detail="Result rows are not cached; re-send them as `results`.")
    dataset = _apply_row_filter(dataset, spec.get("row_filter"))
    build_started = time.monotonic()
    try:
        if spec["chart_type"] == "band":
            option = build_band_option(spec, dataset)
        else:
            # Ordinary chart types reuse the deterministic builder with the
            # envelope's explicit bindings — still no LLM.
            from src.api.chart_builder import build_chart_option  # noqa: PLC0415

            ys = spec.get("y_columns") or []
            option = build_chart_option({
                "chart_type": spec["chart_type"],
                "x": spec.get("x_column"),
                "y": ys if len(ys) > 1 else (ys[0] if ys else None),
                "series": spec.get("series_column"),
                "aggregate": "sum",
                "sort": "desc" if spec["chart_type"] in ("bar", "horizontal_bar") and spec.get("x_column") != "lag" else "none",
                "x_label": spec.get("x_label") or "",
                "y_label": spec.get("y_label") or "",
                "value_format": "number",
            }, dataset)
            # Keep the token rule on decomposition lines: the queried series is
            # ink, model components are lavender.
            if spec["chart_type"] == "line":
                for s in option.get("series") or []:
                    s["jeenRole"] = "actual" if str(s.get("name", "")).lower() == "actual" else "expected"
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    build_ms = int((time.monotonic() - build_started) * 1000)
    persist_started = time.monotonic()
    await _persist_chart_baseline(
        query_id=request.query_id,
        user_id=user_id,
        chart_spec=spec,
        chart_config=option,
        request_started_at=request_started_at,
    )
    persist_ms = int((time.monotonic() - persist_started) * 1000)
    total_ms = int((time.monotonic() - chart_started) * 1000)
    response.headers["Server-Timing"] = (
        f"chart-build;dur={build_ms}, chart-persist;dur={persist_ms}, total;dur={total_ms}"
    )
    logger.info(
        "analysis_chart_timing query_id=%s build_ms=%d persist_ms=%d total_ms=%d",
        request.query_id,
        build_ms,
        persist_ms,
        total_ms,
    )
    return GenerateChartResponse(chart_config=option, chart_type=spec["chart_type"], chart_spec=spec)


def _apply_row_filter(dataset: Dict[str, Any], row_filter: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only rows where ``row_filter.column == row_filter.value`` (e.g. one
    dimension of a contribution table, one series of a multi-series result)."""
    if not row_filter or not row_filter.get("column"):
        return dataset
    column, value = str(row_filter["column"]), row_filter.get("value")
    columns = list(dataset.get("columns") or [])
    rows = dataset.get("rows") if dataset.get("rows") is not None else dataset.get("data") or []
    kept = []
    for row in rows:
        if isinstance(row, dict):
            cell = row.get(column)
        elif isinstance(row, (list, tuple)) and column in columns:
            cell = row[columns.index(column)]
        else:
            cell = None
        if str(cell) == str(value):
            kept.append(row)
    return {**dataset, "rows": kept, "row_count": len(kept)}


@router.get("/suggestions")
async def analysis_suggestions(connection: str = Query(...), principal: Principal = Depends(get_principal)):
    """Quick-start ML questions for the empty state, once the catalog has a
    date column and a numeric measure on the same table."""
    _require_enabled()
    from src.agent.analysis_planner import catalog_candidates  # noqa: PLC0415
    from src.api.dependencies import get_metadata_loader  # noqa: PLC0415

    try:
        bundle = await get_metadata_loader().load_all(connection)
    except Exception:  # noqa: BLE001
        return {"suggestions": []}
    cands = catalog_candidates((bundle or {}).get("columns", ""))
    # One ML suggestion per connection (spec §8.9); the two candidates alternate
    # by connection so different sources surface different skills.
    for tc in sorted(cands.values(), key=lambda t: (-len(t.numeric_columns), t.name.lower())):
        if tc.date_columns and tc.numeric_columns:
            measure = tc.numeric_columns[0]
            candidates = [
                {"text": f"Forecast {measure} for the next 8 weeks", "skill": "forecast"},
                {"text": f"Is anything unusual in weekly {measure}?", "skill": "anomaly_detection"},
            ]
            pick = candidates[sum(ord(c) for c in connection) % len(candidates)]
            return {"suggestions": [pick], "table": tc.name}
    return {"suggestions": []}


@router.get("/routing")
async def routing_preview(
    q: str = Query(..., min_length=1, max_length=2000, description="A question to classify"),
    analysis: Optional[bool] = Query(None, description="Simulate the per-request analysis override"),
    principal: Principal = Depends(get_principal),
):
    """Predict, without any LLM call, whether a question would take the ML skill
    path or the text-to-SQL path — the same rule the router applies at run time.

    ``would_route`` is one of ``needs_analysis`` (ML), ``needs_query`` (SQL),
    ``greeting``, or ``router_decides`` (no deterministic cue — the router LLM
    chooses at run time). This is the contract the e2e suite asserts against so
    "when does ML trigger vs SQL" is observable and stable.
    """
    from src.agent.langgraph_agent.nodes.router import explain_routing  # noqa: PLC0415

    prediction = explain_routing(
        q, ml_skills_enabled=bool(settings.ML_SKILLS_ENABLED), analysis_override=analysis,
    )
    return {"contract_version": CONTRACT_VERSION, **prediction}


@router.get("/skills")
async def list_skills(connection: str = Query(...), principal: Principal = Depends(get_principal)):
    """Registered skills plus this user's consent state on the connection."""
    _require_enabled()
    store = _store()
    prefs = await store.list_skill_prefs(user_id=principal.user_id, source_key=connection)
    return {
        "contract_version": CONTRACT_VERSION,
        "runner": getattr(getattr(api_state, "analysis_runner", None), "name", None),
        "skills": [
            {
                "name": s.name, "tier": s.tier, "title": s.title, "description": s.description,
                "estimated_seconds": s.estimated_seconds, "remembered": bool(prefs.get(s.name)),
            }
            for s in SKILLS.values()
        ],
    }


@router.post("/skills/prefs")
async def set_skill_pref(body: SkillPrefPatch, principal: Principal = Depends(get_principal)):
    """Forget a remembered skill. Opting *in* only happens from a displayed
    confirm card (``/run`` with ``remember=true``), where the egress notice
    was actually shown — this endpoint cannot grant consent."""
    _require_enabled()
    store = _store()
    try:
        get_skill(body.skill)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown skill {body.skill!r}")
    if body.remember:
        raise HTTPException(status_code=400, detail="Consent is given from the confirm card, not from settings.")
    await store.set_skill_pref(user_id=principal.user_id, source_key=body.connection, skill=body.skill, remember=False)
    return {"skill": body.skill, "connection": body.connection, "remembered": False}
