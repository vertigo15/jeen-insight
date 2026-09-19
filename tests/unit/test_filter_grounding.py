"""Focused regression tests for SQL filter planning and grounding."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import AsyncMock

import pytest

from src.agent.langgraph_agent.nodes import filtering
from src.agent.langgraph_agent.nodes.filtering import (
    SqlValueProbe,
    decide_column,
    empty_filter_result_check,
    extract_candidate_literals,
    make_filter_grounder,
    make_filter_planner,
    normalize_typed_filter,
)
from src.metadata.value_index import value_domain_cache
from src.metadata.value_store import (
    CaptureContract,
    ColumnHit,
    ColumnProfile,
    DomainEvidence,
    Relationship,
)


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _Runner:
    database_type = "postgres"
    supports_server_side_timeout = True

    def __init__(self, values=(), *, exists: Optional[bool] = None):
        self.values = list(values)
        self.exists = exists
        self.sql = ""
        self.calls: List[str] = []

    async def run_sql(self, sql, **_kwargs):
        self.sql = sql
        self.calls.append(sql)
        if "SELECT 1 AS present" in sql:
            return {"rows": [{"present": 1}] if self.exists else []}
        return {"rows": [{"value": value} for value in self.values]}


class _Store:
    available = True

    def __init__(self, *, profiles=None, domains=None, hits=None, edges=None):
        self.profiles: Dict[Tuple[str, str], ColumnProfile] = profiles or {}
        self.domains: Dict[Tuple[str, str], DomainEvidence] = domains or {}
        self.hits: List[ColumnHit] = hits or []
        self.edges: List[Relationship] = edges or []
        self.contract = CaptureContract()
        self.domain_calls: List[Tuple[str, str, Optional[str]]] = []

    async def column_profile(self, source, table, column):
        return self.profiles.get((table.lower(), column.lower()))

    async def domain(self, source, table, column, *, needle=None, limit=1000):
        self.domain_calls.append((table, column, needle))
        return self.domains.get((table.lower(), column.lower()))

    async def find_columns_for_value(self, source, needles, *, tables=None, limit=12):
        return list(self.hits)

    async def relationships(self, source):
        return list(self.edges)


def _evidence(values: Sequence[str], *, complete=True, fresh_absence=True, source="captured") -> DomainEvidence:
    return DomainEvidence(
        values=tuple(values), complete=complete, source=source, snapshot="captured:1",
        age_seconds=60.0, fresh_for_existence=True, fresh_for_absence=fresh_absence,
    )


def _hit(table, column, value, similarity=0.8, semantic_type="categorical") -> Dict[str, Any]:
    return {"table": table, "column": column, "value": value, "similarity": similarity,
            "count": 5, "semantic_type": semantic_type, "source": "captured"}


def _state(filters, *, question="show sales in mosco", store_hits=None, **extra):
    state = {
        "question": question,
        "catalog_source_used": "db",
        "source_key": "sales",
        "user_id": "user-1",
        "metadata_bundle": {"columns": "\n".join([
            "- sales.region - Type: text",
            "- dim_customer.city - Type: text, Description: Customer city",
            "- dim_dealer.city - Type: text, Description: Dealer city",
            "- product.name - Type: varchar",
            "- orders.region - Type: text",
            "- orders.status - Type: text",
            "- people.full_name - Type: text",
        ])},
        "table_columns": {
            "sales": ["region", "customer_id", "dealer_id"],
            "dim_customer": ["id", "city"],
            "dim_dealer": ["id", "city"],
            "product": ["name"],
            "orders": ["region", "status"],
            "people": ["full_name"],
        },
        "filter_plan": {"filters": filters},
    }
    state.update(extra)
    return state


def _filter(table, column, value, *, op="equals", data_type="text", candidates=None):
    return {"table": table, "column": column, "op": op, "value": value,
            "data_type": data_type, "resolved": False,
            "candidate_columns": candidates or []}


@pytest.fixture(autouse=True)
def _clear_cache():
    value_domain_cache.clear()
    yield
    value_domain_cache.clear()


def _grounder(runner, store, **kwargs):
    return make_filter_grounder(runner, value_store_provider=lambda _state: store, **kwargs)


# ── Proposed filter targets ───────────────────────────────────────────────────


def test_canonical_target_reduces_qualified_and_quoted_identifiers():
    # The planner echoes identifiers the way the catalog showed them; the
    # allowlist is keyed by the bare, lower-cased table and column.
    assert filtering._canonical_target({"table": "factinternetsales", "column": "orderdate"}) == ("factinternetsales", "orderdate")
    assert filtering._canonical_target({"table": "public.FactInternetSales", "column": "OrderDate"}) == ("factinternetsales", "orderdate")
    assert filtering._canonical_target({"table": '"public"."factinternetsales"', "column": '"orderdate"'}) == ("factinternetsales", "orderdate")
    assert filtering._canonical_target({"target": '"public"."dimdate"."calendaryear"'}) == ("dimdate", "calendaryear")
    assert filtering._canonical_target({"table": "", "column": "x"}) is None


# ── Typed normalisation (unchanged behaviour) ─────────────────────────────────


def test_normalizes_numeric_and_iso_date_ranges():
    number, error = normalize_typed_filter(
        {"op": "between", "value": ["1,000.50", "2000"]}, "decimal"
    )
    assert error is None
    assert number["value"] == ["1000.5", "2000"]
    assert number["resolved"] is True

    dates, error = normalize_typed_filter(
        {"op": "between", "value": ["2026-01-01", "2026-01-31"]}, "date"
    )
    assert error is None
    assert dates["value"] == ["2026-01-01", "2026-01-31"]


def test_rejects_ambiguous_date_format():
    _, error = normalize_typed_filter(
        {"op": "equals", "value": "03/04/2026"}, "date"
    )
    assert "couldn't read" in error.lower()
    _, error = normalize_typed_filter({"op": "between", "value": ["H2 2008", "H1 2009"]}, "date")
    assert "couldn't read that date range" in error.lower()


@pytest.mark.parametrize("literal", [
    "0000", "12/9999", "13/2008", "0/2008", "2008-13", "2008-00", "1899", "2201", "9999",
    "marketing 2008", "octopus 2008", "for 2008", "may be 2008", "q5 2008", "2008 q0",
    "spring 2008", "fy2009", "2008-07-32", "",
])
def test_unreadable_or_out_of_range_date_literals_are_refused_not_raised(literal):
    out, error = normalize_typed_filter({"op": "equals", "value": literal}, "date")
    assert error is not None and out.get("resolved") is not True


def test_month_and_year_literals_cover_their_whole_period():
    dates, error = normalize_typed_filter({"op": "between", "value": ["7/2008", "1/2009"]}, "date")
    assert error is None and dates["value"] == ["2008-07-01", "2009-01-31"] and dates["resolved"] is True
    for spelling in ("2008-07", "2008/7", "Jul 2008", "July 2008", "jul-2008", "2008 Jul", "Q3 2008", "2008-Q3"):
        dates, error = normalize_typed_filter({"op": "between", "value": [spelling, "2008-12"]}, "date")
        assert error is None and dates["value"] == ["2008-07-01", "2008-12-31"], spelling
    dates, _ = normalize_typed_filter({"op": "equals", "value": "Q4 2008"}, "date")
    assert dates["op"] == "between" and dates["value"] == ["2008-10-01", "2008-12-31"]
    assert normalize_typed_filter({"op": "<=", "value": "Sept 2008"}, "date")[0]["value"] == "2008-09-30"
    dates, _ = normalize_typed_filter({"op": "equals", "value": "July 2008"}, "date")
    assert dates["op"] == "between" and dates["value"] == ["2008-07-01", "2008-07-31"]
    dates, _ = normalize_typed_filter({"op": "equals", "value": "2008"}, "date")
    assert dates["op"] == "between" and dates["value"] == ["2008-01-01", "2008-12-31"]
    assert normalize_typed_filter({"op": ">=", "value": "2008-07"}, "date")[0]["value"] == "2008-07-01"
    assert normalize_typed_filter({"op": "<", "value": "2008-07"}, "date")[0]["value"] == "2008-07-01"
    assert normalize_typed_filter({"op": ">", "value": "Jul 2008"}, "date")[0]["value"] == "2008-07-31"
    assert normalize_typed_filter({"op": "<=", "value": "Feb 2024"}, "date")[0]["value"] == "2024-02-29"
    dates, _ = normalize_typed_filter({"op": "equals", "value": "2008-07-15"}, "date")
    assert dates["op"] == "equals" and dates["value"] == "2008-07-15"


def test_normalizes_relative_date_period_to_a_closed_range():
    dates, error = normalize_typed_filter(
        {"op": "equals", "value": "last 30 days"},
        "date",
        today=date(2026, 8, 25),
    )
    assert error is None
    assert dates["op"] == "between"
    assert dates["value"] == ["2026-07-26", "2026-08-25"]
    assert dates["resolved"] is True


# ── Tier 1: metadata evidence ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typo_is_corrected_from_a_complete_captured_domain_without_touching_the_source():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk", "Madrid"])})
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco")]))

    resolved = result["resolved_filters"][0]
    assert resolved["value"] == "Moscow" and resolved["op"] == "equals"
    assert resolved["evidence"] == "metadata" and resolved["tier"] == "T1"
    assert result["filter_clarification_required"] is False
    assert runner.calls == []
    assert any("Moscow" in a for a in result["plan_assumptions"])


@pytest.mark.asyncio
async def test_case_and_punctuation_variants_are_exact_hits_even_on_partial_snapshots():
    runner = _Runner()
    store = _Store(domains={("product", "name"): _evidence(["Mountain-300"], complete=False)})
    result = await _grounder(runner, store)(_state([_filter("product", "name", "mountain 300")]))
    assert result["resolved_filters"][0]["value"] == "Mountain-300"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_partial_capture_confirms_a_strong_candidate_with_one_point_probe():
    runner = _Runner(exists=True)
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk"], complete=False)})
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco")]))

    resolved = result["resolved_filters"][0]
    assert resolved["value"] == "Moscow" and resolved["tier"] == "T2" and resolved["evidence"] == "source"
    assert len(runner.calls) == 1 and "SELECT 1 AS present" in runner.calls[0]
    assert "'Moscow'" in runner.calls[0]


@pytest.mark.asyncio
async def test_partial_capture_candidate_absent_from_source_asks_instead_of_rewriting():
    runner = _Runner(exists=False)
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow"], complete=False)})
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco")]))

    assert result["resolved_filters"] == []
    assert result["filter_clarification_required"] is True
    assert result["filter_clarification"]["kind"] == "value"
    assert "Moscow" in result["clarification"]


@pytest.mark.asyncio
async def test_ambiguous_values_ask_with_the_candidates():
    runner = _Runner()
    store = _Store(domains={("orders", "region"): _evidence(["East", "West"])})
    grounder = _grounder(runner, store, match_threshold=1)
    result = await grounder(_state([_filter("orders", "region", "region")]))

    assert result["filter_clarification_required"] is True
    assert result["filter_ambiguities"][0]["candidates"] == ["East", "West"]
    assert [o["value"] for o in result["filter_clarification"]["options"]] == ["East", "West"]


@pytest.mark.asyncio
async def test_fresh_complete_domain_asserts_absence_with_nearest_values():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Monaco", "Berlin"])})
    grounder = _grounder(runner, store)
    result = await grounder(_state([_filter("dim_customer", "city", "mosc")]))
    # "mosc" covers both Moscow and Monaco loosely → ambiguity, not a silent pick.
    assert result["filter_clarification_required"] is True
    assert runner.calls == []


@pytest.mark.asyncio
async def test_stale_complete_domain_cannot_assert_absence_and_falls_through_to_the_source():
    runner = _Runner(values=[])
    store = _Store(domains={("dim_customer", "city"): _evidence(["Berlin", "Paris"], fresh_absence=False)})
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco")]))
    # The snapshot is too old to tell the user "Moscow does not exist"; the
    # bounded source search ran instead and found nothing → ask, not assert.
    assert any("LIKE" in sql for sql in runner.calls)
    assert result["resolved_filters"] == []
    assert result["filter_clarification_required"] is True


@pytest.mark.asyncio
async def test_multi_value_literal_resolves_each_needle_into_an_in_list():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk", "Madrid"])})
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", ["mosco", "minsk"], op="in")]))
    resolved = result["resolved_filters"][0]
    assert resolved["value"] == ["Moscow", "Minsk"] and resolved["op"] == "in"


@pytest.mark.asyncio
async def test_hebrew_literal_matches_a_captured_hebrew_domain():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["מוסקבה", "תל אביב", "חיפה"])})
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "מוסקבה")], question="מכירות במוסקבה"))
    assert result["resolved_filters"][0]["value"] == "מוסקבה"
    # A one-letter typo is corrected too.
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "מוסקבא")]))
    assert result["resolved_filters"][0]["value"] == "מוסקבה"


@pytest.mark.asyncio
async def test_contains_operator_is_preserved():
    runner = _Runner()
    store = _Store(domains={("product", "name"): _evidence(["Mountain-300", "Road-250"])})
    result = await _grounder(runner, store)(_state([_filter("product", "name", "mountain 300", op="contains")]))
    resolved = result["resolved_filters"][0]
    assert resolved["op"] == "contains" and resolved["value"] == "Mountain-300"


@pytest.mark.asyncio
async def test_complete_domain_is_cached_per_column_and_reused_for_another_literal():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk"])})
    grounder = _grounder(runner, store)
    await grounder(_state([_filter("dim_customer", "city", "mosco")]))
    await grounder(_state([_filter("dim_customer", "city", "minsk")]))
    assert len(store.domain_calls) == 1


# ── Governance ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sensitive_column_is_never_probed_nor_offered():
    runner = _Runner(values=["Alice Cohen"])
    sensitive = ColumnProfile(table="people", column="full_name", sensitivity_tag="person_name",
                              data_type="text", status="success")
    store = _Store(
        profiles={("people", "full_name"): sensitive},
        domains={("dim_customer", "city"): _evidence(["Moscow"])},
    )
    # As the planner's own column: never grounded, never probed.
    result = await _grounder(runner, store)(_state([_filter("people", "full_name", "alice")]))
    assert result["unresolved_filters"][0]["reason"] == "governed column"
    assert runner.calls == []
    # As a reverse-lookup candidate: dropped from the options.
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("people", "full_name", "Mosco Alice")]
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco", candidates=hits)]))
    assert result["resolved_filters"][0]["table"] == "dim_customer"
    assert result["filter_clarification"] is None


@pytest.mark.asyncio
async def test_space_separated_governed_names_are_excluded():
    runner = _Runner(values=["123-45-6789"])
    state = _state([_filter("people", "Social Security Number", "123")])
    state["table_columns"]["people"].append("social security number")
    result = await _grounder(runner, _Store())(state)
    assert result["unresolved_filters"][0]["reason"] == "governed column"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_denylisted_column_is_never_probed():
    runner = _Runner(values=["Moscow"])
    state = _state([_filter("dim_customer", "city", "mosco")], filter_probe_denylist="dim_customer.*")
    result = await _grounder(runner, _Store())(state)
    assert result["unresolved_filters"][0]["reason"] == "governed column"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_user_scoped_visibility_never_shows_captured_values_and_confirms_hits():
    runner = _Runner(exists=True)
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk"])})
    state = _state([_filter("dim_customer", "city", "mosco")], filter_value_visibility="user_scoped")
    result = await _grounder(runner, store)(state)
    assert result["resolved_filters"][0]["value"] == "Moscow" and result["resolved_filters"][0]["tier"] == "T2"
    assert len(runner.calls) == 1


# ── Column decision ───────────────────────────────────────────────────────────


def _edges():
    return [Relationship("sales", "customer_id", "dim_customer", "id"),
            Relationship("sales", "dealer_id", "dim_dealer", "id")]


@pytest.mark.asyncio
async def test_single_eligible_candidate_elsewhere_retargets_and_discloses():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow")]
    result = await _grounder(runner, store)(_state([_filter("sales", "region", "mosco", candidates=hits)]))

    resolved = result["resolved_filters"][0]
    assert (resolved["table"], resolved["column"], resolved["value"]) == ("dim_customer", "city", "Moscow")
    assert result["filter_clarification_required"] is False
    assert any("instead" in a for a in result["plan_assumptions"])


@pytest.mark.asyncio
async def test_competing_business_roles_ask_a_structured_column_question():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow"]),
                            ("dim_dealer", "city"): _evidence(["Moscow"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("dim_dealer", "city", "Moscow")]
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco", candidates=hits)]))

    assert result["filter_clarification_required"] is True
    payload = result["filter_clarification"]
    assert payload["kind"] == "column" and payload["allow_any"] and payload["allow_other"]
    assert [o["id"] for o in payload["options"]] == ["dim_customer.city", "dim_dealer.city"]
    assert payload["options"][0]["label"] == "Customer city"
    assert payload["options"][0]["value"] == "Moscow"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_role_word_in_the_question_resolves_without_asking():
    runner = _Runner()
    store = _Store(domains={("dim_dealer", "city"): _evidence(["Moscow"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("dim_dealer", "city", "Moscow")]
    result = await _grounder(runner, store)(_state(
        [_filter("dim_dealer", "city", "mosco", candidates=hits)],
        question="show sales for dealers in mosco",
    ))
    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["table"] == "dim_dealer"


@pytest.mark.asyncio
async def test_unreachable_candidate_columns_are_not_offered():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("orders", "region", "Moscow")]  # orders not joined to sales
    result = await _grounder(runner, store)(_state([_filter("dim_customer", "city", "mosco", candidates=hits)]))
    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["table"] == "dim_customer"


@pytest.mark.asyncio
async def test_a_remembered_choice_is_honoured_before_asking():
    runner = _Runner()
    store = _Store(domains={("dim_dealer", "city"): _evidence(["Moscow"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("dim_dealer", "city", "Moscow")]
    state = _state([_filter("dim_customer", "city", "mosco", candidates=hits)],
                   filter_choices=[{"literal": "Mosco", "table": "dim_dealer", "column": "city"}])
    result = await _grounder(runner, store)(state)
    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["table"] == "dim_dealer"


@pytest.mark.asyncio
async def test_a_persisted_role_preference_settles_competing_roles_for_a_new_literal():
    runner = _Runner()
    store = _Store(domains={("dim_dealer", "city"): _evidence(["Paris", "Moscow"]),
                            ("dim_customer", "city"): _evidence(["Paris"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Paris"), _hit("dim_dealer", "city", "Paris")]
    # Earlier this user said "mosco" meant the dealer's city; the role-level
    # row ("city" → dim_dealer.city) is reused for "paris" without asking.
    state = _state([_filter("dim_customer", "city", "paris", candidates=hits)],
                   question="sales in paris",
                   filter_preferences=[
                       {"literal": "mosco", "role": "city", "table": "dim_dealer", "column": "city", "any": False, "value": None},
                       {"literal": "", "role": "city", "table": "dim_dealer", "column": "city", "any": False, "value": None},
                   ])
    result = await _grounder(runner, store)(state)
    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["table"] == "dim_dealer"
    # A role preference never introduces a column the evidence did not offer.
    state["filter_preferences"] = [{"literal": "", "role": "region", "table": "orders", "column": "region", "any": False, "value": None}]
    result = await _grounder(runner, store)(state)
    assert result["filter_clarification_required"] is True


@pytest.mark.asyncio
async def test_a_remembered_choice_cannot_introduce_a_column_the_evidence_did_not_offer():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow"]),
                            ("orders", "region"): _evidence(["Moscow"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow")]
    # orders.region is a real, eligible text column — but the reverse lookup did
    # not offer it for "mosco" and it is not joinable from sales. A forged or
    # stale choice pointing at it is ignored; the planner's column stands.
    state = _state([_filter("dim_customer", "city", "mosco", candidates=hits)],
                   filter_choices=[{"literal": "mosco", "table": "orders", "column": "region"}])
    result = await _grounder(runner, store)(state)
    assert result["resolved_filters"][0]["table"] == "dim_customer"
    assert runner.calls == []
    # The planner's own column may always be chosen explicitly.
    state["filter_choices"] = [{"literal": "mosco", "table": "dim_customer", "column": "city"}]
    result = await _grounder(runner, store)(state)
    assert result["resolved_filters"][0]["table"] == "dim_customer"


@pytest.mark.asyncio
async def test_a_role_preference_does_not_override_a_role_named_in_the_question():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Paris"]),
                            ("dim_dealer", "city"): _evidence(["Paris"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Paris"), _hit("dim_dealer", "city", "Paris")]
    state = _state([_filter("dim_customer", "city", "paris", candidates=hits)],
                   question="sales to customers in paris",
                   filter_preferences=[{"literal": "", "role": "city", "table": "dim_dealer", "column": "city", "any": False, "value": None}])
    result = await _grounder(runner, store)(state)
    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["table"] == "dim_customer"


@pytest.mark.asyncio
async def test_a_persisted_literal_preference_is_honoured_like_a_request_choice():
    runner = _Runner()
    store = _Store(domains={("dim_dealer", "city"): _evidence(["Moscow"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("dim_dealer", "city", "Moscow")]
    state = _state([_filter("dim_customer", "city", "mosco", candidates=hits)],
                   filter_preferences=[{"literal": "mosco", "role": "city", "table": "dim_dealer", "column": "city", "any": False, "value": None}])
    result = await _grounder(runner, store)(state)
    assert result["resolved_filters"][0]["table"] == "dim_dealer"


@pytest.mark.asyncio
async def test_any_of_these_fields_choice_grounds_the_value_in_every_column():
    runner = _Runner()
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow"]),
                            ("dim_dealer", "city"): _evidence(["Moscow", "Minsk"])}, edges=_edges())
    hits = [_hit("dim_customer", "city", "Moscow"), _hit("dim_dealer", "city", "Moscow")]
    state = _state([_filter("dim_customer", "city", "mosco", candidates=hits)],
                   filter_choices=[{"literal": "mosco", "any": True}])
    result = await _grounder(runner, store)(state)
    resolved = result["resolved_filters"][0]
    assert set(resolved["any_of_columns"]) == {"dim_customer.city", "dim_dealer.city"}
    assert resolved["value"] == "Moscow"
    # A column where the value cannot be grounded is left out of the OR.
    store.domains.pop(("dim_dealer", "city"))
    value_domain_cache.clear()
    result = await _grounder(runner, store)(state)
    resolved = result["resolved_filters"][0]
    assert (resolved["table"], resolved["column"]) == ("dim_customer", "city")
    assert "any_of_columns" not in resolved


def test_decide_column_rules_directly():
    descriptions = {("dim_customer", "city"): "Customer city", ("dim_dealer", "city"): "Dealer city"}
    common = dict(literal="mosco", question="sales in mosco", eligible=lambda t, c: True,
                  reachable=None, remembered=None, descriptions=descriptions)
    # No hits → the planner's column stands.
    assert decide_column(planner_table="sales", planner_column="region", hits=[], **common).action == "resolve"
    # Planner's column is the only hit → resolve.
    only = [_hit("dim_customer", "city", "Moscow")]
    assert decide_column(planner_table="dim_customer", planner_column="city", hits=only, **common).action == "resolve"
    # Not the planner's, but the only eligible one → retarget with disclosure.
    d = decide_column(planner_table="sales", planner_column="region", hits=only, **common)
    assert d.action == "disclose" and (d.table, d.column) == ("dim_customer", "city") and "instead" in d.assumption
    # Competing roles → ask.
    both = only + [_hit("dim_dealer", "city", "Moscow")]
    assert decide_column(planner_table="dim_customer", planner_column="city", hits=both, **common).action == "ask"
    # A remembered answer wins.
    d = decide_column(planner_table="dim_customer", planner_column="city", hits=both,
                      **{**common, "remembered": ("dim_dealer", "city")})
    assert d.action == "resolve" and d.table == "dim_dealer"


# ── Source tiers and connector gates ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_evidence_and_no_probe_capability_asks_by_default():
    runner = _Runner(values=["Moscow"])
    runner.supports_server_side_timeout = False
    result = await _grounder(runner, _Store())(_state([_filter("dim_customer", "city", "mosco")]))
    assert result["resolved_filters"] == []
    assert result["unresolved_filters"][0]["reason"] == "no evidence available"
    # An unverifiable equality literal is never executed silently: ask.
    assert result["filter_clarification_required"] is True
    assert result["filter_clarification"]["kind"] == "value" and result["filter_clarification"]["options"] == []
    assert "verify" in result["clarification"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_no_evidence_with_allow_policy_passes_through_disclosed():
    runner = _Runner(values=["Moscow"])
    runner.supports_server_side_timeout = False
    state = _state([_filter("dim_customer", "city", "mosco")], filter_unverified_execution="allow")
    result = await _grounder(runner, _Store())(state)
    assert result["filter_clarification_required"] is False
    assert result["unresolved_filters"][0]["reason"] == "no evidence available"


@pytest.mark.asyncio
async def test_transient_probe_failure_fails_open_without_asking():
    class _Broken(_Runner):
        async def run_sql(self, sql, **_kwargs):
            self.calls.append(sql)
            return {"error": "connection reset", "rows": []}

    runner = _Broken()
    result = await _grounder(runner, _Store())(_state([_filter("dim_customer", "city", "mosco")]))
    assert result["resolved_filters"] == []
    assert result["unresolved_filters"][0]["reason"] == "probe error"
    # Infrastructure trouble is not the user's to fix: the literal travels on, disclosed.
    assert result["filter_clarification_required"] is False


@pytest.mark.asyncio
async def test_a_remembered_choice_on_a_sensitive_column_is_ignored():
    runner = _Runner()
    sensitive = ColumnProfile(table="people", column="full_name", sensitivity_tag="person_name",
                              data_type="text", status="success")
    store = _Store(profiles={("people", "full_name"): sensitive},
                   domains={("dim_customer", "city"): _evidence(["Moscow"])})
    state = _state([_filter("dim_customer", "city", "mosco")],
                   filter_choices=[{"literal": "mosco", "table": "people", "column": "full_name"}])
    result = await _grounder(runner, store)(state)
    assert result["resolved_filters"][0]["table"] == "dim_customer"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_chosen_value_is_the_canonical_needle_even_without_evidence():
    runner = _Runner()
    runner.supports_server_side_timeout = False
    state = _state([_filter("dim_customer", "city", "mosco")],
                   filter_choices=[{"literal": "mosco", "table": "dim_customer", "column": "city", "value": "Moscow"}])
    result = await _grounder(runner, _Store())(state)
    resolved = result["resolved_filters"][0]
    assert resolved["value"] == "Moscow" and resolved["evidence"] == "user"
    assert result["filter_clarification_required"] is False


@pytest.mark.asyncio
async def test_small_exact_profile_enumerates_the_source_domain_once():
    runner = _Runner(values=["Mountain-300", "Road-250"])
    profile = ColumnProfile(table="product", column="name", distinct_count=30, distinct_is_approximate=False,
                            row_count=1000, data_type="varchar", status="success")
    store = _Store(profiles={("product", "name"): profile})
    result = await _grounder(runner, store)(_state([_filter("product", "name", "mountaiin 300")]))
    assert result["resolved_filters"][0]["value"] == "Mountain-300"
    assert result["resolved_filters"][0]["tier"] == "T3"
    assert "SELECT DISTINCT" in runner.sql and "LIKE" not in runner.sql


@pytest.mark.asyncio
async def test_estimated_or_unknown_profile_uses_the_bounded_search_and_only_asks():
    runner = _Runner(values=["Mountain-300"])
    profile = ColumnProfile(table="product", column="name", distinct_count=30, distinct_is_approximate=True,
                            data_type="varchar", status="success")
    store = _Store(profiles={("product", "name"): profile})
    result = await _grounder(runner, store)(_state([_filter("product", "name", "mountaiin 300")]))
    assert "LIKE" in runner.sql
    # A bounded search cannot prove uniqueness: the fuzzy hit becomes a question.
    assert result["resolved_filters"] == []
    assert result["filter_clarification_required"] is True
    assert "Mountain-300" in result["clarification"]


@pytest.mark.asyncio
async def test_bounded_search_exact_hit_is_trusted():
    runner = _Runner(values=["Mountain-300"])
    result = await _grounder(runner, _Store())(_state([_filter("product", "name", "mountain 300")]))
    assert result["resolved_filters"][0]["value"] == "Mountain-300"


@pytest.mark.asyncio
async def test_unverified_execution_allow_never_asks_about_a_missing_value():
    runner = _Runner(values=[])
    state = _state([_filter("product", "name", "speedster 900")], filter_unverified_execution="allow")
    result = await _grounder(runner, _Store())(state)
    assert result["filter_clarification_required"] is False
    assert result["unresolved_filters"][0]["reason"] == "not found in bounded search"


@pytest.mark.asyncio
async def test_source_probes_are_capped_per_request():
    runner = _Runner(values=[])
    filters = [_filter("product", "name", f"thing{i}") for i in range(4)]
    for f in filters:
        f["value"] = f["value"].replace("thing", "gadget")  # letters only
    await _grounder(runner, _Store())(_state(filters))
    assert len(runner.calls) <= filtering._MAX_SOURCE_PROBES


def test_point_probe_escapes_quotes_in_canonical_values():
    class _Rec:
        database_type = "postgres"
        supports_server_side_timeout = True
        sql = ""

        async def run_sql(self, sql, **_):
            self.sql = sql
            return {"rows": []}

    runner = _Rec()
    probe = SqlValueProbe(runner, "postgres")
    import asyncio
    asyncio.run(probe.exists("people", "name", "O'Brien; DROP TABLE x", timeout_ms=100))
    assert "'O''Brien; DROP TABLE x'" in runner.sql
    assert runner.sql.count("'") == 4


# ── Zero-row escalation ───────────────────────────────────────────────────────


def test_empty_result_diagnostic_is_bounded_and_covers_metadata_only_resolutions():
    state = {
        "query_result": {"rows": []},
        "unresolved_filters": [],
        "resolved_filters": [{"target": "dim_customer.city", "evidence": "metadata"}],
        "empty_filter_diagnostics": 0,
    }
    first = empty_filter_result_check(state)
    assert first["needs_filter_reground"] is True
    second = empty_filter_result_check({**state, **first})
    assert second["needs_filter_reground"] is False
    # A source-confirmed resolution and no unverified literal: nothing to re-check.
    assert empty_filter_result_check({**state, "resolved_filters": [{"evidence": "source"}]})["needs_filter_reground"] is False


@pytest.mark.asyncio
async def test_escalated_pass_confirms_metadata_values_against_the_source():
    runner = _Runner(exists=False)
    store = _Store(domains={("dim_customer", "city"): _evidence(["Moscow", "Minsk"])})
    state = _state([_filter("dim_customer", "city", "mosco")], needs_filter_reground=True)
    result = await _grounder(runner, store)(state)
    # The snapshot said Moscow existed; the source says it no longer does.
    assert any("SELECT 1 AS present" in sql for sql in runner.calls)
    assert result["resolved_filters"] == []
    assert result["filter_clarification_required"] is True
    assert result["needs_filter_reground"] is False


# ── Forecast / typed clarifications (unchanged behaviour) ────────────────────


def _period_filter_state(question: str, route: str, value):
    return _state(
        [_filter("dimdate", "fulldatealternatekey", value, op="between", data_type="date")],
        question=question, route=route,
    )


@pytest.mark.asyncio
async def test_grounder_explains_an_unreadable_date_instead_of_could_not_verify():
    result = await _grounder(_Runner(), _Store())(_period_filter_state(
        "total profit from H2 2008 to H1 2009", "needs_query", ["H2 2008", "H1 2009"],
    ))
    assert result["filter_clarification_required"] is True
    assert result["clarification"] == (
        'I couldn\'t read that date range unambiguously ("H2 2008 to H1 2009" for fulldatealternatekey). '
        'Please write dates as YYYY-MM-DD or as a month like "Jul 2008".'
    )
    assert "could not verify" not in result["clarification"]


@pytest.mark.asyncio
async def test_grounder_does_not_block_a_forecast_on_its_target_period():
    grounder = _grounder(_Runner(), _Store())
    result = await grounder(_period_filter_state(
        "forecast the profit for the next 6 month (H2 2008 to H1 2009)", "needs_analysis", ["H2 2008", "H1 2009"],
    ))
    assert result["filter_clarification_required"] is False and result["clarification"] is None
    assert result["resolved_filters"] == []
    assert result["unresolved_filters"] == [{
        "target": "dimdate.fulldatealternatekey", "value": ["H2 2008", "H1 2009"],
        "reason": "I couldn't read that date range unambiguously.",
    }]
    result = await grounder(_period_filter_state(
        "forecast the profit for the next 6 month (7/2008 to 1/2009)", "needs_analysis", ["7/2008", "1/2009"],
    ))
    assert result["filter_clarification_required"] is False
    assert result["resolved_filters"][0]["value"] == ["2008-07-01", "2009-01-31"]
    result = await grounder(_period_filter_state(
        "anything weird in profit between H2 2008 and H1 2009?", "needs_analysis", ["H2 2008", "H1 2009"],
    ))
    assert result["filter_clarification_required"] is True


# ── Literal extraction & planner ─────────────────────────────────────────────


def test_extract_candidate_literals_keeps_values_and_drops_schema_words_numbers_and_stopwords():
    table_columns = {"sales": ["region", "amount"], "dim_customer": ["city"]}
    assert extract_candidate_literals("show all cars in mosco", table_columns) == ["mosco"]
    assert extract_candidate_literals('sales for "New York" in 2024 by region', table_columns) == ["New York"]
    # Hebrew words are candidates too (the function word "לפי" is not).
    hebrew = extract_candidate_literals("מכירות במוסקבה לפי אזור", table_columns)
    assert "במוסקבה" in hebrew and "לפי" not in hebrew
    assert "region" not in extract_candidate_literals("sales by region", table_columns)


class _PromptLoader:
    async def arender(self, name, **kwargs):
        return json.dumps({k: str(v) for k, v in kwargs.items()})

    async def model_override_for(self, name):
        return None


@pytest.mark.asyncio
async def test_planner_runs_on_a_strong_reverse_hit_even_without_a_cue_word_and_prunes_to_candidates():
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value={
        "content": json.dumps({"filters": [{"table": "dim_customer", "column": "city", "op": "equals", "value": "Moscow"}]}),
        "usage": {},
    })
    store = _Store(hits=[ColumnHit("dim_customer", "city", "Moscow", 0.62, 12, "categorical")])
    planner = make_filter_planner(llm, _PromptLoader(), value_store_provider=lambda _s: store)
    state = _state([], question="Moscow sales")  # no English predicate cue
    result = await planner(state)

    llm.generate.assert_awaited_once()
    prompt = json.loads(llm.generate.call_args.kwargs["messages"][0]["content"])
    assert "dim_customer.city" in prompt["candidate_columns"] and "Moscow" in prompt["candidate_columns"]
    planned = result["filter_plan"]["filters"][0]
    assert planned["candidate_columns"][0]["column"] == "city"
    assert result["filter_candidates"][0]["table"] == "dim_customer"


@pytest.mark.asyncio
async def test_planner_sees_candidates_on_the_cue_word_path_and_hides_governed_columns():
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value={"content": json.dumps({"filters": []}), "usage": {}})
    sensitive = ColumnProfile(table="people", column="full_name", sensitivity_tag="person_name",
                              data_type="text", status="success")
    store = _Store(
        profiles={("people", "full_name"): sensitive},
        hits=[ColumnHit("dim_customer", "city", "Moscow", 0.62, 12, "categorical"),
              ColumnHit("people", "full_name", "Mosco Alice", 0.5, 1, "large_categorical")],
    )
    planner = make_filter_planner(llm, _PromptLoader(), value_store_provider=lambda _s: store)
    result = await planner(_state([], question="show sales in mosco"))  # "in" is a cue word

    prompt = json.loads(llm.generate.call_args.kwargs["messages"][0]["content"])
    # Hits were awaited before the LLM call, so they reach the prompt on this path too…
    assert "dim_customer.city" in prompt["candidate_columns"]
    # …fenced as data, and never a sensitive column.
    assert "<<<BEGIN_UNTRUSTED_DATA>>>" in prompt["candidate_columns"]
    assert "people" not in prompt["candidate_columns"] and "Alice" not in prompt["candidate_columns"]
    assert [h["table"] for h in result["filter_candidates"]] == ["dim_customer"]


@pytest.mark.asyncio
async def test_planner_hides_values_under_user_scoped_visibility_and_survives_an_llm_failure():
    llm = AsyncMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("model down"))
    store = _Store(hits=[ColumnHit("dim_customer", "city", "Moscow", 0.62, 12, "categorical")])
    planner = make_filter_planner(llm, _PromptLoader(), value_store_provider=lambda _s: store)
    result = await planner(_state([], question="show sales in mosco", filter_value_visibility="user_scoped"))
    prompt = json.loads(llm.generate.call_args.kwargs["messages"][0]["content"])
    assert "Moscow" not in prompt["candidate_columns"] and "matching value" in prompt["candidate_columns"]
    assert result["filter_plan"] == {"filters": [], "invalid_filters": []}


@pytest.mark.asyncio
async def test_planner_is_skipped_without_cue_word_or_hit():
    llm = AsyncMock()
    llm.generate = AsyncMock()
    planner = make_filter_planner(llm, _PromptLoader(), value_store_provider=lambda _s: _Store())
    result = await planner(_state([], question="Moscow sales"))
    llm.generate.assert_not_awaited()
    assert result["filter_plan"] == {"filters": [], "invalid_filters": []}
