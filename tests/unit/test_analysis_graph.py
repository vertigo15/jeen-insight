"""End-to-end LangGraph flows for the ML-skills branch with mocked services.

Scenarios:
  - first run → planner → guard → confirm stop (status=confirm, proposal persisted)
  - confirmed re-entry → guard → sql → validate → dlp → execute → run → narration
  - remembered consent → straight through
  - pre-SQL guard refusal (short span) → status=blocked with executable exits
  - planner clarification → status=clarify with options
  - planner fallback → ordinary SQL path
  - ML skills disabled → needs_analysis collapses to needs_query
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import numpy as np
import pytest

from src.agent.analysis_store import InMemoryAnalysisStore
from src.agent.langgraph_agent.graph import build_graph
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.analysis.runner import InProcessRunner

pytestmark = pytest.mark.filterwarnings("ignore")

_METADATA = {
    "tables": "- FactInternetSales - Internet sales fact table\n- DimProduct - Product dimension",
    "columns": (
        "- FactInternetSales.OrderDate - Type: timestamp\n"
        "- FactInternetSales.ShipDate - Type: timestamp\n"
        "- FactInternetSales.Profit - Type: decimal\n"
        "- FactInternetSales.SalesAmount - Type: money\n"
        "- FactInternetSales.SalesTerritoryKey - Type: integer\n"
        "- DimProduct.ProductKey - Type: integer, PK: true"
    ),
    "relationships": "",
    "sources": "- AdventureWorks | postgresql | dbo | (Active: True)",
    "knowledge_pairs": "No knowledge pairs registered.",
    "business_terms": "No business terms registered.",
    "column_statistics": "",
    "column_samples": "",
}

_PLAN = {
    "skill": "anomaly_detection", "table": "FactInternetSales", "date_column": "OrderDate",
    "measure_column": "Profit", "agg": "sum", "grain": "week", "window_periods": 26,
    "sensitivity": 0.95, "ambiguous": None, "reason": "anomaly check",
}


def _resp(content: Any, usage=None) -> Dict[str, Any]:
    return {
        "content": json.dumps(content) if not isinstance(content, str) else content,
        "finish_reason": "stop",
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _weekly_rows(n: int = 130, start: date = date(2024, 1, 1)) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(7)
    t = np.arange(n)
    y = 100_000 + 300 * t + 20_000 * np.sin(2 * np.pi * t / 52) + rng.normal(0, 3000, n)
    if n > 40:
        y[40] *= 0.65
    return [{"ts": datetime.combine(start + timedelta(weeks=i), datetime.min.time()), "value": Decimal(f"{v:.2f}")}
            for i, v in enumerate(y)]


class _FakeRunner:
    """SqlRunner double: answers the span probe and the series query."""

    database_type = "postgres"

    def __init__(self, rows: List[Dict[str, Any]], *, span_weeks: int = 130):
        self.rows = rows
        self.span_weeks = span_weeks
        self.calls: List[str] = []

    async def run_sql(self, sql: str, **kw):
        self.calls.append(sql)
        if "min_ts" in sql:
            first = datetime(2024, 1, 1)
            return {"columns": ["min_ts", "max_ts", "n"],
                    "rows": [{"min_ts": first, "max_ts": first + timedelta(weeks=self.span_weeks - 1, days=3), "n": 12345}],
                    "row_count": 1}
        return {"columns": ["ts", "value"], "rows": self.rows, "row_count": len(self.rows)}


def _make_llm(*, route="needs_analysis", plan=_PLAN, narration=None, filters=None):
    llm = MagicMock()

    async def generate(messages, **kw):
        system = messages[0]["content"]
        if "routing classifier" in system:
            return _resp({"route": route, "reason": "test"})
        if "extract database filter intent" in system:
            return _resp({"filters": filters or []})
        if "registered analysis skill" in system:
            return _resp(plan)
        if "senior data analyst writing up" in system:
            return _resp(narration or {
                "answers_intent": True,
                "summary": "Ten weeks fall outside the expected range.",
                "insights": ["Largest deviation 35% below expectation"],
                "follow_up_questions": ["Break the flagged weeks out by territory?"],
            })
        if "senior data analyst" in system:
            return _resp({"answers_intent": True, "summary": "SQL summary", "insights": [], "follow_up_questions": []})
        return _resp({"route": "needs_query", "reason": "fallback"})

    llm.generate = AsyncMock(side_effect=generate)
    return llm


@pytest.fixture(scope="module")
def prompt_loader():
    return PromptLoader()


def _history():
    h = MagicMock()
    h.persistence_enabled = True
    h.log_query = AsyncMock(return_value=uuid4())
    h.get_conversation_context = AsyncMock(return_value=[])
    h.update_llm_response = AsyncMock()
    h.update_execution = AsyncMock()
    h.upsert_turn_artifact = AsyncMock(return_value=True)
    return h


def _build(llm, runner, store, prompt_loader, *, ml_enabled=True, analysis_runner=None, limiter=None, audit=None,
           max_series_rows=1500):
    metadata_loader = MagicMock()
    metadata_loader.load_all = AsyncMock(return_value=_METADATA)
    history = _history()
    graph = build_graph(
        llm=llm, router_llm=llm, sql_runner=runner, metadata_loader=metadata_loader,
        history_service=history, prompt_loader=prompt_loader, deployment_name="test",
        ml_skills_enabled=ml_enabled, analysis_store=store,
        analysis_runner_provider=(lambda: analysis_runner) if analysis_runner is not None else (lambda: InProcessRunner()),
        analysis_limiter=limiter, analysis_audit=audit, analysis_max_series_rows=max_series_rows,
    )
    return graph, history


def _state(question: str, **over) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "question": question, "session_id": uuid4(), "source_key": "aw", "user_context": {},
        "connection_display_name": "AdventureWorksDW", "database_type": "postgres",
        "connection_database": "aw", "connection_catalog": None, "connection_schema": "dbo",
        "query_id": uuid4(), "user_id": "user-a", "start_time": 0.0, "llm_call_count": 0,
        "llm_latency_ms": 0, "token_usage": {}, "conversation_history": [], "memory_window": 5,
        "prior_refs": [], "history_query": None, "route": "needs_query", "route_reason": "", "metadata_bundle": {},
        "catalog_seeded": False, "dialect_rules": "", "known_tables": [], "known_columns": [],
        "table_columns": {}, "catalog_available": False, "catalog_error": None, "catalog_blocked": False,
        "filter_plan": None, "resolved_filters": [], "unresolved_filters": [], "filter_ambiguities": [],
        "filter_clarification_required": False, "filter_resolution_attempts": 0, "empty_filter_diagnostics": 0,
        "needs_filter_reground": False, "filter_resolution_enabled": False, "retry_count": 0,
        "generated_sql": None, "clarification": None, "error_context": None, "sqlglot_error": None,
        "dlp_blocked": False, "governance_error": None, "query_result": None, "exec_error": None,
        "execution_time_ms": None, "is_trivial": False, "eval_result": None, "feedback_type": None,
        "eval_analytics_override": None, "llm_timeout_seconds": None, "max_result_rows": 10000,
        "statement_timeout_ms": 30000, "trace": [], "node_prompts": {}, "answer": None, "error": None,
        "analysis_skill": None, "analysis_params": None, "analysis_resume": False, "analysis_confirmed": False,
        "analysis_confirm_required": False, "analysis_override_guards": False, "analysis_proposal": None,
        "analysis_clarification": None, "analysis_guard_failure": None, "analysis_guard_results": [],
        "analysis_dropped_filters": [], "analysis_span": None, "analysis_result": None,
        "analysis_error": None, "low_confidence": False, "parent_query_id": None,
    }
    base.update(over)
    return base


def _nodes(final) -> List[str]:
    return [e["node"] for e in final.get("trace") or []]


# ── Scenarios ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_run_stops_at_confirm_and_persists_the_proposal(prompt_loader):
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows())
    graph, history = _build(_make_llm(), runner, store, prompt_loader)

    final = await graph.ainvoke(_state("Is anything weird in profit over the last six months?"))
    fr = final["formatted_response"]

    assert final["route"] == "needs_analysis"
    assert fr["status"] == "confirm"
    assert fr["sql"] is None and fr["results"] is None and fr["error"] is None
    proposal = fr["proposal"]
    assert proposal["kind"] == "confirm" and proposal["skill"] == "anomaly_detection"
    assert proposal["proposal_id"] in store.proposals
    assert {c["key"] for c in proposal["chips"]} >= {"measure_column", "date_column", "grain", "window", "sensitivity"}
    assert "sent to the analysis service" in proposal["egress_summary"]
    assert "Confirm or adjust" in fr["answer"]
    # The window was filled from the probe and only the probe ran.
    params = store.proposals[proposal["proposal_id"]]["params"]
    assert params["series"]["start"] and params["series"]["end"]
    assert len(runner.calls) == 1 and "min_ts" in runner.calls[0]
    nodes = _nodes(final)
    assert "analysis_planner" in nodes and "analysis_guard" in nodes and "analysis_sql" not in nodes
    # Persisted as a text turn carrying the proposal for restore.
    kwargs = history.upsert_turn_artifact.await_args.kwargs
    assert kwargs["result_kind"] == "text" and kwargs["analysis"]["proposal"]["kind"] == "confirm"


@pytest.mark.asyncio
async def test_confirmed_reentry_runs_the_skill_and_narrates(prompt_loader):
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows())
    audit_events: List[Dict[str, Any]] = []

    async def audit(ev):
        audit_events.append(ev)

    graph, history = _build(_make_llm(), runner, store, prompt_loader, audit=audit)
    params = {
        "series": {"table": "FactInternetSales", "schema_name": "dbo", "date_column": "OrderDate",
                   "measure_column": "Profit", "agg": "sum", "grain": "week", "start": None, "end": None,
                   "filters": []},
        "window": 130, "sensitivity": 0.95, "method": "auto",
    }
    final = await graph.ainvoke(_state(
        "Is anything weird in profit?", route="needs_analysis", analysis_skill="anomaly_detection",
        analysis_params=params, analysis_confirmed=True,
    ))
    fr = final["formatted_response"]

    assert fr["status"] == "completed", fr.get("error")
    assert fr["analysis"]["skill"] == "anomaly_detection"
    assert "rows" not in fr["analysis"]
    assert "method_options" not in fr["analysis"]  # the model is changed on the setup card, not a strip selector
    # "Edit setup": the confirm card's chips with the values that ran.
    definition = fr["analysis"]["definition"]
    chips = {c["key"]: c for c in definition["chips"]}
    assert {"measure_column", "date_column", "grain", "window", "sensitivity", "method"} <= set(chips)
    assert chips["measure_column"]["value"] == "Profit" and chips["grain"]["value"] == "week"
    assert chips["window"]["value"] == 130 and chips["method"]["options"] == ["auto", "sigma3"]
    assert chips["method"]["label"] == "Model" and chips["method"]["option_labels"]["sigma3"] == "3-sigma"
    # The card's sections and bounds come from the contract, not the browser.
    assert chips["measure_column"]["group"] == "Data" and chips["method"]["group"] == "Model"
    assert chips["window"]["group"] == "Model" and chips["window"]["unit_from"] == "grain"
    assert (chips["window"]["min"], chips["window"]["max"], chips["window"]["step"]) == (12, 1500, 1)
    assert chips["window"]["defaults_by_grain"] == {"day": 90, "week": 26, "month": 24}
    assert chips["sensitivity"]["option_labels"]["0.95"] == "95%"
    assert "sent to the analysis service" in definition["egress_summary"]
    assert fr["analysis"]["facts"]["n_flagged"] >= 1
    assert fr["low_confidence"] is False
    assert fr["results"]["columns"] == ["ts", "actual", "expected", "lower", "upper", "score", "is_anomaly", "observed"]
    # The probe's newest row is a Thursday, so the 130th week is incomplete: the
    # span's max_ts reaches the runner as ``data_end`` and that week is set aside.
    assert fr["results"]["row_count"] == 129
    partial = next(g for g in fr["analysis"]["guard_results"] if g["name"] == "partial_tail")
    assert partial["passed"] and "data ends 25 Jun 2026, 4 of 7 days" in partial["detail"]
    assert any(c.startswith("week of 22 Jun 2026 is incomplete") for c in fr["analysis"]["caveats"])
    assert fr["answer"] == "Ten weeks fall outside the expected range."  # narration summary (LLM)
    # Hybrid: findings + follow-ups come deterministically from the engine facts
    # (the LLM only writes the summary), so they mirror the narrator exactly.
    from src.analysis.narration import narrate
    _narr = narrate(final["analysis_result"])
    assert _narr.findings, "narrator should produce deterministic findings"
    assert fr["findings"] == _narr.findings
    assert fr.get("followups") == _narr.followups
    assert fr["metrics"]["skill"] == "anomaly_detection"
    assert 'DATE_TRUNC(\'WEEK\', "OrderDate")' in fr["sql"]
    nodes = _nodes(final)
    assert "analysis_planner" not in nodes and "fused_router" not in nodes
    assert nodes[:2] == ["catalog_lookup", "analysis_guard"]
    for expected in ("analysis_sql", "sqlglot_validate", "dlp_check", "execute_query", "analysis_run", "fused_eval_analytics"):
        assert expected in nodes
    assert "feedback_classifier" not in nodes
    # Narration mode used the analysis prompt, never the row sample.
    prompt = final["node_prompts"]["fused_eval_analytics"]
    assert "statistical analysis for a business" in prompt and "Sample rows" not in prompt
    # Persisted with the analysis view and the flag.
    kwargs = history.upsert_turn_artifact.await_args.kwargs
    assert kwargs["result_kind"] == "table" and kwargs["analysis"]["skill"] == "anomaly_detection"
    assert kwargs["low_confidence"] is False
    assert audit_events and audit_events[0]["outcome"] == "ok" and audit_events[0]["rows_sent"] == 130
    assert "params" in audit_events[0] and "rows" not in audit_events[0]


@pytest.mark.asyncio
async def test_remembered_consent_runs_straight_through(prompt_loader):
    store = InMemoryAnalysisStore()
    await store.set_skill_pref(user_id="user-a", source_key="aw", skill="anomaly_detection", remember=True)
    runner = _FakeRunner(_weekly_rows())
    graph, _ = _build(_make_llm(), runner, store, prompt_loader)

    final = await graph.ainvoke(_state("Is anything weird in profit?"))
    fr = final["formatted_response"]
    assert fr["status"] == "completed", fr.get("error")
    assert fr["analysis"]["params"]["window"] == 26
    assert len(runner.calls) == 2  # probe + series


@pytest.mark.asyncio
async def test_short_span_is_refused_before_any_series_sql(prompt_loader):
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows(8), span_weeks=8)
    graph, history = _build(_make_llm(), runner, store, prompt_loader)

    final = await graph.ainvoke(_state("Is anything weird in profit over the last six months?"))
    fr = final["formatted_response"]
    assert fr["status"] == "blocked" and fr["error"] is None
    proposal = fr["proposal"]
    assert proposal["kind"] == "guard"
    failed = [g for g in proposal["guard_results"] if not g["passed"]]
    assert failed[0]["name"] == "series_length" and "of 12 weeks needed" in failed[0]["detail"]
    kinds = {o["kind"] for o in proposal["options"]}
    assert {"patch", "override"} <= kinds
    assert len(runner.calls) == 1  # the probe only: a blocked run reads no series
    assert fr["answer"].endswith("needed.")


@pytest.mark.asyncio
async def test_override_runs_past_the_guard_and_flags_low_confidence(prompt_loader):
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows(30), span_weeks=30)
    graph, history = _build(_make_llm(), runner, store, prompt_loader)
    params = {"series": {"table": "FactInternetSales", "date_column": "OrderDate", "measure_column": "Profit",
                         "agg": "sum", "grain": "week", "filters": []}, "horizon": 20, "method": "seasonal_naive"}
    final = await graph.ainvoke(_state(
        "Forecast profit", route="needs_analysis", analysis_skill="forecast", analysis_params=params,
        analysis_confirmed=True, analysis_override_guards=True,
    ))
    fr = final["formatted_response"]
    assert fr["status"] == "completed", fr.get("error")
    assert fr["low_confidence"] is True and fr["analysis"]["low_confidence"] is True
    assert any(not g["passed"] and g["name"] == "max_horizon" for g in fr["analysis"]["guard_results"])
    kwargs = history.upsert_turn_artifact.await_args.kwargs
    assert kwargs["low_confidence"] is True


@pytest.mark.asyncio
async def test_forecast_window_travels_from_the_card_to_the_engine_and_back(prompt_loader):
    """A confirmed forecast with an explicit look-back: the guard sizes the history
    from it, the sandbox-bound params carry it (contract 2 accepts the field), and
    the finished result's setup card shows it as a number."""
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows(), span_weeks=130)
    graph, _ = _build(_make_llm(), runner, store, prompt_loader)
    params = {"series": {"table": "FactInternetSales", "date_column": "OrderDate", "measure_column": "Profit",
                         "agg": "sum", "grain": "week", "filters": [], "start": None, "end": None},
              "window": 60, "horizon": 8}
    final = await graph.ainvoke(_state(
        "Forecast profit", route="needs_analysis", analysis_skill="forecast", analysis_params=params,
        analysis_confirmed=True,
    ))
    fr = final["formatted_response"]
    assert fr["status"] == "completed", fr.get("error")
    assert fr["analysis"]["params"]["window"] == 60
    # The guard sized the history from the window: 60 weeks before the exclusive
    # end (the Monday after the probe's newest row, 2026-06-29).
    assert "2025-05-05" in final["generated_sql"] and "2026-06-29" in final["generated_sql"]
    chips = {c["key"]: c for c in fr["analysis"]["definition"]["chips"]}
    assert chips["window"]["value"] == 60 and isinstance(chips["window"]["value"], int)
    assert chips["method"]["label"] == "Model" and chips["horizon"]["value"] == 8
    assert chips["horizon"]["group"] == "Output" and chips["horizon"]["unit_from"] == "grain"


@pytest.mark.asyncio
async def test_planner_ambiguity_asks_with_options(prompt_loader):
    store = InMemoryAnalysisStore()
    plan = dict(_PLAN, date_column=None, ambiguous={"date_column": ["OrderDate", "ShipDate"]})
    graph, _ = _build(_make_llm(plan=plan), _FakeRunner(_weekly_rows()), store, prompt_loader)

    final = await graph.ainvoke(_state("Is anything weird in profit?"))
    fr = final["formatted_response"]
    assert fr["status"] == "clarify"
    assert [o["label"] for o in fr["proposal"]["options"]] == ["OrderDate", "ShipDate"]
    assert fr["proposal"]["options"][0]["params_patch"] == {"series": {"date_column": "OrderDate"}}
    assert "Which date" in fr["answer"]
    assert "analysis_guard" not in _nodes(final)
    # The clarification is a real, resumable proposal: persisted with the plan so
    # /api/analysis/run can complete it server-side from the chosen option.
    pid = fr["proposal"]["proposal_id"]
    assert pid and pid in store.proposals and fr["proposal"]["expires_at"]
    stored = store.proposals[pid]
    assert stored["kind"] == "clarify" and stored["skill"] == "anomaly_detection"
    assert stored["params"]["_plan"]["ambiguous"] == {"date_column": ["OrderDate", "ShipDate"]}

    # Resolving the pick and resuming (unconfirmed) lands on the confirm card, not straight in a run.
    from src.agent.analysis_planner import apply_clarification, build_params_from_plan, catalog_candidates
    resolved = apply_clarification(stored["params"]["_plan"], {"series": {"date_column": "ShipDate"}})
    outcome = build_params_from_plan(resolved, catalog_candidates(_METADATA["columns"]), resolved_filters=[],
                                     connection_schema="dbo", connection_catalog=None)
    assert outcome.kind == "params" and outcome.params["series"]["date_column"] == "ShipDate"
    runner = _FakeRunner(_weekly_rows())
    graph, _ = _build(_make_llm(plan=plan), runner, store, prompt_loader)
    final = await graph.ainvoke(_state(
        "Is anything weird in profit?", route="needs_analysis", analysis_skill="anomaly_detection",
        analysis_params={k: v for k, v in outcome.params.items() if not k.startswith("_")},
        analysis_resume=True, analysis_confirmed=False,
    ))
    fr = final["formatted_response"]
    assert fr["status"] == "confirm" and fr["proposal"]["kind"] == "confirm"
    assert fr["proposal"]["params"]["series"]["date_column"] == "ShipDate"
    assert "analysis_planner" not in _nodes(final) and len(runner.calls) == 1  # probe only


@pytest.mark.asyncio
async def test_identifiers_are_canonicalised_and_schema_comes_from_the_connection(prompt_loader):
    """A case-variant or client-chosen table/schema can never reach the SQL: the
    guard rewrites every identifier to the catalog's exact spelling and takes
    the schema from the connection."""
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows())
    graph, _ = _build(_make_llm(), runner, store, prompt_loader)
    params = {
        "series": {"table": "FACTINTERNETSALES", "schema_name": "PUBLIC", "catalog": "evil",
                   "date_column": "orderdate", "measure_column": "PROFIT", "agg": "sum", "grain": "week",
                   "filters": [{"table": "FACTINTERNETSALES", "column": "salesterritorykey", "op": "equals", "value": 6}]},
        "window": 130, "sensitivity": 0.95, "method": "auto",
    }
    final = await graph.ainvoke(_state(
        "Is anything weird in profit?", route="needs_analysis", analysis_skill="anomaly_detection",
        analysis_params=params, analysis_resume=True, analysis_confirmed=True,
    ))
    fr = final["formatted_response"]
    assert fr["status"] == "completed", fr.get("error")
    used = fr["analysis"]["params"]["series"]
    assert used["table"] == "FactInternetSales" and used["date_column"] == "OrderDate" and used["measure_column"] == "Profit"
    assert used["schema_name"] == "dbo" and used["catalog"] is None
    assert used["filters"][0]["column"] == "SalesTerritoryKey" and used["filters"][0]["table"] == "FactInternetSales"
    sql = fr["sql"]
    assert '"dbo"."FactInternetSales"' in sql and '"PUBLIC"' not in sql and "evil" not in sql
    assert "FACTINTERNETSALES" not in sql and '"OrderDate"' in sql and '"Profit"' in sql
    # Client patches cannot move the analysis to another table or schema at all.
    from src.analysis.contracts import merge_params_patch
    typed = merge_params_patch("anomaly_detection", fr["analysis"]["params"],
                               {"series": {"table": "DimProduct", "schema_name": "x", "grain": "month"}})
    assert typed.series.table == "FactInternetSales" and typed.series.schema_name == "dbo" and typed.series.grain == "month"


@pytest.mark.asyncio
async def test_fetch_limit_follows_the_skill_cap_and_truncation_is_refused(prompt_loader):
    """execute_query defaults to 100 rows; the analysis branch must raise that to
    the skill's cap and refuse to analyse a result the database cut short."""
    store = InMemoryAnalysisStore()

    class _LimitAwareRunner(_FakeRunner):
        def __init__(self, rows, **kw):
            super().__init__(rows, **kw)
            self.limits: List[int] = []

        async def run_sql(self, sql, **kw):
            if "min_ts" not in sql:
                self.limits.append(int(kw.get("limit") or 0))
                lim = int(kw.get("limit") or len(self.rows))
                out = self.rows[:lim]
                return {"columns": ["ts", "value"], "rows": out, "row_count": len(out), "truncated": len(out) < len(self.rows)}
            return await super().run_sql(sql, **kw)

    params = {
        "series": {"table": "FactInternetSales", "schema_name": "dbo", "date_column": "OrderDate",
                   "measure_column": "Profit", "agg": "sum", "grain": "week", "filters": []},
        "window": 130, "sensitivity": 0.95, "method": "auto",
    }
    runner = _LimitAwareRunner(_weekly_rows(130))
    graph, _ = _build(_make_llm(), runner, store, prompt_loader)
    final = await graph.ainvoke(_state("Is anything weird in profit?", route="needs_analysis",
                                       analysis_skill="anomaly_detection", analysis_params=params,
                                       analysis_resume=True, analysis_confirmed=True))
    assert final["formatted_response"]["status"] == "completed", final["formatted_response"].get("error")
    assert runner.limits == [1501]  # tier-A cap + 1, not the executor's default 100

    # A tiny deployment cap: the read comes back truncated and no engine runs on it.
    small = _LimitAwareRunner(_weekly_rows(130))
    graph_small, _ = _build(_make_llm(), small, store, prompt_loader, max_series_rows=50)
    final = await graph_small.ainvoke(_state("Is anything weird in profit?", route="needs_analysis",
                                             analysis_skill="anomaly_detection", analysis_params=params,
                                             analysis_resume=True, analysis_confirmed=True))
    fr = final["formatted_response"]
    assert small.limits == [51]
    assert "more than 50 rows" in (fr["error"] or "") and not fr.get("analysis")
    assert "analysis_run" in _nodes(final) and "fused_eval_analytics" not in _nodes(final)


@pytest.mark.asyncio
async def test_audit_and_sandbox_payload_never_carry_filter_values(prompt_loader):
    store = InMemoryAnalysisStore()
    seen: Dict[str, Any] = {}
    audit_events: List[Dict[str, Any]] = []

    class _SpyRunner(InProcessRunner):
        async def run(self, skill, params, series, *, override_guards=False, context=None):
            seen["params"] = params
            return await super().run(skill, params, series, override_guards=override_guards, context=context)

    async def audit(ev):
        audit_events.append(ev)

    graph, _ = _build(_make_llm(), _FakeRunner(_weekly_rows()), store, prompt_loader,
                      analysis_runner=_SpyRunner(), audit=audit)
    params = {
        "series": {"table": "FactInternetSales", "schema_name": "dbo", "date_column": "OrderDate",
                   "measure_column": "Profit", "agg": "sum", "grain": "week",
                   "filters": [{"table": "FactInternetSales", "column": "SalesTerritoryKey", "op": "in", "value": [6, 7, 8]}]},
        "window": 130, "sensitivity": 0.95, "method": "auto",
    }
    final = await graph.ainvoke(_state("Is anything weird in profit?", route="needs_analysis",
                                       analysis_skill="anomaly_detection", analysis_params=params,
                                       analysis_resume=True, analysis_confirmed=True))
    assert final["formatted_response"]["status"] == "completed", final["formatted_response"].get("error")
    for blob in (json.dumps(seen["params"]), json.dumps(audit_events[0], default=str)):
        assert "[6, 7, 8]" not in blob and '"value": 6' not in blob
    assert seen["params"]["series"]["filters"][0] == {"table": "FactInternetSales", "column": "SalesTerritoryKey",
                                                       "op": "in", "value": "<3 values>"}
    assert audit_events[0]["params"]["series"]["filters"][0]["value"] == "<3 values>"
    # The SQL that ran did apply the real values.
    assert "6, 7, 8" in final["formatted_response"]["sql"] or "6,7,8" in final["formatted_response"]["sql"].replace(" ", "")


@pytest.mark.asyncio
async def test_budget_is_charged_once_and_only_for_an_execution(prompt_loader):
    class _Limiter:
        """The graph's budget hook: ``user_id -> allowed``."""

        def __init__(self):
            self.calls: List[str] = []
            self.allowed = True

        async def __call__(self, user_id: str) -> bool:
            self.calls.append(f"analysis:{user_id}")
            return self.allowed

    limiter = _Limiter()
    store = InMemoryAnalysisStore()
    runner = _FakeRunner(_weekly_rows())
    graph, _ = _build(_make_llm(), runner, store, prompt_loader, limiter=limiter)

    # Stopping at the confirm card costs nothing.
    final = await graph.ainvoke(_state("Is anything weird in profit over the last six months?"))
    assert final["formatted_response"]["status"] == "confirm" and limiter.calls == []

    # A refused guard costs nothing either.
    short_graph, _ = _build(_make_llm(), _FakeRunner(_weekly_rows(8), span_weeks=8), store, prompt_loader, limiter=limiter)
    final = await short_graph.ainvoke(_state("Is anything weird in profit over the last six months?"))
    assert final["formatted_response"]["status"] == "blocked" and limiter.calls == []

    # The confirmed execution is charged exactly once.
    params = store.proposals[next(iter(store.proposals))]["params"]
    final = await graph.ainvoke(_state(
        "Is anything weird in profit?", route="needs_analysis", analysis_skill="anomaly_detection",
        analysis_params=params, analysis_resume=True, analysis_confirmed=True,
    ))
    assert final["formatted_response"]["status"] == "completed", final["formatted_response"].get("error")
    assert limiter.calls == ["analysis:user-a"]

    # Over budget: refused before any series SQL, with the fixed message.
    limiter.allowed = False
    runner.calls.clear()
    final = await graph.ainvoke(_state(
        "Is anything weird in profit?", route="needs_analysis", analysis_skill="anomaly_detection",
        analysis_params=params, analysis_resume=True, analysis_confirmed=True,
    ))
    fr = final["formatted_response"]
    assert "hourly limit" in (fr["error"] or "") and fr["answer"] == fr["error"]
    assert not fr.get("analysis") and not fr.get("proposal")
    assert len(runner.calls) == 1  # probe only


@pytest.mark.asyncio
async def test_planner_fallback_takes_the_sql_path(prompt_loader):
    store = InMemoryAnalysisStore()
    llm = _make_llm(plan={"skill": "none", "reason": "a plain aggregate answers it"})
    original = llm.generate.side_effect

    async def generate(messages, **kw):
        system = messages[0]["content"]
        if "Jeen Insights" in system or "SQL" in system and "registered analysis skill" not in system and "routing classifier" not in system and "extract database filter intent" not in system and "senior data analyst" not in system:
            return {"content": "", "finish_reason": "tool_calls",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "run_sql", "arguments": json.dumps({"sql": 'SELECT SUM("Profit") AS total FROM "FactInternetSales"'})}}],
                    "usage": {}}
        return await original(messages, **kw)

    llm.generate = AsyncMock(side_effect=generate)
    runner = _FakeRunner([{"total": Decimal("123.45")}])
    graph, _ = _build(llm, runner, store, prompt_loader)

    final = await graph.ainvoke(_state("Is anything weird in profit?"))
    assert final["route"] == "needs_query"
    nodes = _nodes(final)
    assert "analysis_planner" in nodes and "prompt_builder" in nodes and "sql_generator" in nodes
    assert "analysis_guard" not in nodes
    assert final["formatted_response"].get("status") is None


@pytest.mark.asyncio
async def test_ml_disabled_collapses_needs_analysis_to_needs_query(prompt_loader):
    store = InMemoryAnalysisStore()
    llm = _make_llm()
    graph, _ = _build(llm, _FakeRunner([]), store, prompt_loader, ml_enabled=False)
    # Stop right after routing by making the catalog empty → catalog_blocked.
    graph_meta = MagicMock()
    graph_meta.load_all = AsyncMock(return_value={"tables": "", "columns": ""})
    final = await graph.ainvoke(_state("Forecast profit for next quarter", catalog_seeded=True,
                                       metadata_bundle={"tables": "", "columns": ""}))
    assert final["route"] == "needs_query"
    assert "ML skills disabled" in final["route_reason"]
    assert "analysis_planner" not in _nodes(final)


@pytest.mark.asyncio
async def test_per_request_analysis_false_keeps_the_sql_path(prompt_loader):
    """'Answer with SQL instead' on a confirm card: the same question stays on SQL."""
    store = InMemoryAnalysisStore()
    graph, _ = _build(_make_llm(), _FakeRunner(_weekly_rows()), store, prompt_loader)
    final = await graph.ainvoke(_state("Forecast profit for next quarter", analysis_enabled_override=False,
                                       catalog_seeded=True, metadata_bundle={"tables": "", "columns": ""}))
    assert final["route"] == "needs_query"
    assert "analysis_planner" not in _nodes(final)


@pytest.mark.asyncio
async def test_router_keyword_cue_upgrades_needs_query(prompt_loader):
    store = InMemoryAnalysisStore()
    graph, _ = _build(_make_llm(route="needs_query"), _FakeRunner(_weekly_rows()), store, prompt_loader)
    final = await graph.ainvoke(_state("Please forecast profit for the next 8 weeks"))
    assert final["route"] == "needs_analysis"
    assert "keyword cue for forecast" in final["route_reason"]


class _FamilyRunner(_FakeRunner):
    """Answers the probe, the contribution UNION query and the entity query."""

    def __init__(self):
        super().__init__(_weekly_rows())
        rng = np.random.default_rng(3)
        n = 300
        income = rng.normal(60_000, 15_000, n) + rng.integers(0, 3, n) * 40_000
        kids = rng.integers(0, 5, n)
        self.entities = [{"entity_key": i, "YearlyIncome": Decimal(f"{income[i]:.2f}"), "TotalChildren": int(kids[i]),
                          "target": Decimal(f"{0.02 * income[i] + 500 * kids[i]:.2f}")} for i in range(n)]

    async def run_sql(self, sql: str, **kw):
        self.calls.append(sql)
        if "min_ts" in sql:
            return await super().run_sql(sql, **kw)
        if "UNION ALL" in sql.upper() or "'before'" in sql:
            rows = []
            for dim, sl, b, a in [("SalesTerritoryKey", "1", 1000, 600), ("SalesTerritoryKey", "5", 500, 520),
                                  ("ProductLine", "Mountain", 1200, 800), ("ProductLine", "Road", 300, 320)]:
                rows += [{"dimension": dim, "slice": sl, "period": "before", "value": Decimal(b)},
                         {"dimension": dim, "slice": sl, "period": "after", "value": Decimal(a)}]
            return {"columns": ["dimension", "slice", "period", "value"], "rows": rows, "row_count": len(rows)}
        if "entity_key" in sql:
            return {"columns": ["entity_key", "YearlyIncome", "TotalChildren", "target"], "rows": self.entities, "row_count": len(self.entities)}
        return await super().run_sql(sql, **kw)


_METADATA_P6 = dict(_METADATA, tables=_METADATA["tables"] + "\n- DimCustomer - Customer dimension\n- FactExperiment - Experiment log", columns=_METADATA["columns"] + (
    "\n- FactInternetSales.ProductLine - Type: varchar"
    "\n- DimCustomer.CustomerKey - Type: integer"
    "\n- DimCustomer.YearlyIncome - Type: money"
    "\n- DimCustomer.TotalChildren - Type: integer"
    "\n- DimCustomer.Gender - Type: char"
    "\n- DimCustomer.SignupDate - Type: date"
    "\n- DimCustomer.LastOrderDate - Type: date"
    "\n- FactExperiment.Variant - Type: varchar"
    "\n- FactExperiment.Converted - Type: integer"
))


def _build_p6(llm, runner, store, prompt_loader, **kw):
    metadata_loader = MagicMock()
    metadata_loader.load_all = AsyncMock(return_value=_METADATA_P6)
    history = _history()
    graph = build_graph(
        llm=llm, router_llm=llm, sql_runner=runner, metadata_loader=metadata_loader,
        history_service=history, prompt_loader=prompt_loader, deployment_name="test",
        ml_skills_enabled=True, analysis_store=store, analysis_runner_provider=lambda: InProcessRunner(), **kw,
    )
    return graph, history


@pytest.mark.asyncio
async def test_contribution_flow_defaults_the_periods_and_runs(prompt_loader):
    store = InMemoryAnalysisStore()
    await store.set_skill_pref(user_id="user-a", source_key="aw", skill="contribution", remember=True)
    plan = {"skill": "contribution", "table": "FactInternetSales", "date_column": "OrderDate", "measure_column": "Profit",
            "agg": "sum", "dimensions": ["SalesTerritoryKey", "ProductLine"], "reason": "why did it drop"}
    runner = _FamilyRunner()
    graph, history = _build_p6(_make_llm(plan=plan), runner, store, prompt_loader)
    final = await graph.ainvoke(_state("Why did profit drop last quarter?"))
    fr = final["formatted_response"]
    assert fr["status"] == "completed", fr.get("error")
    analysis = fr["analysis"]
    assert analysis["skill"] == "contribution" and analysis["facts"]["delta"] == -380
    # Periods were filled from the probe: last quarter vs the one before.
    params = analysis["params"]
    assert params["after_end"] and params["before_start"] and params["before_end"] == params["after_start"]
    assert "UNION ALL" in fr["sql"].upper() and "'before'" in fr["sql"]
    assert fr["results"]["columns"][:2] == ["dimension", "slice"]
    assert analysis["chart_spec"]["chart_type"] == "horizontal_bar"
    assert history.upsert_turn_artifact.await_args.kwargs["analysis"]["skill"] == "contribution"


@pytest.mark.asyncio
async def test_tier_b_clustering_flow_confirms_with_row_level_egress_then_runs(prompt_loader):
    store = InMemoryAnalysisStore()
    plan = {"skill": "clustering", "table": "DimCustomer", "entity_key": "CustomerKey",
            "features": ["YearlyIncome", "TotalChildren", "Gender"], "k": 3, "reason": "segment customers"}
    runner = _FamilyRunner()
    graph, _ = _build_p6(_make_llm(plan=plan), runner, store, prompt_loader)

    # First run: confirm card states the row-level egress; no probe, no rows read.
    final = await graph.ainvoke(_state("Segment our customers by income and children"))
    fr = final["formatted_response"]
    assert fr["status"] == "confirm", fr.get("error")
    proposal = fr["proposal"]
    assert proposal["tier"] == "B" and "Row-level" in proposal["egress_summary"] and "audited" in proposal["egress_summary"]
    assert {c["key"] for c in proposal["chips"]} >= {"entity_key", "features", "row_cap", "k"}
    features_chip = next(c for c in proposal["chips"] if c["key"] == "features")
    # Features are picked from the catalog's numeric columns, not typed as a comma list.
    assert features_chip["kind"] == "multiselect" and isinstance(features_chip["value"], list)
    assert (features_chip["min"], features_chip["max"]) == (2, 8)
    # Clustering has no "Output" section and its row cap is a data-volume control.
    assert {c["group"] for c in proposal["chips"]} == {"Data", "Model"}
    assert next(c for c in proposal["chips"] if c["key"] == "row_cap")["group"] == "Data"
    assert runner.calls == []  # nothing read before consent
    params = store.proposals[proposal["proposal_id"]]["params"]
    assert params["entity"]["features"] == ["YearlyIncome", "TotalChildren"]  # Gender dropped by the guard

    # Confirmed re-entry runs the entity query and the engine.
    audit_events = []

    async def audit(ev):
        audit_events.append(ev)

    graph2, history = _build_p6(_make_llm(plan=plan), runner, store, prompt_loader, analysis_audit=audit)
    final2 = await graph2.ainvoke(_state("Segment our customers", route="needs_analysis", analysis_skill="clustering",
                                         analysis_params=params, analysis_confirmed=True))
    fr2 = final2["formatted_response"]
    assert fr2["status"] == "completed", fr2.get("error")
    assert fr2["analysis"]["egress"]["tier"] == "B" and fr2["analysis"]["facts"]["k"] == 3
    assert fr2["results"]["columns"][:2] == ["entity_key", "cluster"] and fr2["results"]["row_count"] == 300
    assert '"entity_key"' in fr2["sql"] and "ORDER BY" in fr2["sql"].upper()
    # Audited with the column list, never values.
    assert audit_events and audit_events[-1]["columns"] == ["entity_key", "YearlyIncome", "TotalChildren", "target"]
    assert "rows" not in audit_events[-1]


class _CohortRunner(_FakeRunner):
    """Answers the activity span probe and the cohort UNION query."""

    def __init__(self, n_cohorts: int = 6, offsets: int = 5, size: int = 120, decay: float = 0.8):
        super().__init__(_weekly_rows())
        rows: List[Dict[str, Any]] = []
        for m in range(n_cohorts):
            cohort = (date(2024, 1, 1) + timedelta(days=31 * m)).replace(day=1).isoformat()
            rows.append({"cohort": cohort, "period": None, "active": size})
            for off in range(offsets):
                p = (date.fromisoformat(cohort) + timedelta(days=31 * off)).replace(day=1).isoformat()
                rows.append({"cohort": cohort, "period": p, "active": int(round(size * (decay ** off)))})
        self._cohort_rows = rows

    async def run_sql(self, sql: str, **kw):
        self.calls.append(sql)
        if "min_ts" in sql:
            first = datetime(2024, 1, 1)
            return {"columns": ["min_ts", "max_ts", "n"],
                    "rows": [{"min_ts": first, "max_ts": first + timedelta(days=210), "n": 5000}], "row_count": 1}
        if "COUNT(DISTINCT" in sql.upper() or '"active"' in sql:
            return {"columns": ["cohort", "period", "active"], "rows": self._cohort_rows, "row_count": len(self._cohort_rows)}
        return await super().run_sql(sql, **kw)


@pytest.mark.asyncio
async def test_cohort_retention_flow_runs_and_charts_a_curve(prompt_loader):
    store = InMemoryAnalysisStore()
    await store.set_skill_pref(user_id="user-a", source_key="aw", skill="cohort_retention", remember=True)
    plan = {"skill": "cohort_retention", "table": "DimCustomer", "entity_key": "CustomerKey",
            "cohort_date": "SignupDate", "activity_date": "LastOrderDate", "grain": "month",
            "max_periods": 12, "reason": "retention by signup cohort"}
    runner = _CohortRunner()
    graph, history = _build_p6(_make_llm(plan=plan), runner, store, prompt_loader)
    final = await graph.ainvoke(_state("Show me the retention curve by monthly signup cohort"))
    fr = final["formatted_response"]
    assert fr["status"] == "completed", fr.get("error")
    analysis = fr["analysis"]
    assert analysis["skill"] == "cohort_retention"
    assert analysis["egress"]["tier"] == "A" and analysis["chart_spec"]["chart_type"] == "line"
    assert analysis["facts"]["n_cohorts"] == 6
    assert "COUNT(DISTINCT" in fr["sql"].upper() and "UNION ALL" in fr["sql"].upper()
    # A size-weighted average curve leads the per-cohort rows.
    assert fr["results"]["rows"][0]["cohort"] == "All cohorts"
    assert history.upsert_turn_artifact.await_args.kwargs["analysis"]["skill"] == "cohort_retention"


class _ExperimentRunner(_FakeRunner):
    """Answers the per-arm summary query for an A/B test (no span probe)."""

    def __init__(self):
        super().__init__(_weekly_rows())

    async def run_sql(self, sql: str, **kw):
        self.calls.append(sql)
        if "sum_x2" in sql or ('"arm"' in sql and "SUM" in sql.upper()):
            rows = [{"arm": "control", "n": 1000, "sum_x": 100, "sum_x2": 100},
                    {"arm": "treatment", "n": 1000, "sum_x": 140, "sum_x2": 140}]
            return {"columns": ["arm", "n", "sum_x", "sum_x2"], "rows": rows, "row_count": len(rows)}
        return await super().run_sql(sql, **kw)


@pytest.mark.asyncio
async def test_experiment_flow_confirms_then_runs_the_test(prompt_loader):
    store = InMemoryAnalysisStore()
    plan = {"skill": "experiment_test", "table": "FactExperiment", "group_column": "Variant",
            "outcome_column": "Converted", "outcome_type": "binary", "control": "control",
            "reason": "a/b test"}
    runner = _ExperimentRunner()
    graph, _ = _build_p6(_make_llm(plan=plan), runner, store, prompt_loader)

    # First run: aggregate-tier confirm card, no rows read before consent.
    final = await graph.ainvoke(_state("Did variant B beat control in our A/B test?"))
    fr = final["formatted_response"]
    assert fr["status"] == "confirm", fr.get("error")
    proposal = fr["proposal"]
    assert proposal["tier"] == "A" and {c["key"] for c in proposal["chips"]} >= {"group_column", "outcome_column", "outcome_type"}
    assert runner.calls == []
    params = store.proposals[proposal["proposal_id"]]["params"]

    # Confirmed re-entry runs the summary query and the test.
    graph2, history = _build_p6(_make_llm(plan=plan), runner, store, prompt_loader)
    final2 = await graph2.ainvoke(_state("Did variant B beat control", route="needs_analysis",
                                         analysis_skill="experiment_test", analysis_params=params, analysis_confirmed=True))
    fr2 = final2["formatted_response"]
    assert fr2["status"] == "completed", fr2.get("error")
    analysis = fr2["analysis"]
    assert analysis["skill"] == "experiment_test" and analysis["egress"]["tier"] == "A"
    assert analysis["facts"]["significant"] is True and analysis["facts"]["control"] == "control"
    assert "SUM(" in fr2["sql"].upper() and analysis["chart_spec"]["chart_type"] == "bar"
    assert history.upsert_turn_artifact.await_args.kwargs["analysis"]["skill"] == "experiment_test"


@pytest.mark.asyncio
async def test_no_runner_yields_a_clear_message(prompt_loader):
    store = InMemoryAnalysisStore()
    await store.set_skill_pref(user_id="user-a", source_key="aw", skill="anomaly_detection", remember=True)
    metadata_loader = MagicMock()
    metadata_loader.load_all = AsyncMock(return_value=_METADATA)
    graph = build_graph(
        llm=_make_llm(), router_llm=_make_llm(), sql_runner=_FakeRunner(_weekly_rows()),
        metadata_loader=metadata_loader, history_service=_history(), prompt_loader=prompt_loader,
        deployment_name="t", ml_skills_enabled=True, analysis_store=store, analysis_runner_provider=lambda: None,
    )
    final = await graph.ainvoke(_state("Is anything weird in profit?"))
    fr = final["formatted_response"]
    assert "not available" in fr["error"] and fr.get("status") is None


# ── A forecast's named period must reach the planner as the horizon ──────────

_METADATA_DIMDATE = dict(
    _METADATA,
    tables=_METADATA["tables"] + "\n- DimDate - Calendar dimension",
    columns=_METADATA["columns"] + "\n- DimDate.FullDateAlternateKey - Type: date\n- DimDate.DateKey - Type: integer",
)
_FORECAST_PLAN = {
    "skill": "forecast", "table": "FactInternetSales", "date_column": "OrderDate", "measure_column": "Profit",
    "agg": "sum", "grain": "month", "horizon": None, "start": None, "end": None, "ambiguous": None,
    "reason": "forecast",
}


def _build_with_dimdate(llm, runner, store, prompt_loader):
    metadata_loader = MagicMock()
    metadata_loader.load_all = AsyncMock(return_value=_METADATA_DIMDATE)
    graph = build_graph(
        llm=llm, router_llm=llm, sql_runner=runner, metadata_loader=metadata_loader,
        history_service=_history(), prompt_loader=prompt_loader, deployment_name="test",
        ml_skills_enabled=True, analysis_store=store, analysis_runner_provider=lambda: InProcessRunner(),
    )
    return graph


@pytest.mark.asyncio
@pytest.mark.parametrize("period,expected_horizon", [
    (["7/2008", "1/2009"], 7),   # readable month/year: Jul 2008 .. Jan 2009 = 7 months
    (["H2 2008", "H1 2009"], 8),  # unreadable: not a reason to stop; the default horizon stands
])
async def test_forecast_period_bound_to_a_date_column_becomes_the_horizon(prompt_loader, period, expected_horizon):
    """Regression: "forcast the profit for the next 6 month (7/2008 to 1/2009)" dead-ended
    in the filter grounder with 'I could not verify ... in fulldatealternatekey'."""
    store = InMemoryAnalysisStore()
    dimdate_filter = {"table": "dimdate", "column": "fulldatealternatekey", "op": "between", "value": period}
    llm = _make_llm(plan=_FORECAST_PLAN, filters=[dimdate_filter])
    graph = _build_with_dimdate(llm, _FakeRunner(_weekly_rows()), store, prompt_loader)

    final = await graph.ainvoke(_state(
        f"forcast the profit for the next 6 month ({period[0]} to {period[1]})", filter_resolution_enabled=True,
    ))
    fr = final["formatted_response"]

    assert final["route"] == "needs_analysis"
    assert not final.get("filter_clarification_required"), fr.get("answer")
    assert fr["status"] == "confirm", fr.get("answer") or fr.get("error")
    proposal = fr["proposal"]
    assert proposal["skill"] == "forecast"
    params = store.proposals[proposal["proposal_id"]]["params"]
    assert params["horizon"] == expected_horizon
    assert params["series"]["filters"] == []  # the period is not a filter on the history
    assert final.get("analysis_dropped_filters") == []  # ...nor reported as one that was dropped
    # The look-back the guard filled (24 months by default) is a real parameter
    # the card shows and can change — not an empty chip.
    assert params["window"] == 24
    window_chip = next(c for c in proposal["chips"] if c["key"] == "window")
    assert window_chip["value"] == 24
    assert next(c for c in proposal["chips"] if c["key"] == "method")["label"] == "Model"
    nodes = _nodes(final)
    assert "filter_grounder" in nodes and "analysis_planner" in nodes and "analysis_guard" in nodes
