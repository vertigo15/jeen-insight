"""Dataset insights + profiling reports."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.api.dependencies import get_history_service, resolve_agent
from src.api.models import (
    GenerateInsightsRequest,
    GenerateInsightsResponse,
    GenerateProfileRequest,
)
from src.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["insights"])


def _sse(event: str, payload: dict) -> str:
    """Format a single Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/generate-insights", response_model=GenerateInsightsResponse)
async def generate_insights_endpoint(request: GenerateInsightsRequest):
    agent = await resolve_agent(request.connection)
    logger.info("Generating insights for: %s", request.question[:50])
    try:
        from src.api import state as app_state

        # ── LangGraph eval path (preferred when SQL is available) ───────────
        if request.sql and app_state.insights_eval_graph is not None:
            logger.info("insights: using LangGraph eval node")
            from src.agent.langgraph_agent import run_eval

            columns  = request.dataset.get("columns", [])
            rows_raw = request.dataset.get("rows") or request.dataset.get("data") or []
            results  = [
                dict(zip(columns, row)) if isinstance(row, (list, tuple)) else row
                for row in rows_raw
            ]

            start = time.time()
            state_out = await run_eval(
                app_state.insights_eval_graph,
                question  = request.question,
                sql       = request.sql,
                results   = results,
                row_count = len(results),
            )
            exec_time_ms = int((time.time() - start) * 1000)

            summary  = state_out.get("summary", "")
            findings = state_out.get("insights") or []
            suggestions = state_out.get("follow_up_questions") or []

            history = get_history_service() if request.query_id else None
            if history and request.query_id:
                try:
                    await history.add_insight(
                        query_id=request.query_id,
                        insight_type="summary",
                        content=summary or "Analysis complete",
                        llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                        llm_execution_time_ms=exec_time_ms,
                        tokens_input=0,
                        tokens_output=0,
                    )
                    for finding in findings:
                        await history.add_insight(
                            query_id=request.query_id,
                            insight_type="finding",
                            content=finding,
                            llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                        )
                    for suggestion in suggestions:
                        await history.add_insight(
                            query_id=request.query_id,
                            insight_type="suggestion",
                            content=suggestion,
                            llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to log eval insights to history")

            return GenerateInsightsResponse(
                summary=summary or "Analysis complete",
                findings=findings,
                suggestions=suggestions,
            )

        # ── Legacy insight_service path (no SQL, or graph unavailable) ──────
        logger.info("insights: using legacy insight_service path")
        from src.agent.insight_service import generate_insights

        bundle  = await agent.metadata_loader.load_all(agent.source_key)
        context = {"documentation": [bundle.get("business_terms", "")]}

        prompt_template = None
        model_override  = None
        if app_state.prompt_cache:
            try:
                prompt_template = await app_state.prompt_cache.get_content("insights")
                model_override  = await app_state.prompt_cache.get_model_override("insights")
            except Exception:
                pass

        start = time.time()
        insights = await generate_insights(
            dataset=request.dataset,
            context=context,
            original_question=request.question,
            llm_service=agent.llm,
            prompt_template=prompt_template,
            model_override=model_override,
        )
        exec_time_ms = int((time.time() - start) * 1000)

        history = get_history_service() if request.query_id else None
        if history and request.query_id:
            try:
                await history.add_insight(
                    query_id=request.query_id,
                    insight_type="summary",
                    content=insights.get("summary", "Analysis complete"),
                    llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    llm_execution_time_ms=exec_time_ms,
                    tokens_input=insights.get("usage", {}).get("prompt_tokens", 0),
                    tokens_output=insights.get("usage", {}).get("completion_tokens", 0),
                )
                for finding in insights.get("findings", []):
                    await history.add_insight(
                        query_id=request.query_id,
                        insight_type="finding",
                        content=finding,
                        llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    )
                for suggestion in insights.get("suggestions", []):
                    await history.add_insight(
                        query_id=request.query_id,
                        insight_type="suggestion",
                        content=suggestion,
                        llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to log insights to history")

        return GenerateInsightsResponse(
            summary=insights.get("summary", "Analysis complete"),
            findings=insights.get("findings", []),
            suggestions=insights.get("suggestions", []),
            prompt=insights.get("prompt"),
            system_message=insights.get("system_message"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Insights generation error")
        return GenerateInsightsResponse(
            summary="Unable to generate insights",
            findings=[],
            suggestions=[],
        )


@router.post("/generate-insights/stream")
async def generate_insights_stream_endpoint(request: GenerateInsightsRequest):
    """Streaming version of /api/generate-insights using Server-Sent Events.

    The response is ``text/event-stream`` with named events: ``open``,
    ``ttft``, ``delta``, ``done``, ``error``. The client renders deltas as
    they arrive and replaces the placeholder with the structured insights
    once ``done`` fires. Real TTFT (first non-empty content chunk) is
    measured server-side and emitted as its own event.
    """
    agent = await resolve_agent(request.connection)
    logger.info("Streaming insights for: %s", request.question[:50])

    from src.api import state as app_state
    from src.agent.insight_service import generate_insights_stream

    bundle = await agent.metadata_loader.load_all(agent.source_key)
    context = {"documentation": [bundle.get("business_terms", "")]}
    history = get_history_service() if request.query_id else None

    async def event_generator():
        yield ": ping\n\n"

        final_insights: dict = {}
        final_metrics: dict = {}

        # ── LangGraph eval path (preferred when SQL is available) ──────────
        if request.sql and app_state.insights_eval_graph is not None:
            logger.info("insights/stream: using LangGraph eval node")
            try:
                from src.agent.langgraph_agent import run_eval

                columns  = request.dataset.get("columns", [])
                rows_raw = request.dataset.get("rows") or request.dataset.get("data") or []
                results  = [
                    dict(zip(columns, row)) if isinstance(row, (list, tuple)) else row
                    for row in rows_raw
                ]

                t0 = time.time()
                state_out = await run_eval(
                    app_state.insights_eval_graph,
                    question  = request.question,
                    sql       = request.sql,
                    results   = results,
                    row_count = len(results),
                )
                latency_ms = int((time.time() - t0) * 1000)

                final_insights = {
                    "summary":     state_out.get("summary", ""),
                    "findings":    state_out.get("insights") or [],
                    "suggestions": state_out.get("follow_up_questions") or [],
                }
                final_metrics = {"llm_latency_ms": latency_ms}
                yield _sse("done", {"insights": final_insights, "metrics": final_metrics})
            except Exception as exc:  # noqa: BLE001
                logger.exception("LangGraph eval node failed in stream endpoint")
                yield _sse("error", {"error": str(exc)})
                return

        else:
            # ── Legacy streaming path ──────────────────────────────────────
            prompt_template = None
            model_override   = None
            if app_state.prompt_cache:
                try:
                    prompt_template = await app_state.prompt_cache.get_content("insights")
                    model_override   = await app_state.prompt_cache.get_model_override("insights")
                except Exception:
                    pass

            try:
                async for ev in generate_insights_stream(
                    dataset=request.dataset,
                    context=context,
                    original_question=request.question,
                    llm_service=agent.llm,
                    prompt_template=prompt_template,
                    model_override=model_override,
                ):
                    kind = ev.get("type")
                    if kind == "open":
                        yield _sse("open", {
                            "prompt": ev.get("prompt", ""),
                            "system_message": ev.get("system_message", ""),
                        })
                    elif kind == "ttft":
                        yield _sse("ttft", {"ms": ev.get("ms")})
                    elif kind == "delta":
                        yield _sse("delta", {"text": ev.get("text", "")})
                    elif kind == "error":
                        yield _sse("error", {"error": ev.get("error", "unknown error")})
                        return
                    elif kind == "done":
                        final_insights = ev.get("insights") or {}
                        final_metrics  = ev.get("metrics") or {}
                        yield _sse("done", {
                            "insights": final_insights,
                            "metrics":  final_metrics,
                        })
            except Exception as e:  # noqa: BLE001
                logger.exception("Streaming insights failed")
                yield _sse("error", {"error": str(e)})
                return

        # Best-effort: log the same insights to history (mirrors the
        # non-streaming endpoint). Done after streaming so the client
        # already has its data even if logging fails.
        if history and request.query_id and final_insights:
            try:
                await history.add_insight(
                    query_id=request.query_id,
                    insight_type="summary",
                    content=final_insights.get("summary", "Analysis complete"),
                    llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    llm_execution_time_ms=final_metrics.get("llm_latency_ms") or 0,
                    tokens_input=final_metrics.get("input_tokens") or 0,
                    tokens_output=final_metrics.get("output_tokens") or 0,
                )
                for finding in final_insights.get("findings", []) or []:
                    await history.add_insight(
                        query_id=request.query_id,
                        insight_type="finding",
                        content=finding,
                        llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    )
                for suggestion in final_insights.get("suggestions", []) or []:
                    await history.add_insight(
                        query_id=request.query_id,
                        insight_type="suggestion",
                        content=suggestion,
                        llm_model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to log streamed insights to history")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering if present
            "Connection": "keep-alive",
        },
    )


@router.post("/generate-profile")
async def generate_profile_endpoint(request: GenerateProfileRequest):
    report_type = request.report_type.lower()
    logger.info("Generating data profile report using: %s", report_type)
    try:
        if report_type == "sweetviz":
            from src.agent.sweetviz_service import generate_sweetviz_report

            html_report = await generate_sweetviz_report(request.dataset)
        else:
            from src.agent.profiling_service import generate_profile_report

            html_report = await generate_profile_report(request.dataset)
        return {"html": html_report}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Profile generation error")
        raise HTTPException(status_code=500, detail=str(e)) from e
