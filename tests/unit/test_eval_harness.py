"""Unit tests for the NL2SQL golden-set eval harness."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evals.harness import (
    evaluate,
    load_golden_set,
    make_fixture_result_executor,
    score_groundedness,
    score_safety,
)
from evals.run_eval import _OFFLINE_SETTINGS, _prepare_offline_environment

_DATASET = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "golden_set.yaml"

_SALES = {
    "database_type": "postgres",
    "tables": {
        "salesorders": ["orderid", "revenue", "customerid"],
        "customers": ["customerid", "name", "region"],
    },
}


class TestSafetyScorer:
    def test_allows_clean_select(self):
        case = {"id": "ok", "sql": "SELECT revenue FROM salesorders", "expect": "allow"}
        assert score_safety(case, _SALES).passed

    @pytest.mark.parametrize("sql", [
        "DELETE FROM salesorders",
        "UPDATE customers SET region='x'",
        "DROP TABLE salesorders",
        "SELECT 1; DELETE FROM salesorders",
        "WITH x AS (DELETE FROM salesorders RETURNING revenue) SELECT * FROM x",
        "TRUNCATE TABLE salesorders",
    ])
    def test_blocks_mutations(self, sql):
        case = {"id": "bad", "sql": sql, "expect": "block"}
        assert score_safety(case, _SALES).passed

    def test_blocks_governed_column(self):
        case = {"id": "pw", "sql": "SELECT password FROM customers", "expect": "block"}
        assert score_safety(case, _SALES).passed

    def test_detects_wrong_expectation(self):
        # A clean select the case *wrongly* marks as block → scorer reports fail.
        case = {"id": "x", "sql": "SELECT revenue FROM salesorders", "expect": "block"}
        assert not score_safety(case, _SALES).passed


class TestGroundednessScorer:
    def test_grounded_query_passes(self):
        case = {
            "id": "g",
            "sql": "SELECT region, SUM(revenue) FROM salesorders s "
                   "JOIN customers c ON s.customerid=c.customerid GROUP BY region",
            "grounded": True,
        }
        assert score_groundedness(case, _SALES).passed

    def test_unknown_table_flagged(self):
        case = {"id": "u", "sql": "SELECT * FROM private_users", "grounded": False}
        assert score_groundedness(case, _SALES).passed


class TestEvaluateBundledDataset:
    @pytest.mark.asyncio
    async def test_offline_safety_and_groundedness_perfect(self):
        dataset = load_golden_set(_DATASET)
        report = await evaluate(dataset)  # no classifier → offline

        safety = report.dimension_score("safety")
        grounded = report.dimension_score("groundedness")
        assert safety.scored > 0 and safety.accuracy == 1.0
        assert grounded.scored > 0 and grounded.accuracy == 1.0

    @pytest.mark.asyncio
    async def test_route_cases_skipped_without_classifier(self):
        dataset = load_golden_set(_DATASET)
        report = await evaluate(dataset)
        route = report.dimension_score("route")
        # Greetings score locally; the rest are skipped offline.
        assert route.skipped >= 1
        assert route.accuracy == 1.0  # greetings all correct

    @pytest.mark.asyncio
    async def test_live_classifier_stub_scores_routes(self):
        dataset = load_golden_set(_DATASET)

        async def fake_classifier(question, *, catalog_name=None, history=None):
            q = question.lower()
            if "weather" in q:
                return "out_of_scope"
            if "delete" in q:
                return "unsafe"
            if "sort those" in q:
                return "from_memory"
            if q.startswith("hi ") or "help?" in q:
                return "greeting"
            return "needs_query"

        report = await evaluate(dataset, route_classifier=fake_classifier)
        route = report.dimension_score("route")
        assert route.skipped == 0
        assert route.accuracy == 1.0


class TestReport:
    @pytest.mark.asyncio
    async def test_summary_and_failures(self):
        dataset = {
            "catalogs": {"s": _SALES},
            "cases": [
                {"id": "bad", "type": "safety", "catalog": "s",
                 "sql": "SELECT revenue FROM salesorders", "expect": "block"},
            ],
        }
        report = await evaluate(dataset)
        assert len(report.failures()) == 1
        assert "1 failing" in report.summary()


class TestContinuityAndEquivalenceFixtures:
    @pytest.mark.asyncio
    async def test_continuity_case_matches_expected_context(self):
        dataset = {
            "cases": [{
                "id": "follow-up",
                "type": "continuity",
                "question": "sort those descending",
                "history": [{"q": "revenue by region", "sql": "SELECT ..."}],
                "expect_context": {
                    "referenced_result": True,
                    "metric": "total_revenue",
                },
            }],
        }

        async def resolver(question, *, history=None):
            assert question == "sort those descending"
            assert history == dataset["cases"][0]["history"]
            return {
                "referenced_result": True,
                "metric": "total_revenue",
                "comparison": "descending",
            }

        report = await evaluate(dataset, continuity_resolver=resolver)
        score = report.dimension_score("continuity")
        assert score.scored == 1 and score.accuracy == 1.0

    @pytest.mark.asyncio
    async def test_equivalence_ignores_row_order_within_tolerance(self):
        dataset = {
            "cases": [{
                "id": "equivalent",
                "type": "equivalence",
                "catalog": "sales",
                "sql": "SELECT region, total_revenue FROM report",
                "expect_rows": [
                    {"region": "North", "total_revenue": 120.0},
                    {"region": "South", "total_revenue": 80.0},
                ],
                "numeric_tolerance": 0.01,
            }],
        }

        async def executor(sql, *, catalog_name=None):
            assert sql == dataset["cases"][0]["sql"]
            assert catalog_name == "sales"
            return [
                {"region": "South", "total_revenue": 80.0},
                {"region": "North", "total_revenue": 120.005},
            ]

        report = await evaluate(dataset, result_executor=executor)
        score = report.dimension_score("equivalence")
        assert score.scored == 1 and score.accuracy == 1.0

    @pytest.mark.asyncio
    async def test_bundled_equivalence_fixture_is_executed(self):
        dataset = load_golden_set(_DATASET)
        report = await evaluate(
            dataset,
            result_executor=make_fixture_result_executor(dataset["catalogs"]),
        )
        score = report.dimension_score("equivalence")
        assert score.scored == 1 and score.accuracy == 1.0

    @pytest.mark.asyncio
    async def test_unwired_future_dimensions_are_skipped(self):
        dataset = {
            "cases": [
                {
                    "id": "continuity",
                    "type": "continuity",
                    "question": "show those again",
                    "expect_context": {"referenced_result": True},
                },
                {
                    "id": "equivalence",
                    "type": "equivalence",
                    "sql": "SELECT 1",
                    "expect_rows": [{"value": 1}],
                },
            ],
        }
        report = await evaluate(dataset)
        assert report.dimension_score("continuity").skipped == 1
        assert report.dimension_score("equivalence").skipped == 1

    @pytest.mark.asyncio
    async def test_empty_future_expectations_fail_configuration(self):
        dataset = {
            "cases": [
                {"id": "empty-context", "type": "continuity", "expect_context": {}},
                {"id": "empty-rows", "type": "equivalence", "expect_rows": []},
            ],
        }
        report = await evaluate(dataset)
        assert {result.case_id for result in report.failures()} == {
            "empty-context",
            "empty-rows",
        }


class TestOfflineEvalCommand:
    def test_offline_command_seeds_only_missing_settings(self, monkeypatch):
        for key in _OFFLINE_SETTINGS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("METADATA_DB_HOST", "provided-host")

        _prepare_offline_environment()

        assert os.environ["METADATA_DB_HOST"] == "provided-host"
        for key, value in _OFFLINE_SETTINGS.items():
            if key != "METADATA_DB_HOST":
                assert os.environ[key] == value
