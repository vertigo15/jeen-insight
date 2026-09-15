"""Planner: keyword cues, catalog parsing, deterministic plan validation and the
one mocked LLM call."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.analysis_planner import (
    build_params_from_plan,
    catalog_candidates,
    detect_analysis_intent,
    diff_params,
    plan_analysis,
    render_catalog,
)
from src.agent.langgraph_agent.prompt_loader import PromptLoader

_COLUMNS = (
    "- FactInternetSales.OrderDate - Type: timestamp\n"
    "- FactInternetSales.ShipDate - Type: timestamp\n"
    "- FactInternetSales.Profit - Type: decimal\n"
    "- FactInternetSales.SalesAmount - Type: money\n"
    "- FactInternetSales.SalesReason - Type: varchar\n"
    '- "dbo"."DimProduct"."ProductKey" - Type: integer, PK: true\n'
    "- FactProductInventory.MovementDate - Type: date\n"
    "- FactProductInventory.UnitsBalance - Type: integer\n"
)


@pytest.mark.parametrize("question,expected", [
    ("Forecast revenue for the next quarter", "forecast"),
    ("Will profit grow next month?", "forecast"),
    ("At this rate where do we land?", "forecast"),
    ("Is anything weird in profit over the last six months?", "anomaly_detection"),
    ("Show outliers in weekly sales", "anomaly_detection"),
    ("Any unusual spikes in orders?", "anomaly_detection"),
    ("What was profit last quarter?", None),
    ("How much did sales drop in March?", None),
    ("", None),
])
def test_detect_analysis_intent(question, expected):
    assert detect_analysis_intent(question) == expected


def test_catalog_candidates_keeps_case_and_splits_by_kind():
    cands = catalog_candidates(_COLUMNS)
    fis = cands["factinternetsales"]
    assert fis.name == "FactInternetSales"
    assert fis.date_columns == ["OrderDate", "ShipDate"]
    assert fis.numeric_columns == ["Profit", "SalesAmount"]
    assert cands["dimproduct"].date_columns == [] and cands["dimproduct"].numeric_columns == ["ProductKey"]
    rendered = render_catalog(cands)
    assert "FactInternetSales: dates [OrderDate, ShipDate]" in rendered
    assert "DimProduct" not in rendered  # no date column → cannot host a series


def _plan(**over):
    base = {"skill": "anomaly_detection", "table": "factinternetsales", "date_column": "orderdate",
            "measure_column": "profit", "agg": "sum", "grain": "week", "window_periods": 26, "sensitivity": 0.9}
    base.update(over)
    return base


def test_plan_is_validated_and_case_corrected():
    out = build_params_from_plan(_plan(), catalog_candidates(_COLUMNS), resolved_filters=[],
                                 connection_schema="dbo", connection_catalog=None)
    assert out.kind == "params" and out.skill == "anomaly_detection"
    s = out.params["series"]
    assert (s["table"], s["date_column"], s["measure_column"], s["schema_name"]) == ("FactInternetSales", "OrderDate", "Profit", "dbo")
    assert out.params["window"] == 26 and out.params["sensitivity"] == 0.9


def test_plan_forecast_carries_horizon_and_window_hint():
    out = build_params_from_plan(_plan(skill="forecast", horizon=13, interval=0.9, window_periods=104),
                                 catalog_candidates(_COLUMNS), resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert out.kind == "params" and out.params["horizon"] == 13 and out.params["interval"] == 0.9
    assert out.params["_window"] == 104


def test_plan_ambiguity_becomes_clarification_with_patches():
    out = build_params_from_plan(_plan(date_column=None, ambiguous={"date_column": ["ShipDate", "OrderDate"]}),
                                 catalog_candidates(_COLUMNS), resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert out.kind == "clarify"
    assert [o.label for o in out.options] == ["ShipDate", "OrderDate"]
    assert out.options[0].params_patch == {"series": {"date_column": "ShipDate"}} and out.options[0].recommended


def test_plan_missing_choices_use_the_only_candidate_or_clarify():
    cands = catalog_candidates(_COLUMNS)
    single = build_params_from_plan(_plan(table="FactProductInventory", date_column="x", measure_column="y"),
                                    cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert single.kind == "params"
    assert single.params["series"]["date_column"] == "MovementDate" and single.params["series"]["measure_column"] == "UnitsBalance"
    many = build_params_from_plan(_plan(measure_column="revenue"), cands, resolved_filters=[],
                                  connection_schema=None, connection_catalog=None)
    assert many.kind == "clarify" and "measure" in many.message


def test_plan_fallbacks():
    cands = catalog_candidates(_COLUMNS)
    assert build_params_from_plan({"skill": "none"}, cands, resolved_filters=[], connection_schema=None, connection_catalog=None).kind == "fallback"
    assert build_params_from_plan(_plan(skill="clustering"), cands, resolved_filters=[], connection_schema=None, connection_catalog=None).kind == "fallback"
    assert build_params_from_plan(_plan(table="DimProduct"), cands, resolved_filters=[], connection_schema=None, connection_catalog=None).kind == "fallback"
    assert build_params_from_plan(_plan(table="nope"), cands, resolved_filters=[], connection_schema=None, connection_catalog=None).kind == "fallback"


def test_grounded_filters_are_kept_and_date_ranges_become_the_window():
    filters = [
        {"table": "FactInternetSales", "column": "SalesReason", "op": "equals", "value": "Promotion"},
        {"table": "FactInternetSales", "column": "OrderDate", "op": "between", "value": ["2026-01-01", "2026-07-01"]},
        {"table": "DimProduct", "column": "Color", "op": "equals", "value": "Red"},
    ]
    out = build_params_from_plan(_plan(), catalog_candidates(_COLUMNS), resolved_filters=filters,
                                 connection_schema=None, connection_catalog=None)
    s = out.params["series"]
    assert s["start"] == "2026-01-01" and s["end"] == "2026-07-01"
    assert [f["column"] for f in s["filters"]] == ["SalesReason"]
    assert out.dropped_filters == ["DimProduct.Color"]


def test_plan_analysis_calls_the_llm_once_and_validates():
    llm = MagicMock()
    llm.generate = AsyncMock(return_value={"content": "```json\n" + json.dumps(_plan()) + "\n```", "usage": {"total_tokens": 9}})
    out = asyncio.run(plan_analysis(
        question="Is anything weird in profit?", columns_text=_COLUMNS, resolved_filters=[],
        llm=llm, prompt_loader=PromptLoader(), skill_hint="anomaly_detection",
    ))
    assert out.kind == "params" and out.usage == {"total_tokens": 9}
    assert llm.generate.await_count == 1
    system = llm.generate.await_args.kwargs["messages"][0]["content"]
    assert "FactInternetSales: dates [OrderDate, ShipDate]" in system and "anomaly_detection" in system
    assert llm.generate.await_args.kwargs["temperature"] == 0.0


def test_plan_analysis_llm_failure_and_garbage_fall_back():
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("boom"))
    out = asyncio.run(plan_analysis(question="q", columns_text=_COLUMNS, resolved_filters=[], llm=llm, prompt_loader=PromptLoader()))
    assert out.kind == "fallback" and "planner unavailable" in out.reason
    llm.generate = AsyncMock(return_value={"content": "not json"})
    out = asyncio.run(plan_analysis(question="q", columns_text=_COLUMNS, resolved_filters=[], llm=llm, prompt_loader=PromptLoader()))
    assert out.kind == "fallback"
    out = asyncio.run(plan_analysis(question="q", columns_text="", resolved_filters=[], llm=llm, prompt_loader=PromptLoader()))
    assert out.kind == "fallback" and llm.generate.await_count == 1  # no catalog → no call


_COLUMNS_P6 = _COLUMNS + (
    "- FactInternetSales.SalesTerritoryKey - Type: integer\n"
    "- FactInternetSales.ProductLine - Type: varchar\n"
    "- DimCustomer.CustomerKey - Type: integer\n"
    "- DimCustomer.YearlyIncome - Type: money\n"
    "- DimCustomer.TotalChildren - Type: integer\n"
    "- DimCustomer.NumberCarsOwned - Type: integer\n"
    "- DimCustomer.Gender - Type: char\n"
)


@pytest.mark.parametrize("question,expected", [
    ("Why did profit drop in Q2?", "contribution"),
    ("What explains the fall in revenue?", "contribution"),
    ("When did sales start growing?", "changepoint"),
    ("Is revenue seasonal?", "seasonality"),
    ("Does profit correlate with order quantity?", "correlation"),
    ("Segment our customers by income and children", "clustering"),
    ("What drives profit per customer?", "driver_analysis"),
    ("Which factors predict churn?", "driver_analysis"),
    ("Run a linear regression of profit on income", "regression"),
    ("What is the effect of income on profit?", "regression"),
    ("Show me the coefficients for revenue", "regression"),
    ("How much does discount affect margin?", "regression"),
    ("Which customers are likely to churn?", "classification"),
    ("What is the probability of churn per customer?", "classification"),
    ("Predict who will cancel their subscription", "classification"),
    ("Estimate the propensity to buy", "classification"),
])
def test_detect_analysis_intent_p6(question, expected):
    assert detect_analysis_intent(question) == expected


def test_catalog_candidates_keep_text_columns_and_render_them():
    cands = catalog_candidates(_COLUMNS_P6)
    assert cands["factinternetsales"].text_columns == ["SalesReason", "ProductLine"]
    assert cands["dimcustomer"].text_columns == ["Gender"] and cands["dimcustomer"].date_columns == []
    rendered = render_catalog(cands)
    assert "DimCustomer" in rendered  # entity table without dates is now listed
    assert "dimensions/keys [SalesReason, ProductLine]" in rendered


def test_plan_correlation_contribution_and_multi_series():
    cands = catalog_candidates(_COLUMNS_P6)
    corr = build_params_from_plan(_plan(skill="correlation", other_measure_column="salesamount", max_lag=4), cands,
                                  resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert corr.kind == "params" and corr.params["other_measure_column"] == "SalesAmount" and corr.params["max_lag"] == 4
    ambiguous = build_params_from_plan(_plan(skill="correlation"), cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert ambiguous.kind == "clarify"
    assert [o.label for o in ambiguous.options] == ["SalesAmount", "SalesTerritoryKey"]
    assert ambiguous.options[0].params_patch == {"other_measure_column": "SalesAmount"}

    contrib = build_params_from_plan(_plan(skill="contribution", dimensions=["productline", "OrderDate", "nope"],
                                           before_start="2026-01-01", before_end="2026-04-01"),
                                     cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert contrib.kind == "params" and contrib.params["dimensions"] == ["ProductLine"]
    assert contrib.params["before_start"] == "2026-01-01" and contrib.params["after_start"] is None
    assert "group_by" not in contrib.params["series"] or contrib.params["series"]["group_by"] is None

    multi = build_params_from_plan(_plan(group_by="salesterritorykey"), cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert multi.kind == "params" and multi.params["series"]["group_by"] == "SalesTerritoryKey"


def test_plan_entity_skills():
    cands = catalog_candidates(_COLUMNS_P6)
    cl = build_params_from_plan({"skill": "clustering", "table": "DimCustomer", "entity_key": "customerkey",
                                 "features": ["yearlyincome", "totalchildren", "gender"], "k": 4},
                                cands, resolved_filters=[], connection_schema="dbo", connection_catalog=None)
    assert cl.kind == "params"
    assert cl.params["entity"]["entity_key"] == "CustomerKey" and cl.params["entity"]["features"] == ["YearlyIncome", "TotalChildren"]
    assert cl.params["k"] == 4 and cl.params["entity"]["schema_name"] == "dbo"
    # Driver analysis without a target clarifies; with one it excludes it from the features.
    d0 = build_params_from_plan({"skill": "driver_analysis", "table": "DimCustomer", "entity_key": "CustomerKey",
                                 "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"]},
                                cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert d0.kind == "clarify" and "drivers explain" in d0.message
    d1 = build_params_from_plan({"skill": "driver_analysis", "table": "DimCustomer", "entity_key": "CustomerKey",
                                 "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"], "target": "NumberCarsOwned"},
                                cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert d1.kind == "params" and d1.params["entity"]["target"] == "NumberCarsOwned"
    assert d1.params["entity"]["features"] == ["YearlyIncome", "TotalChildren"]
    # Regression behaves the same: no target clarifies (with its own wording); a target excludes itself.
    r0 = build_params_from_plan({"skill": "regression", "table": "DimCustomer", "entity_key": "CustomerKey",
                                 "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"]},
                                cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert r0.kind == "clarify" and "regression model" in r0.message
    r1 = build_params_from_plan({"skill": "regression", "table": "DimCustomer", "entity_key": "CustomerKey",
                                 "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"], "target": "NumberCarsOwned"},
                                cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert r1.kind == "params" and r1.params["entity"]["target"] == "NumberCarsOwned"
    assert r1.params["entity"]["features"] == ["YearlyIncome", "TotalChildren"]
    # Classification also needs a target and clarifies with its own wording.
    c0 = build_params_from_plan({"skill": "classification", "table": "DimCustomer", "entity_key": "CustomerKey",
                                 "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"]},
                                cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert c0.kind == "clarify" and "model predict" in c0.message
    c1 = build_params_from_plan({"skill": "classification", "table": "DimCustomer", "entity_key": "CustomerKey",
                                 "features": ["YearlyIncome", "TotalChildren", "NumberCarsOwned"], "target": "NumberCarsOwned"},
                                cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert c1.kind == "params" and c1.params["entity"]["target"] == "NumberCarsOwned"
    # Features default to the numeric columns when the plan names too few.
    auto = build_params_from_plan({"skill": "clustering", "table": "DimCustomer", "entity_key": "CustomerKey", "features": []},
                                  cands, resolved_filters=[], connection_schema=None, connection_catalog=None)
    assert auto.kind == "params" and len(auto.params["entity"]["features"]) >= 2


def test_diff_params_flattens_series_changes():
    base = {"series": {"grain": "week", "measure_column": "Profit"}, "sensitivity": 0.95}
    new = {"series": {"grain": "month", "measure_column": "Profit"}, "sensitivity": 0.9}
    assert diff_params(base, new) == {"grain": {"from": "week", "to": "month"}, "sensitivity": {"from": 0.95, "to": 0.9}}
