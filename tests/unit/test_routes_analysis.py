"""/api/analysis/* — proposals are the only trusted source of params, ownership
and expiry are enforced, patches are allowlisted, re-runs create child turns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.agent.analysis_store import InMemoryAnalysisStore
from src.api import state as api_state

_PARAMS = {
    "series": {"table": "FactInternetSales", "schema_name": "dbo", "catalog": None, "date_column": "OrderDate",
               "measure_column": "Profit", "agg": "sum", "grain": "week", "start": "2026-03-02",
               "end": "2026-09-07", "filters": [], "timezone": "UTC", "week_start": "monday"},
    "window": 26, "sensitivity": 0.95, "method": "auto",
}


def _result(query_id=None, **over):
    base = {
        "question": "Is anything weird in profit?", "query_id": query_id or uuid4(), "session_id": uuid4(),
        "sql": "SELECT 1", "results": {"columns": ["ts"], "rows": [], "row_count": 0}, "answer": "ok",
        "prompt": None, "error": None, "metrics": {}, "status": "completed",
        "analysis": {"skill": "anomaly_detection", "params": _PARAMS, "low_confidence": False},
        "low_confidence": False,
    }
    base.update(over)
    return base


@pytest.fixture
def ml_state(fake_state, monkeypatch):
    store = InMemoryAnalysisStore()
    monkeypatch.setattr(api_state, "analysis_store", store)
    monkeypatch.setattr(api_state, "rate_limiter", None)
    agent = MagicMock()
    agent.process_confirmed_analysis = AsyncMock(return_value=_result())
    agent.source_key = "sales_db"
    agent.connection = SimpleNamespace(db_schema="dbo", connection_catalog=None)
    agent.metadata_loader.load_all = AsyncMock(return_value={
        "columns": "- FactInternetSales.OrderDate - Type: timestamp\n- FactInternetSales.ShipDate - Type: timestamp\n"
                   "- FactInternetSales.Profit - Type: decimal\n- FactInternetSales.SalesAmount - Type: money",
    })
    fake_state.agent_registry.get_agent = AsyncMock(return_value=agent)
    fake_state.history_service.conversation_belongs_to_user = AsyncMock(return_value=True)
    fake_state.history_service.get_turn_analysis = AsyncMock(return_value=None)
    fake_state.store = store
    fake_state.agent = agent
    return fake_state


async def _proposal(store, **over):
    kw = dict(user_id="user-a", source_key="sales_db", session_id=uuid4(), parent_query_id=uuid4(),
              kind="confirm", skill="anomaly_detection", params=_PARAMS, question="Is anything weird in profit?",
              proposal={"kind": "confirm"})
    kw.update(over)
    return await store.create_proposal(**kw)


def test_run_requires_auth(anon_client):
    r = anon_client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": str(uuid4())})
    assert r.status_code == 401


def test_run_unknown_proposal_is_404(client, ml_state):
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": str(uuid4())})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_other_users_proposal_is_404(client, ml_state):
    pid = await _proposal(ml_state.store, user_id="user-b")
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_other_connection_is_404(client, ml_state):
    pid = await _proposal(ml_state.store)
    r = client.post("/api/analysis/run", json={"connection": "other_db", "proposal_id": pid})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_expired_is_410(client, ml_state):
    pid = await _proposal(ml_state.store)
    ml_state.store.proposals[pid]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid})
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_run_merges_allowlisted_patch_and_consumes_once(client, ml_state):
    pid = await _proposal(ml_state.store)
    body = {"connection": "sales_db", "proposal_id": pid,
            "params_patch": {"grain": "month", "sensitivity": 0.9, "rows": "smuggled", "series": {"agg": "avg"}},
            "remember": True, "idempotency_key": "k1"}
    r = client.post("/api/analysis/run", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["status"] == "completed" and payload["analysis"]["skill"] == "anomaly_detection"

    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["skill"] == "anomaly_detection"
    assert call["params"]["series"]["grain"] == "month"
    assert call["params"]["series"]["agg"] == "avg"
    assert call["params"]["sensitivity"] == 0.9
    assert "rows" not in call["params"]
    assert call["parent_query_id"] == ml_state.store.proposals[pid]["parent_query_id"]
    assert call["override_guards"] is False
    assert call["confirmed"] is True  # the confirm card is the consent
    # Consent remembered; proposal claimed exactly once and its child turn recorded.
    assert await ml_state.store.has_skill_pref(user_id="user-a", source_key="sales_db", skill="anomaly_detection")
    row = ml_state.store.proposals[pid]
    assert row["consumed_at"] is not None and str(row["consumed_query_id"]) == payload["query_id"]
    again = client.post("/api/analysis/run", json=body)
    assert again.status_code == 409 and again.json()["detail"]["query_id"] == payload["query_id"]
    assert ml_state.agent.process_confirmed_analysis.await_count == 1


@pytest.mark.asyncio
async def test_run_claims_before_executing_and_releases_on_failure(client, ml_state):
    """A duplicate click during a run never starts a second analysis; a crash frees the slot."""
    pid = await _proposal(ml_state.store)
    # Simulate the claim of an in-flight run: a second caller must be refused.
    assert await ml_state.store.claim_proposal(pid, user_id="user-a", idempotency_key="k-a") == "claimed"
    assert await ml_state.store.claim_proposal(pid, user_id="user-a", idempotency_key="k-b") == "consumed"
    other = await _proposal(ml_state.store)
    assert await ml_state.store.claim_proposal(other, user_id="user-a", idempotency_key="k-a") == "duplicate_key"
    assert await ml_state.store.claim_proposal(uuid4(), user_id="user-a", idempotency_key=None) == "not_found"
    # Release after a failed execution.
    ml_state.agent.process_confirmed_analysis = AsyncMock(side_effect=RuntimeError("boom"))
    failing = await _proposal(ml_state.store)
    with pytest.raises(RuntimeError):  # TestClient re-raises server exceptions
        client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": failing})
    assert ml_state.store.proposals[failing]["consumed_at"] is None, "a crashed run must not burn the proposal"
    ml_state.agent.process_confirmed_analysis = AsyncMock(return_value=_result())
    assert client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": failing}).status_code == 200


@pytest.mark.asyncio
async def test_run_rejects_invalid_patch(client, ml_state):
    pid = await _proposal(ml_state.store)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid,
                                               "params_patch": {"sensitivity": 5}})
    assert r.status_code == 422
    assert not ml_state.store.proposals[pid]["consumed_at"]
    # The body names the chip so the setup card can mark that field.
    detail = r.json()["detail"]
    assert detail["field"] == "sensitivity" and detail["loc"] == ["sensitivity"]
    assert detail["message"].startswith("Invalid parameter:")
    # A nested location resolves to the chip key, not the container.
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid,
                                               "params_patch": {"grain": "fortnight"}})
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "grain"
    assert r.json()["detail"]["loc"] == ["series", "grain"]


@pytest.mark.asyncio
async def test_run_guard_exit_forwards_override_but_not_consent(client, ml_state):
    pid = await _proposal(ml_state.store, kind="guard")
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid, "override_guards": True})
    assert r.status_code == 200
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["override_guards"] is True
    assert call["confirmed"] is False, "a guard exit is not consent: the confirm card still follows on a first run"
    # An override recorded on a confirm card (chosen on the guard card before it) survives the card.
    pid2 = await _proposal(ml_state.store, kind="confirm", params={**_PARAMS, "_override_guards": True})
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid2})
    assert r.status_code == 200
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["override_guards"] is True and call["confirmed"] is True
    assert "_override_guards" not in call["params"]


@pytest.mark.asyncio
async def test_run_resolves_a_clarification_from_the_stored_plan(client, ml_state):
    """The pick completes the server's plan; the client never supplies params."""
    plan = {"skill": "anomaly_detection", "table": "FactInternetSales", "measure_column": "Profit", "agg": "sum",
            "grain": "week", "ambiguous": {"date_column": ["OrderDate", "ShipDate"]}}
    pid = await _proposal(ml_state.store, kind="clarify", params={"_plan": plan, "_filters": []})
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid,
                                               "params_patch": {"series": {"date_column": "ShipDate"}}})
    assert r.status_code == 200, r.text
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["skill"] == "anomaly_detection"
    assert call["params"]["series"]["date_column"] == "ShipDate"
    assert call["params"]["series"]["schema_name"] == "dbo"
    assert call["confirmed"] is False  # not consented yet: the confirm card follows unless remembered
    # A pick that names a column the catalog does not have stays ambiguous → 409, nothing runs.
    ml_state.agent.process_confirmed_analysis.reset_mock()
    pid2 = await _proposal(ml_state.store, kind="clarify", params={"_plan": plan, "_filters": []})
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid2,
                                               "params_patch": {"series": {"date_column": "DeliveredAt"}}})
    assert r.status_code == 409 and "Still ambiguous" in r.json()["detail"]
    assert ml_state.agent.process_confirmed_analysis.await_count == 0
    # A clarification with no stored plan cannot be resumed.
    pid3 = await _proposal(ml_state.store, kind="clarify", params={})
    assert client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid3}).status_code == 409


_FORECAST_PARAMS = {
    "series": {**_PARAMS["series"], "grain": "month", "start": "2006-08-01", "end": "2008-08-01"},
    "window": 24, "horizon": 8, "interval": 0.8, "method": "auto",
}


@pytest.mark.asyncio
async def test_run_window_or_grain_change_resets_the_stored_range(client, ml_state):
    """A confirm card already carries the range the guard filled; a wider window
    typed on it (or a grain change) must make the guard re-probe, or the range
    would stay exactly where it was and the edit would be a no-op."""
    pid = await _proposal(ml_state.store, skill="forecast", params=_FORECAST_PARAMS)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid, "params_patch": {"window": 36}})
    assert r.status_code == 200, r.text
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["params"]["window"] == 36
    assert call["params"]["series"]["start"] is None and call["params"]["series"]["end"] is None
    # A "Look back N months" guard exit goes through the same door.
    pid = await _proposal(ml_state.store, kind="guard", skill="forecast", params=_FORECAST_PARAMS)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid, "params_patch": {"window": 48}})
    assert r.status_code == 200, r.text
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["params"]["window"] == 48 and call["params"]["series"]["start"] is None
    # A grain change likewise; an unrelated patch keeps the range.
    pid = await _proposal(ml_state.store, skill="forecast", params=_FORECAST_PARAMS)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid, "params_patch": {"grain": "week"}})
    assert r.status_code == 200 and ml_state.agent.process_confirmed_analysis.await_args.kwargs["params"]["series"]["start"] is None
    pid = await _proposal(ml_state.store, skill="forecast", params=_FORECAST_PARAMS)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid, "params_patch": {"horizon": 12}})
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert r.status_code == 200 and call["params"]["horizon"] == 12
    assert call["params"]["series"]["start"] == "2006-08-01" and call["params"]["window"] == 24


@pytest.mark.asyncio
async def test_run_refuses_a_proposal_from_an_older_contract(client, ml_state):
    pid = await _proposal(ml_state.store, contract_version="1")
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid})
    assert r.status_code == 409 and "contract changed" in r.json()["detail"]
    ml_state.agent.process_confirmed_analysis.assert_not_awaited()


def test_run_disabled_is_404(client, ml_state, monkeypatch):
    from src.api.routes import analysis as routes

    monkeypatch.setattr(routes.settings, "ML_SKILLS_ENABLED", False)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": str(uuid4())})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_does_not_charge_the_budget_at_the_route(client, ml_state, monkeypatch):
    """The hourly budget is charged once, inside the graph, right before an execution."""
    pid = await _proposal(ml_state.store)
    limiter = MagicMock()
    limiter.allow = AsyncMock(return_value=False)
    monkeypatch.setattr(api_state, "rate_limiter", limiter)
    r = client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid})
    assert r.status_code == 200
    limiter.allow.assert_not_awaited()


def test_rerun_needs_a_parent_analysis(client, ml_state):
    r = client.post("/api/analysis/rerun", json={"connection": "sales_db", "parent_query_id": str(uuid4()),
                                                 "session_id": str(uuid4()), "params_patch": {"grain": "month"}})
    assert r.status_code == 404


def test_rerun_unusable_instruction_points_to_adjust_analysis(client, ml_state, monkeypatch):
    from src.api.routes import analysis as routes

    parent_id, session = uuid4(), uuid4()
    ml_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "turn_id": str(parent_id),
        "session_id": session,
        "question": "Forecast sales",
        "analysis": {"skill": "forecast", "params": _FORECAST_PARAMS},
        "low_confidence": False,
    })
    monkeypatch.setattr(routes, "_patch_from_instruction", AsyncMock(return_value={}))

    r = client.post("/api/analysis/rerun", json={
        "connection": "sales_db",
        "parent_query_id": str(parent_id),
        "session_id": str(session),
        "instruction": "make it different somehow",
    })

    assert r.status_code == 422
    assert "Adjust analysis" in r.json()["detail"]
    assert "chips" not in r.json()["detail"].lower()


def test_rerun_with_patch_creates_a_child_turn_with_a_diff(client, ml_state):
    parent_id, session = uuid4(), uuid4()
    ml_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "turn_id": str(parent_id), "session_id": session, "question": "Is anything weird in profit?",
        "analysis": {"skill": "anomaly_detection", "params": _PARAMS}, "low_confidence": False,
    })
    ml_state.agent.process_confirmed_analysis.return_value = _result(trace=[
        {"node": "analysis_guard", "elapsed_ms": 11},
        {"node": "analysis_sql", "elapsed_ms": 12},
        {"node": "execute_query", "elapsed_ms": 13},
        {"node": "analysis_run", "elapsed_ms": 14},
        {"node": "fused_eval_analytics", "elapsed_ms": 15},
    ])
    r = client.post("/api/analysis/rerun", json={"connection": "sales_db", "parent_query_id": str(parent_id),
                                                 "session_id": str(session), "params_patch": {"grain": "month"}})
    assert r.status_code == 200, r.text
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["parent_query_id"] == parent_id and call["session_id"] == session
    assert call["params"]["series"]["grain"] == "month"
    # A grain change invalidates the stored window so the guard re-probes.
    assert call["params"]["series"]["start"] is None and call["params"]["series"]["end"] is None
    assert r.json()["analysis"]["param_diff"] == {"grain": {"from": "week", "to": "month"},
                                                 "start": {"from": "2026-03-02", "to": None},
                                                 "end": {"from": "2026-09-07", "to": None}}
    timing = r.json()["metrics"]["analysis_rerun"]
    assert timing["structured_patch"] is True
    assert timing["instruction_ms"] == 0
    assert timing["analysis_ms"] >= 0 and timing["total_ms"] >= timing["analysis_ms"]
    assert set(timing["stages_ms"]) == {
        "guard_ms", "query_build_ms", "data_extraction_ms", "ml_execution_ms", "narration_ms",
    }
    assert timing["stages_ms"] == {
        "guard_ms": 11,
        "query_build_ms": 12,
        "data_extraction_ms": 13,
        "ml_execution_ms": 14,
        "narration_ms": 15,
    }
    # The re-run was recorded as a consumed proposal carrying its child turn.
    rerun = next(p for p in ml_state.store.proposals.values() if p["kind"] == "rerun")
    assert rerun["consumed_at"] and str(rerun["consumed_query_id"]) == r.json()["query_id"]
    # Replaying the same idempotency key cannot execute again.
    body = {"connection": "sales_db", "parent_query_id": str(parent_id), "session_id": str(session),
            "params_patch": {"grain": "month"}, "idempotency_key": "rerun-1"}
    assert client.post("/api/analysis/rerun", json=body).status_code == 200
    assert client.post("/api/analysis/rerun", json=body).status_code == 409
    assert ml_state.agent.process_confirmed_analysis.await_count == 2


def test_rerun_forecast_window_patch_resets_the_range(client, ml_state):
    parent_id, session = uuid4(), uuid4()
    ml_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "turn_id": str(parent_id), "session_id": session, "question": "Forecast sales",
        "analysis": {"skill": "forecast", "params": _FORECAST_PARAMS}, "low_confidence": False,
    })
    r = client.post("/api/analysis/rerun", json={"connection": "sales_db", "parent_query_id": str(parent_id),
                                                 "session_id": str(session), "params_patch": {"window": 36}})
    assert r.status_code == 200, r.text
    call = ml_state.agent.process_confirmed_analysis.await_args.kwargs
    assert call["params"]["window"] == 36
    assert call["params"]["series"]["start"] is None and call["params"]["series"]["end"] is None
    assert r.json()["analysis"]["param_diff"]["window"] == {"from": 24, "to": 36}


def test_rerun_wrong_session_is_409(client, ml_state):
    parent_id = uuid4()
    ml_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "turn_id": str(parent_id), "session_id": uuid4(), "question": "q",
        "analysis": {"skill": "anomaly_detection", "params": _PARAMS}, "low_confidence": False,
    })
    r = client.post("/api/analysis/rerun", json={"connection": "sales_db", "parent_query_id": str(parent_id),
                                                 "session_id": str(uuid4()), "params_patch": {"grain": "month"}})
    assert r.status_code == 409


def test_rerun_without_changes_is_422(client, ml_state):
    parent_id, session = uuid4(), uuid4()
    ml_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "turn_id": str(parent_id), "session_id": session, "question": "q",
        "analysis": {"skill": "anomaly_detection", "params": _PARAMS}, "low_confidence": False,
    })
    r = client.post("/api/analysis/rerun", json={"connection": "sales_db", "parent_query_id": str(parent_id),
                                                 "session_id": str(session)})
    assert r.status_code == 422


def test_chart_builds_band_from_cache_and_persists_baseline(client, ml_state, monkeypatch):
    from src.api.result_cache import result_cache
    from src.api.routes import charts as charts_routes

    qid = str(uuid4())
    ml_state.history_service.query_belongs_to_user = AsyncMock(return_value=True)
    persisted = {}

    async def _persist(**kw):
        persisted.update(kw)

    monkeypatch.setattr(charts_routes, "_persist_chart_baseline", _persist)
    rows = [{"ts": f"2026-01-{d:02d}", "actual": 10 + d, "expected": 10 + d, "lower": 8 + d, "upper": 12 + d, "is_anomaly": d == 3}
            for d in range(1, 15)]
    result_cache.put(user_id="user-a", connection="sales_db", query_id=qid, dataset={"columns": list(rows[0]), "rows": rows})
    spec = {"chart_type": "band", "x_column": "ts", "series": [
        {"role": "actual", "label": "Actual", "column": "actual"},
        {"role": "expected", "label": "Expected", "column": "expected"},
        {"role": "interval", "label": "95% band", "lower_column": "lower", "upper_column": "upper"},
        {"role": "flagged", "label": "Flagged", "column": "actual", "flag_column": "is_anomaly"},
    ]}
    r = client.post("/api/analysis/chart", json={"connection": "sales_db", "query_id": qid, "chart_spec": spec})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chart_type"] == "band"
    assert "chart-build;dur=" in r.headers["server-timing"]
    roles = [s.get("jeenRole") for s in body["chart_config"]["series"]]
    assert "actual" in roles and "flagged" in roles
    assert persisted["query_id"] == qid and persisted["chart_spec"]["chart_type"] == "band"
    assert persisted["request_started_at"].tzinfo is not None

    # Cache miss without rows → 409; with rows re-sent → 200.
    other = str(uuid4())
    r = client.post("/api/analysis/chart", json={"connection": "sales_db", "query_id": other, "chart_spec": spec})
    assert r.status_code == 409
    r = client.post("/api/analysis/chart", json={"connection": "sales_db", "query_id": other, "chart_spec": spec,
                                                 "results": {"columns": list(rows[0]), "rows": rows}})
    assert r.status_code == 200
    # Invalid spec → 422; foreign query → 404.
    r = client.post("/api/analysis/chart", json={"connection": "sales_db", "query_id": qid, "chart_spec": {"chart_type": "pie", "series": []}})
    assert r.status_code == 422
    ml_state.history_service.query_belongs_to_user = AsyncMock(return_value=False)
    r = client.post("/api/analysis/chart", json={"connection": "sales_db", "query_id": qid, "chart_spec": spec})
    assert r.status_code == 404


def test_suggestions_come_from_a_table_with_dates_and_measures(client, ml_state):
    ml_state.metadata_loader.load_all = AsyncMock(return_value={
        "columns": "- FactInternetSales.OrderDate - Type: timestamp\n- FactInternetSales.Profit - Type: decimal\n- DimProduct.ProductKey - Type: integer",
    })
    r = client.get("/api/analysis/suggestions", params={"connection": "sales_db"})
    assert r.status_code == 200
    body = r.json()
    assert body["table"] == "FactInternetSales"
    # One ML suggestion per connection (spec §8.9), chosen from the two v1 starters.
    assert len(body["suggestions"]) == 1
    assert body["suggestions"][0]["skill"] in ("forecast", "anomaly_detection")
    assert "Profit" in body["suggestions"][0]["text"]
    ml_state.metadata_loader.load_all = AsyncMock(return_value={"columns": "- DimProduct.ProductKey - Type: integer"})
    assert client.get("/api/analysis/suggestions", params={"connection": "sales_db"}).json() == {"suggestions": []}


@pytest.mark.asyncio
async def test_skills_listing_and_prefs(client, ml_state):
    r = client.get("/api/analysis/skills", params={"connection": "sales_db"})
    assert r.status_code == 200
    names = {s["name"]: s for s in r.json()["skills"]}
    assert {"anomaly_detection", "forecast", "clustering", "driver_analysis", "regression", "classification"} <= set(names)
    assert not names["forecast"]["remembered"]
    assert names["clustering"]["tier"] == "B" and names["forecast"]["tier"] == "A"
    assert names["regression"]["tier"] == "B" and names["classification"]["tier"] == "B"

    # Settings can only *forget*: consent is granted from a displayed confirm card,
    # never from an endpoint that shows no egress notice.
    r = client.post("/api/analysis/skills/prefs", json={"connection": "sales_db", "skill": "forecast", "remember": True})
    assert r.status_code == 400 and "confirm card" in r.json()["detail"]
    await ml_state.store.set_skill_pref(user_id="user-a", source_key="sales_db", skill="forecast", remember=True)
    r = client.get("/api/analysis/skills", params={"connection": "sales_db"})
    assert {s["name"]: s["remembered"] for s in r.json()["skills"]}["forecast"] is True
    r = client.post("/api/analysis/skills/prefs", json={"connection": "sales_db", "skill": "forecast", "remember": False})
    assert r.status_code == 200 and r.json()["remembered"] is False
    r = client.get("/api/analysis/skills", params={"connection": "sales_db"})
    assert {s["name"]: s["remembered"] for s in r.json()["skills"]}["forecast"] is False

    r = client.post("/api/analysis/skills/prefs", json={"connection": "sales_db", "skill": "made_up_skill", "remember": False})
    assert r.status_code == 400

    # remember=true on /run is honoured only for a confirm card, not for a guard exit.
    pid = await _proposal(ml_state.store, kind="guard", skill="anomaly_detection")
    assert client.post("/api/analysis/run", json={"connection": "sales_db", "proposal_id": pid, "remember": True}).status_code == 200
    assert not await ml_state.store.has_skill_pref(user_id="user-a", source_key="sales_db", skill="anomaly_detection")


# ── /forecast/accuracy: realized accuracy of a captured forecast ──────────────

_TRACKED_PARAMS = {
    "series": {"table": "FactInternetSales", "schema_name": "dbo", "catalog": None, "date_column": "OrderDate",
               "measure_column": "Profit", "agg": "sum", "grain": "week", "start": "2025-01-06", "end": "2026-06-01",
               "filters": [{"table": "FactInternetSales", "column": "Region", "op": "equals", "value": "EMEA"}],
               "timezone": "UTC", "week_start": "monday"},
    "window": 74, "horizon": 3, "interval": 0.8, "method": "auto",
}


def _forecast_envelope():
    rows = [{"ts": "2026-05-25", "actual": 900.0, "forecast": None, "lower": None, "upper": None, "observed": True, "is_forecast": False}]
    for ts, fc in (("2026-06-01", 1000.0), ("2026-06-08", 1100.0), ("2026-06-15", 1200.0)):
        rows.append({"ts": ts, "actual": None, "forecast": fc, "lower": fc - 100, "upper": fc + 100, "observed": False, "is_forecast": True})
    return {
        "schema_version": "3", "skill": "forecast", "params": _TRACKED_PARAMS, "method_used": "AutoETS",
        "rows": rows, "columns": ["ts", "actual", "forecast", "lower", "upper", "observed", "is_forecast"],
        "validation": {"metric": "WAPE", "value": 0.05, "band": "good", "coverage": 0.8, "coverage_n": 6},
        "engine": {"name": "statsforecast", "version": "2", "module_hash": "abc123def456"},
        "facts": {"skill": "forecast", "grain": "week", "horizon": 3, "interval": 0.8, "measure": "SUM(Profit)",
                  "interval_method": "conformal_scaled", "mase_scale": 50.0, "last_actual": {"ts": "2026-05-25"}},
    }


@pytest.fixture
def tracked_forecast(ml_state):
    """A captured forecast turn owned by user-a on sales_db, plus a source that
    has data through Sunday 2026-06-14 (the weeks of Jun 1 and Jun 8 are
    complete; the week of Jun 15 is pending)."""
    query_id = uuid4()
    ml_state.history_service.get_turn_analysis = AsyncMock(return_value={
        "turn_id": str(query_id), "session_id": uuid4(), "question": "forecast profit",
        "analysis": {"skill": "forecast", "params": _TRACKED_PARAMS, "validation": _forecast_envelope()["validation"]},
        "low_confidence": False,
    })
    ml_state.agent.database_type = "postgres"
    calls = []

    async def run_sql(sql, **kw):
        calls.append(sql)
        if "MIN(" in sql.upper() or "min_ts" in sql:
            return {"columns": ["min_ts", "max_ts", "n"], "rows": [{"min_ts": "2025-01-06", "max_ts": "2026-06-14", "n": 500}]}
        return {"columns": ["ts", "value"], "rows": [{"ts": "2026-06-01", "value": 950.0}, {"ts": "2026-06-08", "value": 1300.0}]}

    ml_state.agent.sql_runner.run_sql = AsyncMock(side_effect=run_sql)
    ml_state.query_id = query_id
    ml_state.sql_calls = calls
    return ml_state


@pytest.mark.asyncio
async def test_accuracy_requires_an_owned_forecast_turn_that_was_captured(client, tracked_forecast):
    st = tracked_forecast
    # Not captured (schema predates the run): 404 with a re-run hint.
    r = client.post("/api/analysis/forecast/accuracy", json={"connection": "sales_db", "query_id": str(st.query_id)})
    assert r.status_code == 404 and "not captured" in r.json()["detail"]
    # Not a forecast turn: 409.
    st.history_service.get_turn_analysis = AsyncMock(return_value={"analysis": {"skill": "anomaly_detection"}})
    r = client.post("/api/analysis/forecast/accuracy", json={"connection": "sales_db", "query_id": str(st.query_id)})
    assert r.status_code == 409
    # Unknown turn / other owner: 404.
    st.history_service.get_turn_analysis = AsyncMock(return_value=None)
    r = client.post("/api/analysis/forecast/accuracy", json={"connection": "sales_db", "query_id": str(uuid4())})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_accuracy_scores_only_elapsed_periods_with_a_fresh_range_and_original_filters(client, tracked_forecast):
    st = tracked_forecast
    assert await st.store.record_forecast(query_id=st.query_id, user_id="user-a", source_key="sales_db", session_id=None,
                                          params=_TRACKED_PARAMS, envelope=_forecast_envelope())
    r = client.post("/api/analysis/forecast/accuracy", json={"connection": "sales_db", "query_id": str(st.query_id)})
    assert r.status_code == 200, r.text
    body = r.json()
    # Source data ends Sunday 2026-06-14: the weeks of Jun 1 and Jun 8 are complete, Jun 15 is pending.
    assert body["data_end"] == "2026-06-14"
    assert body["elapsed_periods"] == 2 and body["pending_periods"] == 1
    assert [p["ts"] for p in body["pending"]] == ["2026-06-15"]
    realized = body["realized"]
    assert realized["metric"] == "WAPE"
    assert realized["value"] == round((50 + 200) / (950 + 1300), 4)
    assert realized["coverage"] == 0.5 and realized["coverage_n"] == 2   # 950 inside 900–1100; 1300 outside 1000–1200
    assert body["claimed"] == {"metric": "WAPE", "value": 0.05, "band": "good", "coverage": 0.8, "coverage_n": 6,
                               "interval_level": 0.8, "method": "AutoETS", "interval_method": "conformal_scaled"}
    assert body["engine_hash"] == "abc123def456"
    # The actuals query is rebuilt: a half-open range over the elapsed weeks only,
    # carrying the ORIGINAL filter value (the envelope's copy would be redacted).
    actual_sql = [s for s in st.sql_calls if "min_ts" not in s and "MIN(" not in s.upper()]
    assert len(actual_sql) == 1 and body["sql"] == actual_sql[0]
    assert "2026-06-01" in actual_sql[0] and "2026-06-15" in actual_sql[0] and "EMEA" in actual_sql[0]
    assert "2026-05-25" not in actual_sql[0]
    # The latest evaluation is kept on the run.
    run = await st.store.get_forecast_run(st.query_id, user_id="user-a", source_key="sales_db")
    assert run["evaluation"]["elapsed_periods"] == 2 and run["evaluated_at"]


@pytest.mark.asyncio
async def test_accuracy_with_nothing_elapsed_runs_no_actuals_query(client, tracked_forecast):
    st = tracked_forecast
    await st.store.record_forecast(query_id=st.query_id, user_id="user-a", source_key="sales_db", session_id=None,
                                   params=_TRACKED_PARAMS, envelope=_forecast_envelope())

    async def run_sql(sql, **kw):
        st.sql_calls.append(sql)
        return {"columns": ["min_ts", "max_ts", "n"], "rows": [{"min_ts": "2025-01-06", "max_ts": "2026-06-03", "n": 500}]}

    st.agent.sql_runner.run_sql = AsyncMock(side_effect=run_sql)
    r = client.post("/api/analysis/forecast/accuracy", json={"connection": "sales_db", "query_id": str(st.query_id)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["elapsed_periods"] == 0 and body["pending_periods"] == 3
    assert body["realized"]["value"] is None and body["sql"] is None
    assert len(st.sql_calls) == 1  # the span probe only
