"""Text-to-SQL query + table/schema introspection endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.api.dependencies import (
    get_catalog_provider,
    get_history_service,
    require_user_context_user_id,
    resolve_agent,
)
from src.api.models import QueryRequest, QueryResponse
from src.api.result_cache import result_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query_database(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    user_id = require_user_context_user_id(request.user_context)
    if request.session_id:
        history = get_history_service()
        if not await history.session_belongs_to_user(
            session_id=request.session_id, user_id=user_id
        ):
            raise HTTPException(status_code=404, detail="Session not found for this user")
    agent = await resolve_agent(request.connection)
    try:
        result = await agent.process_question(
            question=request.question,
            session_id=request.session_id,
            user_context=request.user_context or {},
            limit=request.limit,
            temperature=request.temperature,
            eval_analytics=request.eval_analytics,
            llm_timeout=request.llm_timeout,
        )
        # Cache the result so charts / describe / insights can reuse the full
        # rows (keyed by user+connection+query_id) instead of the browser
        # re-uploading them. Best-effort: never fail the query over a cache hiccup.
        try:
            result_cache.put(
                user_id=user_id,
                connection=request.connection,
                query_id=result.get("query_id"),
                dataset=result.get("results"),
            )
        except Exception:  # noqa: BLE001
            logger.debug("result_cache put failed", exc_info=True)
        return QueryResponse(**result)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error processing question")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tables")
async def list_tables(
    connection: str = Query(..., description="source_key of the active connection"),
):
    agent = await resolve_agent(connection)
    try:
        tables = await agent.sql_runner.list_tables()
        return {"tables": tables}
    except Exception as e:  # noqa: BLE001
        logger.exception("Error listing tables")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tables-rich")
async def list_tables_rich(
    connection: str = Query(..., description="source_key of the active connection"),
):
    """Return all catalogued tables with descriptions and column counts.

    Sourced from the active catalog provider (MCP when ``catalog_source=mcp``,
    otherwise the metadata DB). Each entry includes ``name``, ``description``
    (nullable), and ``col_count``.
    """
    # Resolve the provider outside the try so its 503 (e.g. loader not yet
    # initialised) propagates intact instead of being masked as a 500.
    provider = await get_catalog_provider(connection)
    try:
        tables = await provider.load_tables_rich(connection)
        return {"tables": tables}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error listing rich tables for connection %r", connection)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/schema/{table_name}")
async def get_table_schema(
    table_name: str,
    connection: str = Query(..., description="source_key of the active connection"),
):
    agent = await resolve_agent(connection)
    try:
        schema = await agent.sql_runner.get_table_schema(table_name)
        if not schema:
            raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")
        return {"table": table_name, "schema": schema}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Error getting schema")
        raise HTTPException(status_code=500, detail=str(e)) from e
