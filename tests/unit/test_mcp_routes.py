"""Unit tests for MCP diagnostic tool-call route helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.routes import mcp


def _tool(name: str = "list_tables", *, annotations=None, output_schema=None):
    return {
        "name": name,
        "annotations": annotations or {},
        "output_schema": output_schema or {},
    }


def _configure_route(monkeypatch, tools, result=None):
    service = MagicMock()
    service.get_by_id = AsyncMock(return_value=MagicMock(id=1))
    client = MagicMock()
    client.inspect_tools = AsyncMock(return_value=tools)
    client.call_tool_for_test = AsyncMock(return_value=result or {"content": []})
    monkeypatch.setattr(mcp, "_srv_svc", lambda: service)
    monkeypatch.setattr(mcp, "_catalog_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_tool_call_rejects_unknown_tool(monkeypatch):
    client = _configure_route(monkeypatch, [_tool()])

    with pytest.raises(HTTPException) as exc:
        await mcp.call_server_tool(1, mcp.ToolCallRequest(tool_name="not_listed"))

    assert exc.value.status_code == 404
    assert "not_listed" in str(exc.value.detail)
    client.call_tool_for_test.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_call_requires_confirmation_without_read_only_annotation(monkeypatch):
    client = _configure_route(monkeypatch, [_tool()])

    with pytest.raises(HTTPException) as exc:
        await mcp.call_server_tool(1, mcp.ToolCallRequest(tool_name="list_tables"))

    assert exc.value.status_code == 409
    assert exc.value.detail["risk"]["level"] == "confirmation_required"
    client.call_tool_for_test.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_call_returns_full_result_and_output_schema_diagnostic(monkeypatch):
    result = {
        "content": [{"type": "text", "text": "done"}],
        "structuredContent": {"count": 3},
        "_meta": {"traceId": "abc"},
        "isError": False,
    }
    client = _configure_route(
        monkeypatch,
        [_tool(
            annotations={"readOnlyHint": True},
            output_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
        )],
        result,
    )

    response = await mcp.call_server_tool(
        1,
        mcp.ToolCallRequest(tool_name="list_tables", arguments={"connection_id": 1}),
    )

    assert response["ok"] is True
    assert response["result"] == result
    assert response["risk"]["level"] == "read_only"
    assert response["output_validation"] == {"available": True, "valid": True, "errors": []}
    client.call_tool_for_test.assert_awaited_once()


def test_output_validation_reports_mismatch_without_rejecting_result():
    diagnostic = mcp._output_validation_diagnostic(
        {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
        {"structuredContent": {"count": "three"}},
    )

    assert diagnostic["available"] is True
    assert diagnostic["valid"] is False
    assert diagnostic["errors"][0]["path"] == "/count"


@pytest.mark.asyncio
async def test_ai_error_assistance_returns_validated_suggestion_without_calling_tool(monkeypatch):
    client = _configure_route(
        monkeypatch,
        [_tool(
            "get_table_profile",
            output_schema={},
        )],
    )
    client.inspect_tools.return_value[0]["input_schema"] = {
        "type": "object",
        "properties": {"connection_id": {"type": "integer"}, "table_name": {"type": "string"}},
        "required": ["connection_id", "table_name"],
    }
    llm = MagicMock()
    llm.generate = AsyncMock(return_value={
        "content": """{
            "summary": "The table name is missing.",
            "likely_cause": "The tool requires both parameters.",
            "suggested_arguments": {"connection_id": 7, "table_name": "DimDate"},
            "next_steps": ["Review the suggested arguments."]
        }"""
    })
    monkeypatch.setattr(mcp.state, "llm_service", llm)

    response = await mcp.assist_tool_error(
        1,
        mcp.ToolErrorAssistRequest(
            tool_name="get_table_profile",
            arguments={"connection_id": 7},
            error={"message": "table_name is required", "token": "must-not-leak"},
        ),
    )

    assert response["suggested_arguments"] == {"connection_id": 7, "table_name": "DimDate"}
    assert response["suggestion_validation"]["valid"] is True
    assert response["next_steps"] == ["Review the suggested arguments."]
    client.call_tool_for_test.assert_not_awaited()
    prompt = llm.generate.await_args.kwargs["messages"][1]["content"]
    assert "must-not-leak" not in prompt
    assert "[redacted]" in prompt


def test_ai_suggestion_schema_validation_rejects_invalid_arguments():
    diagnostic = mcp._suggestion_schema_diagnostic(
        {
            "type": "object",
            "properties": {"connection_id": {"type": "integer"}},
            "required": ["connection_id"],
        },
        {"connection_id": "seven"},
    )

    assert diagnostic["valid"] is False
    assert diagnostic["errors"]
