"""Unit tests for the deterministic hybrid schema linker."""

from __future__ import annotations

from src.metadata.schema_linker import (
    _split_identifier,
    _tokenize,
    link_bundle,
)


def _make_bundle(n_tables: int, cols_per_table: int) -> dict:
    """Build a formatted bundle shaped like MetadataLoader output."""
    table_lines = []
    column_lines = []
    for t in range(n_tables):
        name = f"Table{t}"
        table_lines.append(f"- {name} - description for table {t}")
        for c in range(cols_per_table):
            column_lines.append(
                f"- {name}.col{c} - Type: int, Description: metric {c}"
            )
    return {
        "tables": "\n".join(table_lines),
        "columns": "\n".join(column_lines),
        "relationships": "No relationships registered.",
        "sources": "- src",
        "knowledge_pairs": "No knowledge pairs registered.",
        "business_terms": "No business terms registered.",
    }


class TestTokenize:
    def test_split_camel_and_snake(self):
        assert _split_identifier("DimProduct") == ["dim", "product"]
        assert _split_identifier("fact_sales") == ["fact", "sales"]
        assert _split_identifier("orderYear2020") == ["order", "year", "2020"]

    def test_tokenize_drops_stopwords_and_short(self):
        toks = _tokenize("Show me the total sales by product")
        assert "sales" in toks
        assert "product" in toks
        # stopwords / generic terms removed
        assert "the" not in toks
        assert "show" not in toks
        assert "total" not in toks

    def test_plural_bridge(self):
        assert "order" in _tokenize("orders")


class TestLinkBundleSmallSchema:
    def test_small_schema_passthrough(self):
        # 3 tables x 3 cols = 9 columns, under the default min_columns=60.
        bundle = _make_bundle(3, 3)
        out, pruned = link_bundle(bundle, "sales by product", min_columns=60)
        assert pruned is False
        assert out is bundle  # unchanged object


class TestLinkBundleLargeSchema:
    def test_prunes_to_relevant_tables(self):
        # Build a large schema; make one table clearly relevant to the question.
        bundle = _make_bundle(30, 5)  # 150 columns
        # Inject a table with a matching name/columns.
        bundle["tables"] += "\n- SalesOrders - customer orders and revenue"
        bundle["columns"] += (
            "\n- SalesOrders.revenue - Type: float, Description: order revenue"
            "\n- SalesOrders.customer - Type: text, Description: customer name"
        )
        out, pruned = link_bundle(
            bundle,
            "what is the total revenue for sales orders",
            min_columns=60,
            max_tables=5,
        )
        assert pruned is True
        assert "SalesOrders" in out["tables"]
        assert "revenue" in out["columns"].lower()
        # The pruned view is much smaller than the original.
        assert out["columns"].count("\n") < bundle["columns"].count("\n")
        # A relevance note is appended.
        assert "most relevant" in out["tables"]

    def test_prunes_column_statistics_and_samples_with_columns(self):
        bundle = _make_bundle(30, 5)
        bundle["tables"] += "\n- SalesOrders - customer orders and revenue"
        bundle["columns"] += (
            "\n- SalesOrders.revenue - Type: decimal, Description: order revenue"
            "\n- SalesOrders.region - Type: text, Description: sales region"
        )
        bundle["column_statistics"] = (
            "- Table0.col0 - min: 0, max: 1\n"
            "- SalesOrders.revenue - min: 1, max: 100\n"
        )
        bundle["column_samples"] = (
            "- Table0.col0 - internal\n"
            "- SalesOrders.region - EMEA, APAC\n"
        )

        out, pruned = link_bundle(
            bundle, "revenue by sales region", min_columns=60, max_tables=5
        )

        assert pruned is True
        assert "SalesOrders.revenue" in out["column_statistics"]
        assert "Table0.col0" not in out["column_statistics"]
        assert "SalesOrders.region" in out["column_samples"]
        assert "Table0.col0" not in out["column_samples"]

    def test_schema_qualified_catalog_keeps_matching_columns(self):
        bundle = _make_bundle(30, 5)
        bundle["tables"] += '\n- "public"."DimTerritory" - sales regions'
        bundle["columns"] += (
            '\n- "public"."DimTerritory"."RegionName" - Type: text, Description: region'
            '\n- "public"."DimTerritory"."SalesAmount" - Type: decimal, Description: sales'
        )

        out, pruned = link_bundle(
            bundle,
            "sales by region",
            min_columns=60,
            max_tables=5,
        )

        assert pruned is True
        assert '"public"."DimTerritory"' in out["tables"]
        assert '"public"."DimTerritory"."RegionName"' in out["columns"]

    def test_table_cap_respected(self):
        bundle = _make_bundle(40, 3)  # 120 columns, all similarly named
        out, pruned = link_bundle(
            bundle,
            "table5 col1 col2",  # match a few
            min_columns=60,
            max_tables=3,
        )
        if pruned:
            # Count kept table lines (exclude the note line).
            kept = [
                ln for ln in out["tables"].splitlines()
                if ln.strip().startswith("-")
            ]
            assert len(kept) <= 3

    def test_no_question_signal_passthrough(self):
        bundle = _make_bundle(30, 5)
        out, pruned = link_bundle(bundle, "the a of for", min_columns=60)
        assert pruned is False
        assert out is bundle

    def test_no_lexical_hits_passthrough(self):
        bundle = _make_bundle(30, 5)
        out, pruned = link_bundle(
            bundle, "zzz_unrelated_topic_qqq", min_columns=60
        )
        assert pruned is False

    def test_global_column_cap(self):
        bundle = _make_bundle(30, 50)  # 1500 columns
        bundle["tables"] += "\n- SalesOrders - revenue"
        cols = "\n".join(
            f"- SalesOrders.metric{i} - Type: int, Description: revenue metric"
            for i in range(100)
        )
        bundle["columns"] += "\n" + cols
        out, pruned = link_bundle(
            bundle,
            "revenue metric sales orders",
            min_columns=60,
            max_tables=20,
            max_columns=30,
        )
        assert pruned is True
        kept = [ln for ln in out["columns"].splitlines() if ln.strip().startswith("-")]
        assert len(kept) <= 30


class TestRelationshipExpansion:
    def test_neighbour_pulled_in(self):
        bundle = _make_bundle(30, 4)
        bundle["tables"] += "\n- Orders - customer orders"
        bundle["tables"] += "\n- Customers - customer directory"
        bundle["columns"] += "\n- Orders.total - Type: float, Description: order total"
        bundle["columns"] += "\n- Customers.region - Type: text, Description: region"
        # Relationship connecting Orders <-> Customers.
        bundle["relationships"] = "[('Orders references Customers',)]"
        out, pruned = link_bundle(
            bundle,
            "order total by customer",  # 'customer' matches Customers too
            min_columns=60,
            max_tables=20,
        )
        assert pruned is True
        # Both related tables should appear.
        assert "Orders" in out["tables"]
        assert "Customers" in out["tables"]
