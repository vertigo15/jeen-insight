"""Unit tests for the metadata-backed value store (tier-1 evidence)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.metadata.value_store import (
    CaptureContract,
    ColumnHit,
    ColumnProfile,
    McpValueStore,
    MetadataDbValueStore,
    NullValueStore,
    Relationship,
    profile_evidence,
    profile_from_mapping,
    rank_hits,
    reachable_tables,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _profile(**overrides) -> ColumnProfile:
    base = dict(
        table="dim_site", column="city", data_type="character varying", semantic_type="categorical",
        column_role="category", value_shape="discrete", distinct_count=3, distinct_is_approximate=False,
        domain_is_complete=True, status="success", profiled_at=NOW - timedelta(hours=2),
        top_values=(("Charleston", 2), ("Norfolk", 1), ("Anchorage", 1)),
    )
    base.update(overrides)
    return ColumnProfile(**base)


# ── CaptureContract ───────────────────────────────────────────────────────────


class TestCaptureContract:
    def test_profile_domain_is_complete_only_when_the_profiler_says_so_and_counts_agree(self):
        contract = CaptureContract()
        assert contract.profile_domain_complete(_profile()) is True
        assert contract.profile_domain_complete(_profile(domain_is_complete=None)) is False
        assert contract.profile_domain_complete(_profile(domain_is_complete=False)) is False
        # An enumerated domain shorter than the distinct count is not complete.
        assert contract.profile_domain_complete(_profile(distinct_count=10)) is False
        assert contract.profile_domain_complete(_profile(status="failed")) is False

    def test_an_estimated_distinct_count_never_certifies_completeness_by_itself(self):
        contract = CaptureContract()
        approx = _profile(distinct_is_approximate=True, distinct_count=10)
        assert contract.profile_domain_complete(approx) is False
        # Captured values must exceed the estimate by the margin before a
        # capture is believed complete.
        assert contract.capture_complete(approx, captured_count=10, captured_class="categorical") is False
        assert contract.capture_complete(approx, captured_count=20, captured_class="categorical") is True
        exact = _profile(distinct_count=10)
        assert contract.capture_complete(exact, captured_count=10, captured_class="categorical") is True
        assert contract.capture_complete(exact, captured_count=9, captured_class="categorical") is False
        # Large / id classes are captured top-N or trigram-only: never complete.
        assert contract.capture_complete(exact, captured_count=10, captured_class="large_categorical") is False
        assert contract.capture_complete(_profile(distinct_count=None), 10, "categorical") is False

    def test_freshness_is_asymmetric(self):
        contract = CaptureContract(existence_max_age_seconds=7 * 86400, absence_max_age_seconds=86400)
        two_days = 2 * 86400.0
        assert contract.fresh_for_existence(two_days) is True
        assert contract.fresh_for_absence(two_days) is False
        assert contract.fresh_for_absence(None) is False


class TestProfileEvidence:
    def test_complete_fresh_profile_yields_a_trusted_domain(self):
        evidence = profile_evidence(_profile(), CaptureContract(), now=NOW)
        assert evidence.values == ("Charleston", "Norfolk", "Anchorage")
        assert evidence.complete and evidence.fresh_for_existence and evidence.fresh_for_absence
        assert evidence.counts["Charleston"] == 2
        assert evidence.as_domain().complete is True

    def test_stale_profile_proves_existence_but_not_absence(self):
        stale = _profile(profiled_at=NOW - timedelta(days=3))
        evidence = profile_evidence(stale, CaptureContract(), now=NOW)
        assert evidence.complete is True
        assert evidence.fresh_for_existence is True
        assert evidence.fresh_for_absence is False
        assert evidence.conclusive_for_absence is False

    def test_profile_without_values_gives_no_evidence(self):
        assert profile_evidence(_profile(top_values=(), example_values=()), CaptureContract()) is None


class TestProfileFromMapping:
    def test_reads_provider_payloads_with_tolerant_keys(self):
        raw = {
            "profile": {
                "semanticType": "categorical", "distinctCount": "39", "distinctIsApproximate": False,
                "domainIsComplete": True, "sensitivityTag": None,
                "topValues": [{"value": "Moscow", "count": 3}], "profiledAt": "2026-09-01T00:00:00Z",
            }
        }
        profile = profile_from_mapping(raw, table="dim_customer", column="city")
        assert profile.semantic_type == "categorical" and profile.distinct_count == 39
        assert profile.domain_is_complete is True and profile.top_values == (("Moscow", 3),)
        assert profile.profiled_at.year == 2026
        assert profile_from_mapping("not json", table="t", column="c") is None


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestRanking:
    def test_rank_hits_reranks_with_the_local_matcher_and_drops_weak_ones(self):
        hits = [
            ColumnHit("dim_customer", "city", "Moscow", 0.5, 12, "categorical"),
            ColumnHit("dim_dealer", "city", "Mosbach", 0.45, 2, "categorical"),
            ColumnHit("orders", "status", "Berlin", 0.4, 1, "categorical"),
        ]
        ranked = rank_hits(["mosco"], hits, threshold=78.0)
        assert [h.value for h in ranked][0] == "Moscow"
        assert all(h.value != "Berlin" for h in ranked)

    def test_reachable_tables_walks_join_edges_up_to_two_hops(self):
        edges = [
            Relationship("sales", "customer_id", "dim_customer", "id"),
            Relationship("dim_customer", "region_id", "dim_region", "id"),
            Relationship("dim_region", "country_id", "dim_country", "id"),
            Relationship("orders", "status_id", "dim_status", "id"),
        ]
        reachable = reachable_tables({"sales"}, edges)
        assert {"sales", "dim_customer", "dim_region"} <= reachable
        assert "dim_country" not in reachable and "orders" not in reachable


# ── Metadata DB store with a fake pool ────────────────────────────────────────


class _Conn:
    def __init__(self, store: "_Pool"):
        self.store = store

    async def fetch(self, sql: str, *args):
        self.store.queries.append((sql, args))
        for needle, rows in self.store.fetch_rows:
            if needle in sql:
                return rows
        return []

    async def fetchrow(self, sql: str, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args):
        self.store.queries.append((sql, args))
        return 1 if "pg_extension" in sql else None


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, fetch_rows: List[Any]):
        self.fetch_rows = fetch_rows
        self.queries: List[Any] = []

    def acquire(self):
        return _Acquire(self)


def _feature_rows():
    return [{"table_name": "metadata_column_profiles"}, {"table_name": "metadata_table_profiles"},
            {"table_name": "metadata_column_value_embeddings"}]


def _profile_row(**overrides) -> Dict[str, Any]:
    row = {
        "table_name": "dim_site", "column_name": "city", "data_type": "character varying",
        "semantic_type": "categorical", "column_role": "category", "value_shape": "discrete",
        "distinct_count": 2, "distinct_is_approximate": False, "domain_is_complete": True,
        "sensitivity_tag": None,
        "top_values": json.dumps([{"value": "Charleston", "count": 2}, {"value": "Norfolk", "count": 1}]),
        "example_values": "[]", "example_strategy": "enumerated", "sample_method": "full_scan",
        "status": "success", "profiled_at": NOW - timedelta(hours=1), "profile_run_id": 7,
        "row_count": 39, "row_count_is_estimate": False, "is_hidden": False, "description": "Site city",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_db_store_reads_the_latest_profile_and_builds_a_complete_domain():
    pool = _Pool([("information_schema.tables", _feature_rows()),
                  ("FROM public.metadata_column_profiles p", [_profile_row()])])
    store = MetadataDbValueStore(pool)
    assert (await store.features()) == {"profiles": True, "table_profiles": True, "captured": True, "trgm": True}

    profile = await store.column_profile("defense", "dim_site", "city")
    assert profile.distinct_count == 2 and profile.domain_is_complete is True
    assert profile.top_values == (("Charleston", 2), ("Norfolk", 1))
    # Identity is by ``source`` + lower-cased table/column (parameterised, never interpolated).
    sql, args = next(q for q in pool.queries if "metadata_column_profiles p" in q[0])
    assert args == ("defense", "dim_site", "city") and "lower(p.table_name) = lower($2)" in sql

    evidence = await store.domain("defense", "dim_site", "city", needle="charlston")
    assert evidence.source == "profile" and evidence.complete is True
    assert evidence.values == ("Charleston", "Norfolk")
    # A complete profile domain answers without reading the captured table.
    assert not any("metadata_column_value_embeddings" in q[0] and "value_text" in q[0] for q in pool.queries)


@pytest.mark.asyncio
async def test_db_store_falls_back_to_captured_values_when_the_profile_is_not_complete():
    captured = [
        {"value_text": "Moscow", "value_count": 12, "semantic_type": "categorical",
         "last_seen_at": NOW - timedelta(hours=3), "last_seen_snapshot": 5, "total": 2},
        {"value_text": "Minsk", "value_count": 4, "semantic_type": "categorical",
         "last_seen_at": NOW - timedelta(hours=3), "last_seen_snapshot": 5, "total": 2},
    ]
    pool = _Pool([("information_schema.tables", _feature_rows()),
                  ("FROM public.metadata_column_profiles p", [_profile_row(domain_is_complete=None, top_values="[]")]),
                  ("FROM public.metadata_column_value_embeddings", captured)])
    store = MetadataDbValueStore(pool)
    evidence = await store.domain("src", "dim_site", "city", needle="mosco")
    assert evidence.source == "captured" and evidence.values == ("Moscow", "Minsk")
    # 2 captured == exact distinct_count 2 and the class is categorical → complete.
    assert evidence.complete is True and evidence.counts["Moscow"] == 12
    sql, args = next(q for q in pool.queries if "metadata_column_value_embeddings" in q[0])
    assert "similarity(value_text, $4)" in sql and args[-1] == "mosco"


@pytest.mark.asyncio
async def test_db_store_reverse_lookup_unions_captured_values_and_profile_top_values():
    captured = [{"table_name": "dim_customer", "column_name": "city", "value_text": "Moscow",
                 "value_count": 12, "semantic_type": "categorical", "sim": 0.62}]
    from_profiles = [{"table_name": "dim_dealer", "column_name": "city", "value_text": "Moscow",
                      "value_count": 3, "semantic_type": "categorical", "sim": 0.62},
                     {"table_name": "dim_customer", "column_name": "city", "value_text": "Moscow",
                      "value_count": 12, "semantic_type": "categorical", "sim": 0.62}]
    pool = _Pool([("information_schema.tables", _feature_rows()),
                  ("JOIN unnest($2::text[]) AS n(needle) ON e.value_text % n.needle", captured),
                  ("jsonb_array_elements", from_profiles)])
    store = MetadataDbValueStore(pool)
    hits = await store.find_columns_for_value("src", ["mosco"])
    assert [(h.table, h.column, h.source) for h in hits] == [
        ("dim_customer", "city", "captured"), ("dim_dealer", "city", "profile"),
    ]
    # Sensitive, hidden, deleted, key-like and measure columns are excluded at
    # the SQL level — on both the captured-value and the profile path — so no
    # governed value ever leaves the store.
    profile_sql = next(q[0] for q in pool.queries if "jsonb_array_elements" in q[0])
    captured_sql = next(q[0] for q in pool.queries if "value_text % n.needle" in q[0])
    for sql in (profile_sql, captured_sql):
        assert "is_hidden" in sql and "is_deleted" in sql and "sensitivity_tag IS NOT NULL" in sql
    assert "'primary_key', 'foreign_key', 'identifier'" in profile_sql
    assert "'large_unit_id'" in captured_sql


@pytest.mark.asyncio
async def test_db_store_without_profile_tables_returns_no_evidence():
    pool = _Pool([("information_schema.tables", [])])
    store = MetadataDbValueStore(pool)
    assert await store.column_profile("src", "t", "c") is None
    assert await store.domain("src", "t", "c") is None
    assert await store.find_columns_for_value("src", ["x"]) == []


@pytest.mark.asyncio
async def test_null_store_has_no_evidence():
    store = NullValueStore()
    assert store.available is False
    assert await store.domain("s", "t", "c") is None
    assert await store.find_columns_for_value("s", ["x"]) == []


# ── MCP store ─────────────────────────────────────────────────────────────────


class _Client:
    def __init__(self, profile=None, values=None, matches=None):
        self._profile = profile
        self._values = values or []
        self._matches = matches or []
        self.calls: List[Any] = []

    async def get_column_profile(self, source, *, table, column):
        self.calls.append(("profile", table, column))
        return self._profile

    async def search_column_values(self, source, *, table, column, query, limit):
        self.calls.append(("search", table, column, query))
        return {"values": self._values, "matches": self._matches, "complete": bool(self._values), "snapshot": "s1"}


@pytest.mark.asyncio
async def test_mcp_store_uses_provider_profile_then_value_search():
    client = _Client(profile={"semantic_type": "categorical", "distinct_count": 2, "domain_is_complete": False},
                     values=["Moscow", "Minsk"])
    store = McpValueStore(client)
    evidence = await store.domain("src", "dim_customer", "city", needle="mosco")
    assert evidence.source == "mcp" and evidence.values == ("Moscow", "Minsk") and evidence.complete is True
    assert ("search", "dim_customer", "city", "mosco") in client.calls


@pytest.mark.asyncio
async def test_mcp_store_reverse_lookup_needs_per_match_columns_and_falls_back_to_the_db_store():
    client = _Client(matches=[{"value": "Moscow", "table": "dim_customer", "column": "city", "score": 0.6}])
    hits = await McpValueStore(client).find_columns_for_value("src", ["mosco"])
    assert [(h.table, h.column) for h in hits] == [("dim_customer", "city")]
    assert client.calls[-1] == ("search", None, None, "mosco")

    class _Fallback(NullValueStore):
        available = True

        async def find_columns_for_value(self, source, needles, *, tables=None, limit=12):
            return [ColumnHit("dim_dealer", "city", "Moscow", 0.7)]

        async def relationships(self, source):
            return [Relationship("sales", "dealer_id", "dim_dealer", "id")]

    store = McpValueStore(_Client(), fallback=_Fallback())
    assert [(h.table, h.column) for h in await store.find_columns_for_value("src", ["mosco"])] == [("dim_dealer", "city")]
    assert len(await store.relationships("src")) == 1
