"""Prometheus metrics: bounded labels, every process instrumented, safe startup."""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from flask import Flask
from prometheus_client import REGISTRY

from src import metrics


def _count(method: str, route: str, status: str) -> float:
    value = REGISTRY.get_sample_value(
        "jeen_http_requests_total", {"method": method, "route": route, "status": status},
    )
    return value or 0.0


def test_asgi_middleware_labels_by_route_template_not_raw_path():
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    app.add_middleware(metrics.PrometheusMiddleware)
    client = TestClient(app)
    before = _count("GET", "/items/{item_id}", "200")
    unmatched = _count("GET", metrics.UNMATCHED_ROUTE, "404")

    client.get("/items/1")
    client.get("/items/2")
    client.get("/nowhere/at/all")

    assert _count("GET", "/items/{item_id}", "200") == before + 2
    assert _count("GET", metrics.UNMATCHED_ROUTE, "404") == unmatched + 1
    assert REGISTRY.get_sample_value(
        "jeen_http_request_duration_seconds_count", {"method": "GET", "route": "/items/{item_id}"},
    ) >= 2


def test_asgi_middleware_counts_an_unhandled_error_as_500():
    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    app.add_middleware(metrics.PrometheusMiddleware)
    before = _count("GET", "/boom", "500")

    TestClient(app, raise_server_exceptions=False).get("/boom")

    assert _count("GET", "/boom", "500") == before + 1


def test_flask_hooks_count_early_returns_and_unhandled_errors():
    app = Flask(__name__)
    metrics.instrument_flask(app)

    @app.before_request
    def guard():
        from flask import request

        if request.path == "/guarded":
            return "no", 401
        return None

    @app.get("/users/<int:user_id>")
    def user(user_id: int):
        return {"id": user_id}

    @app.get("/guarded")
    def guarded():
        return "never"

    @app.get("/broken")
    def broken():
        raise RuntimeError("broken")

    client = app.test_client()
    ok, refused, failed = (
        _count("GET", "/users/<int:user_id>", "200"),
        _count("GET", "/guarded", "401"),
        _count("GET", "/broken", "500"),
    )

    client.get("/users/7")
    client.get("/guarded")
    client.get("/broken")

    assert _count("GET", "/users/<int:user_id>", "200") == ok + 1
    assert _count("GET", "/guarded", "401") == refused + 1
    assert _count("GET", "/broken", "500") == failed + 1


def test_every_service_is_instrumented():
    from src.analytics_service.app import create_app as create_analytics
    from src.api.app_factory import create_app as create_api
    from src.ui_app import app as ui

    async def build_analytics():
        # Its asyncio.Semaphore needs a running loop on Python < 3.10.
        return create_analytics()

    assert any(m.cls is metrics.PrometheusMiddleware for m in create_api().user_middleware)
    assert any(m.cls is metrics.PrometheusMiddleware for m in asyncio.run(build_analytics()).user_middleware)
    assert ui.before_request_funcs[None][0].__name__ == "_metrics_start"


@pytest.mark.parametrize("raw, expected", [("9090", 9090), ("0", 0), ("-3", 0), ("nope", 0)])
def test_metrics_port_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("METRICS_PORT", raw)
    assert metrics.metrics_port() == expected


def test_serve_is_off_at_port_zero_and_survives_a_taken_port(monkeypatch, caplog):
    started = []
    monkeypatch.setattr(metrics, "start_http_server", lambda port, registry: started.append(port))
    assert metrics.serve(0) is False
    assert started == []

    def taken(port, registry):
        raise OSError(48, "Address already in use")

    monkeypatch.setattr(metrics, "start_http_server", taken)
    with caplog.at_level(logging.WARNING, logger="src.metrics"):
        assert metrics.serve(9090) is False
    assert "metrics disabled" in caplog.text


def test_serve_aggregates_workers_in_multiprocess_mode(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(metrics, "start_http_server", lambda port, registry: seen.update(registry=registry))

    assert metrics.serve(9090) is True
    assert seen["registry"] is not REGISTRY


def test_gunicorn_conf_uses_json_logging_and_clears_stale_worker_files(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path / "prom"))
    monkeypatch.setenv("LOG_FORMAT", "json")
    import src.ui_gunicorn_conf as conf

    conf = importlib.reload(conf)
    assert conf.logconfig_dict["handlers"]["default"]["formatter"] == "json"
    assert conf.logconfig_dict["loggers"]["gunicorn.error"] == {"level": "INFO", "handlers": [], "propagate": True}

    (tmp_path / "prom").mkdir()
    stale = tmp_path / "prom" / "counter_123.db"
    stale.write_bytes(b"x")
    conf.on_starting(server=None)
    assert not stale.exists()


def test_analytics_launcher_keeps_the_json_logging(monkeypatch):
    from src.analytics_service import __main__ as launcher

    calls = {}
    monkeypatch.setattr(launcher, "configure_logging", lambda: calls.setdefault("logging", True))
    monkeypatch.setattr(launcher.metrics, "serve", lambda: calls.setdefault("metrics", True))
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *a, **k: calls.setdefault("run", k))

    launcher.main()

    assert calls["logging"] and calls["metrics"]
    assert calls["run"]["log_config"] is None
