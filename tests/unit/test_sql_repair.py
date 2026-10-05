"""sql_repair: focused-context helpers and the routing around the node."""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent.graph import _route_from_feedback, _route_from_sql_repair
from src.agent.langgraph_agent.nodes.sql_repair import (
    columns_for_tables,
    referenced_tables,
    relationships_for_tables,
)

_COLUMNS = (
    "- FactSales.SalesAmount - Type: decimal\n"
    "- FactSales.OrderYear - Type: integer\n"
    "- DimProduct.ProductKey - Type: integer, PK: true\n"
    "- DimProduct.Name - Type: text"
)


class TestReferencedTables:
    def test_joins_and_ctes(self):
        sql = (
            "WITH s AS (SELECT * FROM FactSales) "
            "SELECT p.Name FROM s JOIN DimProduct p ON p.ProductKey = s.ProductKey"
        )
        assert {"factsales", "dimproduct"} <= referenced_tables(sql, "postgresql")

    def test_unparseable_sql_falls_back_to_identifiers(self):
        assert "factsales" in referenced_tables("SELEC x FRM FactSales WHERE (", "postgresql")


class TestFocusedContext:
    def test_only_columns_of_referenced_tables(self):
        text = columns_for_tables(_COLUMNS, {"factsales"})
        assert "FactSales.SalesAmount" in text and "FactSales.OrderYear" in text
        assert "DimProduct" not in text

    def test_unknown_tables_fall_back_to_the_whole_block(self):
        assert columns_for_tables(_COLUMNS, {"nope"}) == _COLUMNS

    def test_relationships_are_filtered_the_same_way(self):
        rels = "FactSales.ProductKey -> DimProduct.ProductKey\nDimDate.Key -> FactOrders.DateKey"
        assert relationships_for_tables(rels, {"factsales"}) == "FactSales.ProductKey -> DimProduct.ProductKey"
        assert relationships_for_tables(rels, {"zzz"}) == "(none)"

    def test_relationships_in_the_loaders_single_line_format(self):
        """The metadata loader emits one ``[('a.x -> b.y',), ...]`` literal."""
        rels = (
            "[('FactSales.ProductKey -> DimProduct.ProductKey',), "
            "('DimDate.Key -> FactOrders.DateKey',), "
            "('FactSales.DateKey -> DimDate.Key',)]"
        )
        assert relationships_for_tables(rels, {"factsales"}) == (
            "FactSales.ProductKey -> DimProduct.ProductKey\nFactSales.DateKey -> DimDate.Key"
        )
        assert relationships_for_tables(rels, {"dimdate"}) == (
            "DimDate.Key -> FactOrders.DateKey\nFactSales.DateKey -> DimDate.Key"
        )
        assert relationships_for_tables(rels, {"zzz"}) == "(none)"

    def test_short_table_names_do_not_match_inside_other_identifiers(self):
        rels = "[('orders.customer_id -> customers.id',), ('items.order_id -> orders.id',)]"
        assert relationships_for_tables(rels, {"id"}) == "(none)"
        assert relationships_for_tables(rels, {"items"}) == "items.order_id -> orders.id"

    def test_a_column_named_like_a_table_is_not_a_table_match(self):
        rels = "[('orders.status -> statuses.code',)]"
        assert relationships_for_tables(rels, {"status"}) == "(none)"


class TestFallbackTokens:
    def test_unparseable_sql_only_yields_catalog_tables(self):
        found = referenced_tables(
            "SELEC x FRM FactSales WHERE (", "postgresql", known_tables=["FactSales", "DimProduct"]
        )
        assert found == {"factsales"}


class TestHeaderStripping:
    @pytest.mark.asyncio
    async def test_header_is_removed_before_values_are_substituted(self):
        from src.agent.langgraph_agent.prompt_loader import PromptLoader

        loader = PromptLoader()
        rendered = await loader.arender(
            "sql_repair",
            strip_header=True,
            question="q", failing_sql="SELECT 1 -- <!-- nope --> trailing", error="bad --> worse",
            database_type="postgresql", dialect_rules="", connection_display_name="d",
            connection_database="db", connection_catalog="c", connection_schema="s",
            columns="", relationships="", filter_plan="{}",
        )
        assert rendered.lstrip().startswith("You repair one SQL statement")
        assert "SELECT 1 -- <!-- nope --> trailing" in rendered
        assert "bad --> worse" in rendered


class TestRouting:
    @pytest.mark.parametrize("feedback", ["syntax", "exec"])
    def test_repair_flag_routes_sql_errors_to_the_repair_node(self, feedback):
        assert _route_from_feedback({"feedback_type": feedback, "use_local_repair": True}) == "sql_repair"
        assert _route_from_feedback({"feedback_type": feedback, "use_local_repair": False}) == "sql_generator"

    @pytest.mark.parametrize("feedback", ["semantic", "empty_recheck"])
    def test_other_retries_never_use_the_repair_node(self, feedback):
        assert _route_from_feedback({"feedback_type": feedback, "use_local_repair": True}) == "sql_generator"

    def test_existing_routes_are_unchanged(self):
        assert _route_from_feedback({"feedback_type": "exhausted"}) == "response_formatter"
        assert _route_from_feedback({"feedback_type": "missing_table"}) == "catalog_lookup"
        assert _route_from_feedback({"feedback_type": "resolve_filters"}) == "filter_grounder"

    def test_an_unusable_edit_goes_to_the_generator(self):
        assert _route_from_sql_repair({"repair_failed": True}) == "sql_generator"
        assert _route_from_sql_repair({"repair_failed": False}) == "sqlglot_validate"
