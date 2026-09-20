"""The ``catalog_help`` route answers "what can I ask about?" from the catalog.

Covers the deterministic cue (and its negatives), the dry-run prediction, the
graph wiring (SQL + DAX), and the node's Markdown output — including DLP
governance filtering and identifier escaping.
"""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent.graph import _route_from_catalog, _route_from_router
from src.agent.langgraph_agent.nodes.catalog_help import make_catalog_help_answer
from src.agent.langgraph_agent.nodes.router import (
    ROUTE_SOURCE_CATALOG_CUE,
    _VALID_ROUTES,
    detect_catalog_help_intent,
    explain_routing,
)


def test_catalog_help_is_a_valid_route():
    assert "catalog_help" in _VALID_ROUTES


# ── detect_catalog_help_intent ─────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "what measures can I ask about?",
    "which dimensions are available?",
    "what fields can I query?",
    "what columns can I use?",
    "list the tables",
    "which metrics are available to ask about?",
])
def test_catalog_cue_positive(q):
    assert detect_catalog_help_intent(q) is True


@pytest.mark.parametrize("q", [
    "show revenue measures by month",     # concrete data ask (grain)
    "what is the total revenue?",         # aggregation
    "top 5 products by sales",            # top-N
    "which ML models can I use?",         # capability, not catalog
    "what's the weather in Paris?",       # off-topic
    "sales in 2007",                      # a year -> data
    "",
])
def test_catalog_cue_negative(q):
    assert detect_catalog_help_intent(q) is False


@pytest.mark.parametrize("enabled", [True, False])
def test_explain_routing_predicts_catalog_help(enabled):
    p = explain_routing("what measures can I ask about?", ml_skills_enabled=enabled)
    assert p["would_route"] == "catalog_help"
    assert p["source"] == ROUTE_SOURCE_CATALOG_CUE


# ── graph wiring ───────────────────────────────────────────────────────────────

def test_router_sends_catalog_help_through_catalog_lookup():
    assert _route_from_router({"route": "catalog_help"}) == "catalog_lookup"


def test_catalog_lookup_branches_to_catalog_help_answer():
    assert _route_from_catalog({"route": "catalog_help"}) == "catalog_help_answer"


def test_blocked_catalog_still_wins():
    assert _route_from_catalog({"route": "catalog_help", "catalog_blocked": True}) == "response_formatter"


def test_dax_graph_wires_catalog_help():
    from src.agent.langgraph_agent_dax.graph import (
        _route_from_catalog as dax_from_catalog,
        _route_from_router as dax_from_router,
    )

    assert dax_from_router({"route": "catalog_help"}) == "dax_catalog_lookup"
    assert dax_from_catalog({"route": "catalog_help"}) == "catalog_help_answer"


# ── the node ────────────────────────────────────────────────────────────────────

_COLUMNS = "\n".join([
    "- FactSales.SalesAmount - Type: decimal",
    "- FactSales.OrderQuantity - Type: int",
    "- FactSales.CustomerKey - Type: int, PK: true",
    "- FactSales.OrderDate - Type: date",
    "- FactSales.password - Type: varchar",          # built-in sensitive
    "- DimCustomer.CustomerName - Type: nvarchar",
    "- DimCustomer.EmailAddress - Type: nvarchar",   # governed via setting
    "- SalesKPIs.Total Revenue - Type: measure",     # curated DAX measure
])


async def _run(governed=None):
    node = make_catalog_help_answer(governed_columns=governed or [])
    return (await node({
        "metadata_bundle": {"columns": _COLUMNS, "knowledge_pairs": "Q: How many customers signed up?\n"},
        "connection_display_name": "AdventureWorksDW",
    }))["answer"]


async def test_node_groups_by_category_and_table():
    answer = await _run()
    assert "AdventureWorksDW" in answer
    assert "### FactSales" in answer and "### DimCustomer" in answer
    assert "**Measures:**" in answer and "`Total Revenue`" in answer   # curated measure
    assert "**Numeric fields:**" in answer and "`SalesAmount`" in answer
    assert "**Dates:**" in answer and "`OrderDate`" in answer
    # A *Key column is a dimension, not a numeric measure.
    assert "`CustomerKey`" in answer
    # Column names are wrapped in code spans (safe from Markdown-active chars).
    assert "`CustomerName`" in answer


async def test_node_never_lists_governed_or_sensitive_columns():
    answer = await _run(governed=["EmailAddress"])
    assert "password" not in answer          # built-in sensitive pattern
    assert "EmailAddress" not in answer       # DLP governed-columns setting


async def test_node_includes_example_questions():
    answer = await _run()
    assert "### Example questions" in answer
    assert "How many customers signed up?" in answer


async def test_node_handles_empty_catalog():
    node = make_catalog_help_answer()
    out = await node({"metadata_bundle": {"columns": ""}, "connection_display_name": "DB"})
    assert "don't have any registered columns" in out["answer"]
