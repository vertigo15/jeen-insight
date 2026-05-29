"""Text-to-SQL query + table/schema introspection endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import get_metadata_loader, resolve_agent
from src.api.models import QueryRequest, QueryResponse
from src.metadata import MetadataLoader

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query_database(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
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
    loader: MetadataLoader = Depends(get_metadata_loader),
):
    """Return all catalogued tables from the metadata DB with descriptions and column counts.

    Sourced from ``public.metadata_tables`` — not from a live connection query.
    Each entry includes ``name``, ``description`` (nullable), and ``col_count``.
    """
    try:
        tables = await loader.load_tables_rich(connection)
        return {"tables": tables}
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
