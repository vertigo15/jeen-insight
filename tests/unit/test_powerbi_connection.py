"""Unit tests for Power BI connection recognition + agent dispatch.

Covers three seams that keep the SQL path untouched:
  * ``factory`` recognises Power BI service types and surfaces dataset ids;
  * ``ConnectionService._row_to_connection`` builds a ``is_power_bi`` Connection
    without any SqlRunner parsing;
  * ``resolve_agent`` dispatches Power BI to the DAX registry, everything else to
    the SQL registry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import src.api.dependencies as deps
from src.connections.connection_service import ConnectionService
from src.connectors.factory import (
    is_power_bi_service_type,
    power_bi_connection_fields,
)


class TestServiceTypeRecognition:
    @pytest.mark.parametrize(
        "value", ["powerbi", "power-bi", "power_bi", "Power BI", "  PowerBI  "]
    )
    def test_recognised(self, value):
        assert is_power_bi_service_type(value) is True

    @pytest.mark.parametrize("value", ["postgres", "trino", "", None, "databricks"])
    def test_not_recognised(self, value):
        assert is_power_bi_service_type(value) is False

    def test_fields_camel_case(self):
        f = power_bi_connection_fields(
            {"workspaceId": "ws", "datasetId": "ds", "modelVersion": "3"}
        )
        assert f["workspace_id"] == "ws"
        assert f["dataset_id"] == "ds"
        assert f["model_version"] == "3"

    def test_fields_snake_case_and_group_alias(self):
        f = power_bi_connection_fields({"group_id": "ws2", "dataset_id": "ds2"})
        assert f["workspace_id"] == "ws2"
        assert f["dataset_id"] == "ds2"


class TestRowToConnection:
    def _svc(self):
        return ConnectionService(metadata_pool=MagicMock())

    def test_power_bi_row_builds_dax_connection(self):
        row = {
            "id": 7,
            "name": "Sales Model",
            "description": "PBI dataset",
            "service_type": "powerbi",
            "connection_config": {"workspaceId": "ws-1", "datasetId": "ds-1"},
            "is_active": True,
        }
        conn = self._svc()._row_to_connection(row)
        assert conn.is_power_bi is True
        assert conn.database_type == "powerbi"
        assert conn.workspace_id == "ws-1"
        assert conn.dataset_id == "ds-1"
        assert conn.connection_host is None
        pub = conn.to_public_dict()
        assert pub["is_power_bi"] is True
        assert pub["dataset_id"] == "ds-1"

    def test_sql_row_is_not_power_bi(self):
        row = {
            "id": 1,
            "name": "PG",
            "description": None,
            "service_type": "postgres",
            "connection_config": {"host": "h", "port": 5432, "database": "d"},
            "is_active": True,
        }
        conn = self._svc()._row_to_connection(row)
        assert conn.is_power_bi is False
        assert conn.database_type == "postgres"
        assert conn.connection_host == "h"


class TestResolveAgentDispatch:
    async def test_missing_source_key_is_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await deps.resolve_agent("")
        assert ei.value.status_code == 400

    async def test_power_bi_routes_to_dax_registry(self, monkeypatch):
        conn = MagicMock(is_power_bi=True)
        cs = MagicMock()
        cs.get_connection = AsyncMock(return_value=conn)
        dax_reg = MagicMock()
        dax_reg.get_agent = AsyncMock(return_value="DAX_AGENT")
        sql_reg = MagicMock()
        sql_reg.get_agent = AsyncMock(return_value="SQL_AGENT")

        monkeypatch.setattr(deps, "get_connection_service", lambda: cs)
        monkeypatch.setattr(deps, "get_dax_agent_registry", lambda: dax_reg)
        monkeypatch.setattr(deps, "get_agent_registry", lambda: sql_reg)

        result = await deps.resolve_agent("pbi-conn")
        assert result == "DAX_AGENT"
        dax_reg.get_agent.assert_awaited_once_with("pbi-conn")
        sql_reg.get_agent.assert_not_awaited()

    async def test_sql_routes_to_sql_registry(self, monkeypatch):
        conn = MagicMock(is_power_bi=False)
        cs = MagicMock()
        cs.get_connection = AsyncMock(return_value=conn)
        dax_reg = MagicMock()
        dax_reg.get_agent = AsyncMock(return_value="DAX_AGENT")
        sql_reg = MagicMock()
        sql_reg.get_agent = AsyncMock(return_value="SQL_AGENT")

        monkeypatch.setattr(deps, "get_connection_service", lambda: cs)
        monkeypatch.setattr(deps, "get_dax_agent_registry", lambda: dax_reg)
        monkeypatch.setattr(deps, "get_agent_registry", lambda: sql_reg)

        result = await deps.resolve_agent("pg-conn")
        assert result == "SQL_AGENT"
        sql_reg.get_agent.assert_awaited_once_with("pg-conn")
        dax_reg.get_agent.assert_not_awaited()

    async def test_missing_connection_is_404(self, monkeypatch):
        from fastapi import HTTPException

        from src.connections import ConnectionNotFound

        cs = MagicMock()
        cs.get_connection = AsyncMock(side_effect=ConnectionNotFound("nope"))
        monkeypatch.setattr(deps, "get_connection_service", lambda: cs)

        with pytest.raises(HTTPException) as ei:
            await deps.resolve_agent("ghost")
        assert ei.value.status_code == 404
