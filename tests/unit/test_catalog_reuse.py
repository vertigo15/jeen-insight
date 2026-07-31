"""Catalog seeding, reuse and refresh.

Both agents load the catalog before starting the graph, concurrently with the
history fetch and the audit insert. The lookup node used to reload it anyway, so
every query paid for two catalog loads. These tests pin the contract that
replaced that:

  - a seeded bundle is consumed instead of reloaded;
  - the ticket is one-shot, so the explicit refresh routes (``missing_table`` in
    SQL, ``refresh_catalog`` in DAX) still get a genuine reload;
  - a failed pre-graph load stays invisible to the user, because the node
    retries and either succeeds or fails closed with its own message.

The last point is the subtle one: ``response_formatter`` prefers
``state["error"]`` over every other answer source, so seeding a transient
pre-graph failure there would turn a recovered query into a visible error.

The agent-level tests at the bottom cover the same pre-graph / post-graph
boundary from the other side, which is why the node-trace persistence check
lives here too: it needs the same fully-doubled agent.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import src.agent.langgraph_agent.nodes.catalog as catalog_mod
from src.agent.langgraph_agent.nodes.catalog import make_catalog_lookup
from src.agent.langgraph_agent_dax.nodes.catalog import make_dax_catalog_lookup

_BUNDLE: Dict[str, str] = {
    "tables": "- FactSales - Internet sales\n- DimProduct - Products",
    "columns": (
        "- FactSales.SalesAmount - Type: decimal\n"
        "- FactSales.OrderDate - Type: date\n"
        "- DimProduct.ProductName - Type: varchar\n"
        "- FactSales.Total Sales - Type: measure"
    ),
    "relationships": "[('FactSales.ProductKey', 'DimProduct.ProductKey')]",
}

_FRESH_BUNDLE: Dict[str, str] = {
    "tables": "- FactSales\n- DimProduct\n- DimCustomer",
    "columns": "- DimCustomer.CustomerName - Type: varchar",
    "relationships": "",
}


def _loader_patch(bundle=None, *, source="db", cache=None, fail=False):
    """Patch the single shared catalog loader and count calls."""
    if fail:
        mock = AsyncMock(side_effect=RuntimeError("catalog backend down"))
    else:
        mock = AsyncMock(
            return_value=(
                bundle if bundle is not None else _FRESH_BUNDLE,
                {"source": source, "cache": cache, "load_ms": 7},
            )
        )
    return patch.object(catalog_mod, "_load_catalog_bundle", mock), mock


def _state(**overrides) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "source_key": "test_db",
        "connection_display_name": "AdventureWorks",
        "metadata_bundle": {},
        "catalog_seeded": False,
    }
    base.update(overrides)
    return base


# ── SQL catalog_lookup ────────────────────────────────────────────────────────


class TestSqlCatalogLookup:
    @pytest.mark.asyncio
    async def test_seeded_bundle_is_used_without_reloading(self):
        ctx, loader = _loader_patch()
        with ctx:
            node = make_catalog_lookup(MagicMock())
            updates = await node(_state(metadata_bundle=_BUNDLE, catalog_seeded=True))

        loader.assert_not_awaited()
        assert updates["metadata_bundle"] is _BUNDLE
        assert updates["known_tables"] == ["factsales", "dimproduct"]
        assert updates["catalog_available"] is True

    @pytest.mark.asyncio
    async def test_loads_once_when_nothing_was_seeded(self):
        ctx, loader = _loader_patch()
        with ctx:
            node = make_catalog_lookup(MagicMock())
            updates = await node(_state())

        assert loader.await_count == 1
        assert "dimcustomer" in updates["known_tables"]

    @pytest.mark.asyncio
    async def test_refresh_reloads_because_the_ticket_is_one_shot(self):
        """The missing_table route re-enters this node expecting fresh metadata.

        On re-entry ``metadata_bundle`` is populated from the first pass, so
        reuse must key off ``catalog_seeded`` — which the first pass cleared —
        rather than off the bundle being present.
        """
        ctx, loader = _loader_patch()
        with ctx:
            node = make_catalog_lookup(MagicMock())
            first = await node(_state(metadata_bundle=_BUNDLE, catalog_seeded=True))
            assert first["catalog_seeded"] is False

            # Second visit carries the first pass's state, bundle included.
            second = await node(_state(**first))

        assert loader.await_count == 1
        assert "dimcustomer" in second["known_tables"]

    @pytest.mark.asyncio
    async def test_seeded_provider_and_cache_survive_into_the_trace(self):
        ctx, loader = _loader_patch()
        with ctx:
            node = make_catalog_lookup(MagicMock())
            updates = await node(_state(
                metadata_bundle=_BUNDLE,
                catalog_seeded=True,
                catalog_source_used="mcp",
                catalog_cache="hit",
                catalog_load_ms=42,
            ))

        loader.assert_not_awaited()
        assert updates["catalog_source_used"] == "mcp"
        assert updates["catalog_cache"] == "hit"
        assert updates["catalog_load_ms"] == 42

    @pytest.mark.asyncio
    async def test_failed_load_still_fails_closed(self):
        ctx, _loader = _loader_patch(fail=True)
        with ctx:
            node = make_catalog_lookup(MagicMock(), require_catalog=True)
            updates = await node(_state())

        assert updates["catalog_blocked"] is True
        assert updates["catalog_available"] is False
        assert "blocked" in updates["error"].lower()
        assert "AdventureWorks" in updates["answer"]

    @pytest.mark.asyncio
    async def test_empty_catalog_is_not_treated_as_a_seed(self):
        """An empty pre-graph bundle means the pre-load failed — reload it."""
        ctx, loader = _loader_patch()
        with ctx:
            node = make_catalog_lookup(MagicMock())
            updates = await node(_state(metadata_bundle={}, catalog_seeded=True))

        assert loader.await_count == 1
        assert updates["catalog_available"] is True


# ── DAX catalog_lookup ────────────────────────────────────────────────────────


class TestDaxCatalogLookup:
    @pytest.mark.asyncio
    async def test_seeded_bundle_is_used_without_reloading(self):
        ctx, loader = _loader_patch()
        with ctx:
            node = make_dax_catalog_lookup(MagicMock())
            updates = await node(_state(metadata_bundle=_BUNDLE, catalog_seeded=True))

        loader.assert_not_awaited()
        assert updates["known_tables"] == ["factsales", "dimproduct"]
        # Measures must still be split out of the same columns block.
        assert updates["known_measures"] == ["total sales"]
        assert updates["measures_available"] is True
        assert "total sales" not in updates["known_columns"]
        assert updates["date_table"] is not None

    @pytest.mark.asyncio
    async def test_refresh_reloads_because_the_ticket_is_one_shot(self):
        ctx, loader = _loader_patch()
        with ctx:
            node = make_dax_catalog_lookup(MagicMock())
            first = await node(_state(metadata_bundle=_BUNDLE, catalog_seeded=True))
            await node(_state(**first))

        assert loader.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_load_fails_closed_with_dax_wording(self):
        ctx, _loader = _loader_patch(fail=True)
        with ctx:
            node = make_dax_catalog_lookup(MagicMock(), require_catalog=True)
            updates = await node(_state())

        assert updates["catalog_blocked"] is True
        assert "DAX" in updates["error"]


# ── Agent-level: pre-graph failure must not outlive itself ────────────────────


def _sql_tool_call(sql: str) -> Dict[str, Any]:
    return {
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_sql", "arguments": json.dumps({"sql": sql})},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _build_agent(history):
    from src.agent.jeen_insights_agent import JeenInsightsAgent
    from src.agent.langgraph_agent.prompt_loader import PromptLoader

    connection = MagicMock()
    connection.source_key = "test_db"
    connection.display_name = "AdventureWorks"
    connection.database_type = "postgresql"
    connection.connection_database = "adventureworks"
    connection.connection_catalog = None
    connection.db_schema = "dbo"

    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=[
        {
            "content": json.dumps({"route": "needs_query", "reason": "test"}),
            "finish_reason": "stop",
            "usage": {},
        },
        _sql_tool_call("SELECT SUM(SalesAmount) FROM FactSales"),
    ])

    sql_runner = MagicMock()
    sql_runner.run_sql = AsyncMock(return_value={
        "columns": ["sum"], "rows": [{"sum": 10}, {"sum": 20}], "row_count": 2,
    })

    metadata_loader = MagicMock()
    metadata_loader.load_all = AsyncMock(return_value=_BUNDLE)

    user_resolver = MagicMock()
    user_resolver.resolve_user = AsyncMock(return_value=MagicMock(id=uuid4()))

    return JeenInsightsAgent(
        connection=connection,
        sql_runner=sql_runner,
        llm_service=llm,
        router_llm_service=llm,
        metadata_loader=metadata_loader,
        history_service=history,
        user_resolver=user_resolver,
        prompt_loader=PromptLoader(),
    )


def _history_mock():
    history = MagicMock()
    history.log_query = AsyncMock(return_value=uuid4())
    history.get_conversation_context = AsyncMock(return_value=[])
    history.update_llm_response = AsyncMock()
    history.update_execution = AsyncMock()
    history.update_node_trace = AsyncMock()
    return history


class TestPreGraphCatalogFailure:
    @pytest.mark.asyncio
    async def test_recovered_pre_graph_failure_is_not_reported_to_the_user(self):
        """Pre-load fails, the node's own load succeeds → no error in the response.

        The pre-load is only a head start. Surfacing its failure would let a
        transient blip mask an otherwise correct answer, because
        ``response_formatter`` prefers ``state["error"]`` over the real answer.
        """
        history = _history_mock()
        agent = _build_agent(history)

        pre_graph = AsyncMock(side_effect=RuntimeError("metadata pool exhausted"))
        in_graph, _loader = _loader_patch(_BUNDLE)

        with patch("src.agent.jeen_insights_agent._load_catalog_bundle", pre_graph), \
             in_graph, \
             patch("src.metadata.runtime_settings.get_runtime_settings",
                   AsyncMock(return_value=_runtime_defaults())):
            result = await agent.process_question(
                question="What are total sales?", eval_analytics=False,
            )

        assert pre_graph.await_count == 1
        assert result["error"] is None
        assert result["sql"] == "SELECT SUM(SalesAmount) FROM FactSales"

    @pytest.mark.asyncio
    async def test_persisted_trace_carries_timings_but_no_prompts(self):
        history = _history_mock()
        agent = _build_agent(history)

        in_graph, _loader = _loader_patch(_BUNDLE)
        with in_graph, \
             patch("src.metadata.runtime_settings.get_runtime_settings",
                   AsyncMock(return_value=_runtime_defaults())):
            result = await agent.process_question(
                question="What are total sales?", eval_analytics=False,
            )

        history.update_node_trace.assert_awaited_once()
        stored = history.update_node_trace.await_args.kwargs["node_trace"]
        assert stored, "expected at least one node timing"
        assert {e["node"] for e in stored} >= {"catalog_lookup", "sql_generator"}
        for entry in stored:
            assert set(entry) == {"node", "elapsed_ms", "type"}

        # The response still gets the rich, prompt-bearing trace.
        assert any("prompt" in ev for ev in result["trace"])


def _runtime_defaults():
    from src.metadata.runtime_settings import _defaults
    return _defaults()

