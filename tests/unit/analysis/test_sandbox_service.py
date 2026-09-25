"""jeen-insights-analytics: same envelope as in-process, hard limits, token auth,
and an import graph free of database / LLM / agent code."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.analysis.contracts import CONTRACT_VERSION
from src.analysis.runner import RunOutcome, execute_skill
from src.analysis.sandbox_client import BREAKER_FAILURES, HttpSandboxRunner
from src.analytics_service.app import create_app
from src.analytics_service.executor import ExecutorConfig, ForkExecutor
from src.security.internal_auth import issue_internal_token
from tests.unit.analysis.synthetic import seasonal_series, series_request, to_payload

pytestmark = pytest.mark.filterwarnings("ignore")

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


# Module-level so a spawned child can import them by path.
def fake_execute(skill, params, series, *, override_guards=False, context=None):
    return RunOutcome(status="guard_failed", guard_results=[], error=None)


def sleepy_execute(skill, params, series, *, override_guards=False, context=None):
    time.sleep(5)
    return RunOutcome(status="ok")


def crashy_execute(skill, params, series, *, override_guards=False, context=None):
    os._exit(3)


def echo_context_execute(skill, params, series, *, override_guards=False, context=None):
    return RunOutcome(status="error", error="|".join(f"{k}={v}" for k, v in sorted((context or {}).items())))


def _token(audience="jeen-insights-analytics"):
    return issue_internal_token({"user_id": "user-a", "role": "service"}, audience=audience)


def _payload(n=30):
    idx, y = seasonal_series(n=n, grain="week", amplitude=0)
    return {"skill": "anomaly_detection", "params": {"series": series_request()}, "series": to_payload(idx, y),
            "contract_version": CONTRACT_VERSION, "context": {"sql": "SELECT 1", "user_id": "leak?"}}


@pytest.fixture(scope="module")
def fast_app():
    """Service wired to a trivial executor target so auth/cap tests stay quick."""
    return create_app(executor=ForkExecutor(ExecutorConfig(timeout_seconds=10), target=fake_execute))


def test_run_requires_a_token_with_the_sandbox_audience(fast_app):
    client = TestClient(fast_app)
    assert client.post("/run", json=_payload()).status_code == 401
    wrong = client.post("/run", json=_payload(), headers={"Authorization": f"Bearer {_token('jeen-insights-api')}"})
    assert wrong.status_code == 401 and "audience" in wrong.json()["detail"]
    ok = client.post("/run", json=_payload(), headers={"Authorization": f"Bearer {_token()}"})
    assert ok.status_code == 200 and ok.json()["status"] == "guard_failed"


def test_sandbox_audience_has_its_own_signing_key(fast_app, monkeypatch):
    """With INTERNAL_ANALYTICS_SECRET set, API-secret material cannot produce a
    token the sandbox accepts, and the sandbox key cannot sign an API token."""
    from src.security.internal_auth import PrincipalError, verify_internal_token

    monkeypatch.setenv("INTERNAL_API_SECRET", "api-key-" + "a" * 48)
    monkeypatch.setenv("INTERNAL_ANALYTICS_SECRET", "sandbox-key-" + "b" * 48)
    client = TestClient(fast_app)
    sandbox_token = _token()  # signed with the analytics key
    assert client.post("/run", json=_payload(), headers={"Authorization": f"Bearer {sandbox_token}"}).status_code == 200
    # A token signed with the API key (audience forged to the sandbox) is rejected by the sandbox…
    monkeypatch.setenv("INTERNAL_ANALYTICS_SECRET", "api-key-" + "a" * 48)
    forged = _token()
    monkeypatch.setenv("INTERNAL_ANALYTICS_SECRET", "sandbox-key-" + "b" * 48)
    assert client.post("/run", json=_payload(), headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    # …and the sandbox key cannot sign anything the API would accept.
    with pytest.raises(PrincipalError):
        verify_internal_token(sandbox_token, audience="jeen-insights-api")
    monkeypatch.setenv("INTERNAL_API_SECRET", "sandbox-key-" + "b" * 48)  # even if someone tried the same bytes as API key…
    with pytest.raises(PrincipalError):  # …the audience still does not match
        verify_internal_token(sandbox_token, audience="jeen-insights-api")


def test_forecast_window_is_accepted_by_the_engine_entry_point():
    """Contract 2 gave ForecastParams a window; the extra-forbid model the sandbox
    validates with must take it (the version check protects an older image)."""
    assert CONTRACT_VERSION == "3"
    idx, y = seasonal_series(n=30, grain="week", amplitude=0)
    out = execute_skill("forecast", {"series": series_request(), "window": 36, "horizon": 4}, to_payload(idx, y))
    assert out.status == "ok", out.error
    assert out.envelope.params["window"] == 36
    # A payload from the previous contract is refused at the door, before any parsing.
    app = create_app(executor=ForkExecutor(ExecutorConfig(timeout_seconds=10), target=fake_execute))
    stale = dict(_payload(), contract_version="1")
    assert TestClient(app).post("/run", json=stale, headers={"Authorization": f"Bearer {_token()}"}).status_code == 409


def test_run_forwards_only_the_allowlisted_context_keys():
    """``data_end`` (the span probe's newest timestamp) reaches the engine so it
    can prove a trailing period incomplete; identity and unknown keys do not."""
    app = create_app(executor=ForkExecutor(ExecutorConfig(timeout_seconds=10), target=echo_context_execute))
    payload = _payload()
    payload["context"] = {"sql": "SELECT 1", "data_end": "2008-07-16 00:00:00", "user_id": "leak?", "code": "print(1)"}
    body = TestClient(app).post("/run", json=payload, headers={"Authorization": f"Bearer {_token()}"}).json()
    assert body["error"] == "data_end=2008-07-16 00:00:00|runner=sandbox|sql=SELECT 1"


def test_health_reports_engines_and_limits(fast_app):
    body = TestClient(fast_app).get("/health").json()
    assert body["status"] == "ok" and {"anomaly_detection", "forecast", "clustering", "driver_analysis", "regression", "classification", "cohort_retention", "experiment_test"} <= set(body["skills"])
    assert "statsforecast" in body["engines"] and body["limits"]["timeout_seconds"] > 0


def test_caps_and_contract_checks(fast_app, monkeypatch):
    from src.analytics_service import app as app_module

    client = TestClient(fast_app)
    headers = {"Authorization": f"Bearer {_token()}"}
    bad_version = dict(_payload(), contract_version="99")
    assert client.post("/run", json=bad_version, headers=headers).status_code == 409
    unknown = dict(_payload(), skill="made_up_skill")
    assert client.post("/run", json=unknown, headers=headers).status_code == 400
    monkeypatch.setattr(app_module, "MAX_SERIES_ROWS", 10)
    assert client.post("/run", json=_payload(30), headers=headers).status_code == 413
    # Tier B has its own, larger cap: the series cap does not apply to entity skills.
    monkeypatch.setattr(app_module, "MAX_ENTITY_ROWS", 5)
    entity = {"skill": "clustering", "params": {"entity": {"table": "t", "entity_key": "k", "features": ["a", "b"]}},
              "series": {"columns": ["k", "a", "b"], "rows": [{"k": i, "a": i, "b": i} for i in range(6)]},
              "contract_version": CONTRACT_VERSION}
    assert client.post("/run", json=entity, headers=headers).status_code == 413
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 100)
    assert client.post("/run", json=_payload(30), headers=headers).status_code == 413
    # The cap counts bytes actually received, so a chunked body (no Content-Length) is refused too.
    import json as _json

    big = _json.dumps(_payload(30)).encode()

    def chunks():
        for i in range(0, len(big), 64):
            yield big[i:i + 64]

    r = client.post("/run", content=chunks(), headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 413
    # Unknown top-level fields are rejected (no free-form payload reaches the engine).
    extra = dict(_payload(), code="print(1)")
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(app_module, "MAX_SERIES_ROWS", 1500)
    assert client.post("/run", json=extra, headers=headers).status_code == 422


def test_executor_kills_on_timeout_and_survives_a_crash():
    slow = ForkExecutor(ExecutorConfig(timeout_seconds=1), target=sleepy_execute)
    out = slow.run_sync("anomaly_detection", {}, {"rows": []})
    assert out["status"] == "error" and "timed out" in out["error"]
    crash = ForkExecutor(ExecutorConfig(timeout_seconds=10), target=crashy_execute)
    out = crash.run_sync("anomaly_detection", {}, {"rows": []})
    assert out["status"] == "error" and "exited 3" in out["error"]


def _entity_payload():
    import numpy as np

    rng = np.random.default_rng(3)
    n = 300
    income = rng.normal(60_000, 15_000, n) + rng.integers(0, 3, n) * 40_000
    kids = rng.integers(0, 5, n)
    rows = [{"entity_key": i, "YearlyIncome": float(income[i]), "TotalChildren": int(kids[i])} for i in range(n)]
    return {
        "skill": "clustering",
        "params": {"entity": {"table": "DimCustomer", "entity_key": "CustomerKey", "features": ["YearlyIncome", "TotalChildren"]}, "k": 3},
        "series": {"columns": ["entity_key", "YearlyIncome", "TotalChildren"], "rows": rows},
        "contract_version": CONTRACT_VERSION, "context": {"sql": "SELECT 1"},
    }


@pytest.mark.slow
@pytest.mark.parametrize("payload_factory", [lambda: _payload(30), _entity_payload], ids=["series", "entity"])
def test_sandbox_envelope_matches_in_process(payload_factory):
    app = create_app(executor=ForkExecutor(ExecutorConfig(timeout_seconds=120)))
    client = TestClient(app)
    payload = payload_factory()
    response = client.post("/run", json=payload, headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200, response.text
    outcome = RunOutcome.from_json(response.json())
    assert outcome.ok, outcome.error
    direct = execute_skill(payload["skill"], payload["params"], payload["series"])
    assert outcome.envelope.rows == direct.envelope.rows
    assert outcome.envelope.engine.runner == "sandbox"
    assert outcome.envelope.provenance.sql == "SELECT 1"
    assert outcome.envelope.egress.tier == ("B" if payload["skill"] == "clustering" else "A")


@pytest.mark.slow
def test_http_sandbox_runner_round_trip_and_breaker():
    app = create_app(executor=ForkExecutor(ExecutorConfig(timeout_seconds=120)))
    transport = httpx.ASGITransport(app=app)

    async def _go():
        client = httpx.AsyncClient(transport=transport, base_url="http://sandbox", timeout=120)
        runner = HttpSandboxRunner("http://sandbox", client=client)
        payload = _payload(30)
        out = await runner.run("anomaly_detection", payload["params"], payload["series"],
                               context={"sql": "SELECT 1", "user_id": "user-a"})
        assert out.ok, out.error
        assert out.envelope.engine.runner == "sandbox"
        # A wrong audience is refused by the service and surfaced as a plain error.
        wrong = HttpSandboxRunner("http://sandbox", audience="jeen-insights-api", client=client)
        refused = await wrong.run("anomaly_detection", payload["params"], payload["series"])
        assert refused.status == "error" and "refused" in refused.error
        # Consecutive transport failures open the breaker.
        dead = HttpSandboxRunner("http://127.0.0.1:9", timeout_seconds=0.2)
        for _ in range(BREAKER_FAILURES):
            res = await dead.run("anomaly_detection", payload["params"], payload["series"])
            assert res.status == "error"
        opened = await dead.run("anomaly_detection", payload["params"], payload["series"])
        assert "temporarily unavailable" in opened.error
        await dead.aclose()
        await client.aclose()

    asyncio.run(_go())


def test_sandbox_import_graph_has_no_db_llm_or_agent_modules():
    """What the image ships must not be able to reach a database or a model."""
    code = (
        "import sys; import src.analytics_service.app; "
        "bad = sorted(m for m in sys.modules if m.startswith(("
        "'src.connectors', 'src.metadata', 'src.agent', 'src.api', 'src.analysis.sql_builder', "
        "'src.analysis.sandbox_client', 'asyncpg', 'psycopg', 'langchain', 'openai', 'sqlglot'))); "
        "print('|'.join(bad))"
    )
    env = {**os.environ, "PYTHONPATH": _REPO, "ANALYSIS_AUTH_ENABLED": "true"}
    proc = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert proc.stdout.strip() == "", f"sandbox imports forbidden modules: {proc.stdout.strip()}"
