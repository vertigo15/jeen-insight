"""Unit tests for the dax_entity_resolver node (value linking before generation)."""

from __future__ import annotations

import pytest

from src.agent.langgraph_agent_dax.nodes import entity_resolver
from src.agent.langgraph_agent_dax.nodes.entity_resolver import (
    build_clarification,
    column_display_names,
    column_types,
    is_text_type,
    make_dax_entity_resolver,
    parse_target,
)
from src.connectors.powerbi_probe import ProbeResult
from src.metadata.value_index import value_domain_cache

PRODUCTS = [
    "Mountain-300 Black, 38",
    "Mountain-300 Black, 40",
    "Mountain-300 Black, 44",
    "Mountain-500 Silver, 40",
    "Mountain-200 Black, 42",
    "Mountain-3000 Pro, 42",
    "Road-250 Red, 44",
]

MODELS = ["Mountain-300", "Mountain-500", "Road-250"]

COLUMNS_TEXT = "\n".join(
    [
        "- Product.Product Name - Type: string, Description: SKU name",
        "- Product.Model Name - Type: string",
        "- Product.List Price - Type: decimal",
        "- Product.Sell Start Date - Type: date",
        "- Product.Social Security Number - Type: string",
        "- Sales.Amount - Type: measure",
    ]
)


class _Probe:
    """Stands in for PowerBiValueProbe, recording what was asked of it."""

    def __init__(self, domains=None, *, complete=True, contains=None, identity=""):
        self.domains = domains or {}
        self.contains_domains = contains or {}
        self.complete = complete
        self.identity = identity
        self.distinct_calls = []
        self.contains_calls = []

    async def authorize(self):
        return self.identity

    async def distinct_values(self, table, column, *, limit):
        self.distinct_calls.append((table, column, limit))
        return ProbeResult(tuple(self.domains.get((table, column), ())), self.complete, True)

    async def contains_values(self, table, column, fragments, *, limit):
        self.contains_calls.append((table, column, list(fragments)))
        return ProbeResult(tuple(self.contains_domains.get((table, column), ())), True, True)


def _state(filters, **overrides):
    state = {
        "question": "Sales for Mountaiin 300 per year",
        "source_key": "AdventureWorks",
        "user_id": "42",
        "workspace_id": "ws",
        "dataset_id": "ds",
        "metadata_bundle": {"columns": COLUMNS_TEXT},
        "table_columns": {
            "product": ["product name", "model name", "list price", "social security number"]
        },
        "query_plan": {
            "grain": "aggregate",
            "filters": filters,
            "assumptions": ["Used the curated measure."],
        },
        "plan_assumptions": ["Used the curated measure."],
        "entity_resolution_attempts": 0,
    }
    state.update(overrides)
    return state


def _node(probe, **kwargs):
    return make_dax_entity_resolver(True, probe_factory=lambda state: probe, **kwargs)


@pytest.fixture(autouse=True)
def _clear_cache():
    value_domain_cache.clear()
    yield
    value_domain_cache.clear()


@pytest.fixture
def probe():
    """Hand back the fake probe the node should use.

    The node takes its probe factory by injection (see ``_node``), so no Power
    BI call is ever attempted and nothing has to be patched into the module.
    """
    return lambda p: p


# ── Catalog parsing helpers ───────────────────────────────────────────────────


class TestCatalogHelpers:
    def test_parse_quoted_target(self):
        assert parse_target("'Product'[Product Name]") == ("Product", "Product Name")

    def test_parse_unquoted_target(self):
        assert parse_target("Product[Model Name]") == ("Product", "Model Name")

    def test_parse_rejects_bare_measure(self):
        assert parse_target("[Total Sales]") is None
        assert parse_target("not a target") is None

    def test_column_types_extracted(self):
        types = column_types(COLUMNS_TEXT)
        assert types[("product", "product name")] == "string"
        assert types[("product", "list price")] == "decimal"

    def test_display_names_preserve_casing(self):
        names = column_display_names(COLUMNS_TEXT)
        assert names[("product", "product name")] == ("Product", "Product Name")

    def test_unknown_type_counts_as_text(self):
        """Curated catalogs often omit the type; skipping those would disable
        resolution exactly where it is needed most."""
        assert is_text_type("") is True
        assert is_text_type("nvarchar(50)") is True
        assert is_text_type("decimal") is False


# ── Resolution outcomes ───────────────────────────────────────────────────────


class TestResolution:
    async def test_mcp_candidate_is_confirmed_by_delegated_probe(self, monkeypatch, probe):
        p = probe(
            _Probe(
                contains={("Product", "Product Name"): ["Mountain-300"]}
            )
        )

        async def candidates(_table, _column, _needle):
            return ["Mountain-300"]

        monkeypatch.setattr(
            entity_resolver,
            "_mcp_search_for_state",
            lambda _state: candidates,
        )
        out = await _node(p)(
            _state(
                [{
                    "target": "'Product'[Product Name]",
                    "op": "equals",
                    "value": "mountaiin 300",
                }],
                catalog_source_used="mcp",
            )
        )

        assert p.distinct_calls == []
        assert p.contains_calls == [
            ("Product", "Product Name", ["moun", "300"])
        ]
        assert out["query_plan"]["filters"][0]["value"] == "Mountain-300"
        assert out["query_plan"]["filters"][0]["resolved"] is True

    async def test_typo_resolves_to_the_matching_sizes(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountaiin 300"}])
        )
        resolved = out["query_plan"]["filters"][0]
        assert resolved["op"] == "in"
        assert resolved["value"] == [
            "Mountain-300 Black, 38",
            "Mountain-300 Black, 40",
            "Mountain-300 Black, 44",
        ]
        assert resolved["resolved"] is True
        assert not out["entity_ambiguities"]
        assert "clarification" not in out

    async def test_correction_is_stated_as_an_assumption(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountaiin 300"}])
        )
        assumptions = out["query_plan"]["assumptions"]
        assert assumptions[0] == "Used the curated measure."
        assert any("Mountaiin 300" in a for a in assumptions[1:])
        assert out["plan_assumptions"] == assumptions

    async def test_single_match_becomes_equals(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}])
        )
        resolved = out["query_plan"]["filters"][0]
        assert resolved["op"] == "equals"
        assert resolved["value"] == "Road-250 Red, 44"

    async def test_exact_value_is_marked_resolved_without_changing_it(self, probe):
        """Even a perfect spelling gets the flag: it is what tells the generator
        to use the literal verbatim, and stops a later pass re-probing it."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        state = _state(
            [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road-250 Red, 44"}]
        )
        out = await _node(p)(state)
        resolved = out["query_plan"]["filters"][0]
        assert resolved["value"] == "Road-250 Red, 44"
        assert resolved["resolved"] is True
        assert out["query_plan"]["assumptions"] == ["Used the curated measure."]
        assert out["resolved_entities"][0]["status"] == "verified"

    async def test_differing_case_is_canonicalised(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "road-250 red, 44"}])
        )
        assert out["query_plan"]["filters"][0]["value"] == "Road-250 Red, 44"

    async def test_ambiguous_single_token_asks_the_user(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain"}])
        )
        assert out["clarification_required"] is True
        assert "Mountain" in out["clarification"]
        assert out["answer"] == out["clarification"]
        assert out["entity_ambiguities"]

    async def test_near_miss_offers_the_closest_ones(self, probe):
        """A value that does not exist but resembles several that do is a
        question, not an empty table."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain-900"}])
        )
        assert out["clarification_required"] is True
        assert "could refer to" in out["clarification"]
        assert "Mountain-300 Black, 38" in out["clarification"]

    async def test_value_absent_from_the_model_is_reported_as_missing(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}), )
        out = await _node(p, cross_column_enabled=False)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Zebra Trike"}])
        )
        assert out["clarification_required"] is True
        assert "couldn't find" in out["clarification"]

    async def test_value_found_on_a_sibling_column(self, probe):
        """"Mountain 300" is a model, not a product name."""
        p = probe(
            _Probe(
                {("Product", "Product Name"): ["Water Bottle", "Sport Helmet"]},
                contains={("Product", "Model Name"): MODELS},
            )
        )
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}])
        )
        resolved = out["query_plan"]["filters"][0]
        assert resolved["target"] == "'Product'[Model Name]"
        assert resolved["value"] == "Mountain-300"
        assert not out["entity_ambiguities"]

    async def test_governed_column_is_never_probed(self, probe):
        """No operator configuration: a spaced Power BI display name has to trip
        the built-in policy on its own, since reading a column's values is a
        data-egress path the SQL-shaped patterns were never written for."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state(
                [
                    {
                        "target": "'Product'[Social Security Number]",
                        "op": "equals",
                        "value": "some value",
                    }
                ]
            )
        )
        assert p.distinct_calls == []
        assert out["unresolved_entities"][0]["reason"] == "governed column"

    async def test_a_governed_sibling_is_not_searched_either(self, probe):
        p = probe(
            _Probe(
                {("Product", "Product Name"): ["Water Bottle"]},
                contains={("Product", "Social Security Number"): ["Mountain-300"]},
            )
        )
        await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}])
        )
        probed = {column for _, column, _ in p.contains_calls}
        assert "Social Security Number" not in probed

    async def test_oversized_domain_does_not_claim_the_value_is_missing(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}, complete=False))
        out = await _node(p, cross_column_enabled=False)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Zebra 900"}])
        )
        assert "clarification" not in out
        assert out["unresolved_entities"][0]["reason"] == "domain too large"

    async def test_list_valued_in_filter_resolves_every_literal(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "in",
                        "value": ["Mountaiin 300", "Road 250"],
                    }
                ]
            )
        )
        resolved = out["query_plan"]["filters"][0]
        assert resolved["op"] == "in"
        assert "Road-250 Red, 44" in resolved["value"]
        assert "Mountain-300 Black, 38" in resolved["value"]

    async def test_one_bad_literal_in_a_list_asks_rather_than_dropping_it(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p, cross_column_enabled=False)(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "in",
                        "value": ["Road 250", "Zebra Trike"],
                    }
                ]
            )
        )
        assert out["clarification_required"] is True
        assert "Zebra Trike" in out["clarification"]
        assert "query_plan" not in out


# ── Guards against silently answering a wider question than was asked ─────────


class TestWideningSafety:
    """Widening one literal to an ``IN`` set is the only change here that can be
    wrong without looking wrong: the query still succeeds and still returns a
    plausible number, just for products nobody asked about. Each rule below
    exists to keep that from happening quietly."""

    async def test_a_different_model_number_is_not_folded_in(self, probe):
        """"300" resembles "3000" strongly enough for any typo threshold, but a
        different number is a different product, not a misspelling."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}])
        )
        values = out["query_plan"]["filters"][0]["value"]
        assert values == [
            "Mountain-300 Black, 38",
            "Mountain-300 Black, 40",
            "Mountain-300 Black, 44",
        ]
        assert "Mountain-3000 Pro, 42" not in values

    async def test_a_typo_in_the_word_is_still_forgiven(self, probe):
        """The numeric rule must not cost us ordinary typo tolerance."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Montain 300"}])
        )
        assert len(out["query_plan"]["filters"][0]["value"]) == 3

    async def test_a_truncated_domain_never_drives_a_rewrite(self, probe):
        """The first N values alphabetically cannot show whether a better match,
        or a fourth size, sits past the cap."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}, complete=False))
        out = await _node(p, cross_column_enabled=False)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}])
        )
        assert "query_plan" not in out
        assert out["unresolved_entities"][0]["reason"] == "domain too large"

    async def test_an_exact_hit_inside_a_truncated_domain_is_still_trusted(self, probe):
        """Failing open on truncation must not throw away a value we did see."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}, complete=False))
        out = await _node(p)(
            _state(
                [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road-250 Red, 44"}]
            )
        )
        assert out["query_plan"]["filters"][0]["value"] == "Road-250 Red, 44"

    async def test_a_sibling_column_cannot_widen_a_single_word(self, probe):
        """"Bike" covers "Bikes", "Bike Racks" and "Bike Stands" — three
        different things. The target column having no match is no reason to
        relax the rule when searching elsewhere."""
        p = probe(
            _Probe(
                {("Product", "Product Name"): ["Water Bottle"]},
                contains={("Product", "Model Name"): ["Bikes", "Bike Racks", "Bike Stands"]},
            )
        )
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Bike"}])
        )
        assert "query_plan" not in out
        assert out["clarification_required"] is True
        assert "Bike Racks" in out["clarification"]

    async def test_a_truncated_sibling_search_is_inconclusive(self, probe):
        """A capped CONTAINSSTRING shows some matches, never all of them — so it
        can neither rewrite the filter nor prove the value exists nowhere."""

        class _Truncated(_Probe):
            async def contains_values(self, table, column, fragments, *, limit):
                self.contains_calls.append((table, column, list(fragments)))
                return ProbeResult(("Mountain-300",), False, True)

        p = probe(_Truncated({("Product", "Product Name"): ["Water Bottle"]}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}])
        )
        assert "query_plan" not in out
        assert "clarification" not in out
        assert out["unresolved_entities"][0]["reason"] == "cross-column search incomplete"

    async def test_a_failed_sibling_search_never_claims_the_value_is_missing(self, probe):
        class _Down(_Probe):
            async def contains_values(self, table, column, fragments, *, limit):
                return ProbeResult.failed()

        p = probe(_Down({("Product", "Product Name"): ["Water Bottle"]}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}])
        )
        assert "clarification" not in out
        assert out["unresolved_entities"][0]["reason"] == "cross-column search incomplete"

    async def test_one_sibling_failing_does_not_turn_a_match_into_a_question(self, probe):
        """Model Name has the answer but Category's probe fell over. Asking now
        would let a transient failure decide whether the user is interrupted,
        and the question would rest on evidence known to be partial."""

        class _HalfDown(_Probe):
            async def contains_values(self, table, column, fragments, *, limit):
                self.contains_calls.append((table, column, list(fragments)))
                if column == "Category":
                    return ProbeResult.failed()
                return ProbeResult(("Mountain-300",), True, True)

        p = probe(_HalfDown({("Product", "Product Name"): ["Water Bottle"]}))
        state = _state(
            [{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}],
            table_columns={"product": ["product name", "model name", "category"]},
            metadata_bundle={"columns": COLUMNS_TEXT + "\n- Product.Category - Type: string"},
        )
        out = await _node(p)(state)
        assert "clarification" not in out
        assert "query_plan" not in out
        assert out["unresolved_entities"][0]["reason"] == "cross-column search incomplete"

    async def test_a_competing_sibling_blocks_automatic_retargeting(self, probe):
        """One column matching exactly is not enough when another column also
        has candidates: retargeting changes what the question is about."""
        p = probe(
            _Probe(
                {("Product", "Product Name"): ["Water Bottle"]},
                contains={
                    ("Product", "Model Name"): ["Mountain-300"],
                    ("Product", "Category"): ["Mountain-300 Spares", "Mountain-300 Kits"],
                },
            ),
        )
        state = _state(
            [{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountain 300"}],
            table_columns={"product": ["product name", "model name", "category"]},
            metadata_bundle={"columns": COLUMNS_TEXT + "\n- Product.Category - Type: string"},
        )
        out = await _node(p)(state)
        assert "query_plan" not in out
        assert out["clarification_required"] is True
        assert "more than one field" in out["clarification"]

    async def test_several_literals_cannot_add_up_to_an_unbounded_filter(self, probe):
        """Each literal is capped on its own; the total needs capping too. Here
        both literals are individually reasonable and jointly far too broad."""
        wide = [f"Mountain-300 Black, {n}" for n in range(30, 45)]
        wide += [f"Road-250 Red, {n}" for n in range(30, 45)]
        p = probe(_Probe({("Product", "Product Name"): wide}))
        out = await _node(p, cross_column_enabled=False)(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "in",
                        "value": ["Mountain 300 Black", "Road 250 Red"],
                    }
                ]
            )
        )
        assert "query_plan" not in out
        assert out["unresolved_entities"][0]["reason"] == "too many matching values"

    async def test_the_same_value_reached_twice_appears_once(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "in",
                        "value": ["Road 250", "Road-250 Red, 44"],
                    }
                ]
            )
        )
        assert out["query_plan"]["filters"][0]["value"] == "Road-250 Red, 44"


# ── Filters that must be left alone ───────────────────────────────────────────


class TestSkipRules:
    async def test_numeric_literal_is_not_probed(self, probe):
        p = probe(_Probe())
        out = await _node(p)(
            _state([{"target": "'Product'[List Price]", "op": "equals", "value": "1200"}])
        )
        assert p.distinct_calls == []
        assert out["unresolved_entities"] == []
        assert "query_plan" not in out

    async def test_invalid_date_literal_is_not_probed_and_requests_clarification(self, probe):
        p = probe(_Probe())
        out = await _node(p)(
            _state(
                [{"target": "'Product'[Sell Start Date]", "op": "equals", "value": "January 2020"}]
            )
        )
        assert p.distinct_calls == []
        assert out["clarification_required"] is True
        assert "couldn't find" in out["clarification"].lower()

    async def test_iso_date_range_is_normalised_without_a_value_probe(self, probe):
        p = probe(_Probe())
        out = await _node(p)(
            _state(
                [{
                    "target": "'Product'[Sell Start Date]",
                    "op": "between",
                    "value": ["2026-01-01", "2026-01-31"],
                    "value_kind": "expression",
                }]
            )
        )
        assert p.distinct_calls == []
        assert out["query_plan"]["filters"][0]["value"] == [
            "2026-01-01", "2026-01-31"
        ]
        assert out["query_plan"]["filters"][0]["resolved"] is True

    async def test_range_operator_is_ignored(self, probe):
        p = probe(_Probe())
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "between", "value": "A"}])
        )
        assert p.distinct_calls == []
        assert "query_plan" not in out

    async def test_expression_value_kind_is_ignored(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        await _node(p)(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "equals",
                        "value": "Mountaiin 300",
                        "value_kind": "expression",
                    }
                ]
            )
        )
        assert p.distinct_calls == []

    async def test_already_resolved_filter_is_not_reprocessed(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        await _node(p)(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "equals",
                        "value": "Mountaiin 300",
                        "resolved": True,
                    }
                ]
            )
        )
        assert p.distinct_calls == []

    async def test_plan_without_filters_is_a_no_op(self, probe):
        p = probe(_Probe())
        out = await _node(p)(_state([]))
        assert "query_plan" not in out
        assert "clarification" not in out

    async def test_no_op_clears_stale_findings_from_an_earlier_pass(self, probe):
        """A leftover unresolved entry would keep sending the feedback router back."""
        p = probe(_Probe())
        out = await _node(p)(
            _state([], unresolved_entities=[{"target": "'Product'[Name]", "value": "old"}])
        )
        assert out["unresolved_entities"] == []
        assert out["entity_ambiguities"] == []


# ── Failing open ──────────────────────────────────────────────────────────────


def _never_probe(state):
    pytest.fail("must not probe")


class TestFailOpen:
    async def test_disabled_node_does_nothing(self):
        node = make_dax_entity_resolver(False, probe_factory=_never_probe)
        out = await node(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "x y"}])
        )
        assert out == {"entity_resolution_attempts": 1}
        assert "query_plan" not in out

    async def test_state_switch_disables_a_node_built_enabled(self):
        """The admin kill switch must stop a resolver that was deployed on."""
        node = make_dax_entity_resolver(True, probe_factory=_never_probe)
        out = await node(
            _state(
                [{"target": "'Product'[Product Name]", "op": "equals", "value": "x y"}],
                entity_resolution_enabled=False,
            )
        )
        assert out == {"entity_resolution_attempts": 1}

    async def test_state_switch_enables_a_node_built_disabled(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        node = make_dax_entity_resolver(False, probe_factory=lambda state: p)
        out = await node(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "equals",
                        "value": "Mountain-300 Black, 38",
                    }
                ],
                entity_resolution_enabled=True,
            )
        )
        assert p.distinct_calls, "state override should re-enable probing"
        assert out["resolved_entities"]

    async def test_absent_snapshot_falls_back_to_build_time_settings(self, probe):
        """Graphs driven directly (tests, evals) seed no snapshot."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        node = _node(p, max_domain_values=77)
        await node(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "equals",
                        "value": "Mountaiin 300 Black, 38",
                    }
                ]
            )
        )
        assert p.distinct_calls[0][2] == 77

    async def test_state_snapshot_overrides_the_domain_ceiling(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        node = _node(p, max_domain_values=77)
        await node(
            _state(
                [
                    {
                        "target": "'Product'[Product Name]",
                        "op": "equals",
                        "value": "Mountaiin 300 Black, 38",
                    }
                ],
                entity_max_domain_values=5,
            )
        )
        assert p.distinct_calls[0][2] == 5

    async def test_unavailable_probe_leaves_the_plan_intact(self):
        """A deployment with no connector platform gets no probe at all."""
        state = _state(
            [{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountaiin 300"}]
        )
        node = make_dax_entity_resolver(True, probe_factory=lambda state: None)
        out = await node(state)
        assert "query_plan" not in out
        assert "clarification" not in out
        assert out["unresolved_entities"][0]["reason"] == "no probe"

    async def test_a_failing_authorization_check_does_not_break_the_flow(self, probe):
        """The identity lookup hits the grant store, which can be down."""

        class _Broken(_Probe):
            async def authorize(self):
                raise RuntimeError("identity store unreachable")

        p = probe(_Broken({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}])
        )
        assert "clarification" not in out
        assert out["unresolved_entities"][0]["reason"] == "no probe"

    async def test_probe_error_does_not_break_the_flow(self, probe):
        class _Boom(_Probe):
            async def distinct_values(self, table, column, *, limit):
                raise RuntimeError("power bi is down")

        p = probe(_Boom())
        out = await _node(p)(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountaiin 300"}])
        )
        assert "clarification" not in out
        assert out["unresolved_entities"][0]["reason"] == "probe error"

    async def test_attempts_counter_increments(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        out = await _node(p)(
            _state(
                [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}],
                entity_resolution_attempts=1,
            )
        )
        assert out["entity_resolution_attempts"] == 2


# ── Caching ───────────────────────────────────────────────────────────────────


class TestCaching:
    async def test_second_question_reuses_the_domain(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        node = _node(p)
        await node(_state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}]))
        await node(
            _state([{"target": "'Product'[Product Name]", "op": "equals", "value": "Mountaiin 300"}])
        )
        assert len(p.distinct_calls) == 1

    async def test_a_different_user_refetches(self, probe):
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        node = _node(p)
        base = [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}]
        await node(_state(base, user_id="1"))
        await node(_state(base, user_id="2"))
        assert len(p.distinct_calls) == 2

    async def test_a_different_dataset_refetches(self, probe):
        """One connection can be retargeted; values must not survive that."""
        p = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        node = _node(p)
        base = [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}]
        await node(_state(base, dataset_id="ds1"))
        await node(_state(base, dataset_id="ds2"))
        assert len(p.distinct_calls) == 2

    async def test_a_failed_probe_is_not_cached(self, probe):
        """Caching a failure would blind the feedback router's retry, which
        exists precisely because the first attempt could not verify anything."""

        class _Down(_Probe):
            async def distinct_values(self, table, column, *, limit):
                self.distinct_calls.append((table, column, limit))
                return ProbeResult.failed()

        p = probe(_Down())
        node = _node(p)
        base = [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}]
        await node(_state(base))
        out = await node(_state(base))
        assert len(p.distinct_calls) == 2
        assert out["unresolved_entities"][0]["reason"] == "probe error"

    async def test_a_revoked_reader_gets_nothing_from_the_cache(self, probe):
        """A cache hit performs no authorization of its own, and a clarification
        answer never reaches the execution node that would have re-checked."""
        base = [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}]
        warm = probe(_Probe({("Product", "Product Name"): PRODUCTS}))
        await _node(warm)(_state(base))

        revoked = probe(_Probe({("Product", "Product Name"): PRODUCTS}, identity=None))
        out = await _node(revoked)(_state(base))
        assert revoked.distinct_calls == []
        assert "query_plan" not in out
        assert out["unresolved_entities"][0]["reason"] == "no probe"

    async def test_relinking_a_different_power_bi_identity_refetches(self, probe):
        """Same app user, different grant: the previous identity's RLS view must
        not carry over."""
        base = [{"target": "'Product'[Product Name]", "op": "equals", "value": "Road 250"}]
        first = probe(_Probe({("Product", "Product Name"): PRODUCTS}, identity="grant-a|ann"))
        await _node(first)(_state(base))
        second = probe(_Probe({("Product", "Product Name"): PRODUCTS}, identity="grant-b|bob"))
        await _node(second)(_state(base))
        assert len(second.distinct_calls) == 1


# ── Clarification wording ─────────────────────────────────────────────────────


class TestClarificationText:
    def test_missing_value_names_the_alternatives(self):
        text = build_clarification(
            [],
            [{"column": "Product Name", "value": "Mountain-900", "candidates": ["Mountain-300", "Mountain-500"]}],
        )
        assert "Mountain-900" in text
        assert "'Mountain-300' or 'Mountain-500'" in text

    def test_ambiguous_value_asks_which_one(self):
        text = build_clarification(
            [{"column": "Product Name", "value": "Mountain", "candidates": ["A", "B", "C"]}], []
        )
        assert "Which did you mean?" in text
        assert "'A', 'B' or 'C'" in text

    def test_cross_column_ambiguity_names_the_fields(self):
        text = build_clarification(
            [
                {
                    "column": "Product Name",
                    "value": "Mountain 300",
                    "columns": ["Product[Model Name]", "Product[Category]"],
                }
            ],
            [],
        )
        assert "more than one field" in text
        assert "Product[Model Name]" in text

    def test_a_single_other_field_names_its_values(self):
        """Reporting "not found" when we know where the value lives wastes the
        one question we get to ask."""
        text = build_clarification(
            [
                {
                    "column": "Product Name",
                    "value": "Bike",
                    "columns": ["Product[Model Name]"],
                    "candidates": ["Bikes", "Bike Racks"],
                }
            ],
            [],
        )
        assert "isn't a Product Name" in text
        assert "Product[Model Name]" in text
        assert "'Bikes' or 'Bike Racks'" in text

    def test_no_close_values_says_so(self):
        text = build_clarification([], [{"column": "Product Name", "value": "Zebra", "candidates": []}])
        assert "nothing in that column looks close" in text
