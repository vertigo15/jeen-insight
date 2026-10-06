"""Prometheus metrics shared by the API, the UI and the analytics sandbox.

Every process serves its metrics on ``METRICS_PORT`` (default 9090, ``0`` turns
it off). That port is a container port only: no Kubernetes Service exposes it,
so the metrics are reachable by a scraper on the pod network but never through
the UI's public address.

The UI runs under gunicorn. Its workers write samples to
``PROMETHEUS_MULTIPROC_DIR`` and the gunicorn master serves the aggregate, so
the counters survive worker recycling (see ``src/ui_gunicorn_conf.py``).

Labels are bounded on purpose: ``route`` is the route template (``/api/conversations/{id}``),
never the raw path, and requests that match no route share ``unmatched``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    multiprocess,
    start_http_server,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9090
UNMATCHED_ROUTE = "unmatched"

_multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
if _multiproc_dir:
    os.makedirs(_multiproc_dir, exist_ok=True)

HTTP_REQUESTS = Counter(
    "jeen_http_requests_total",
    "HTTP requests handled, by method, route template and status code.",
    ["method", "route", "status"],
)
# Answers stream for minutes, so the buckets reach well past the usual web range.
HTTP_DURATION = Histogram(
    "jeen_http_request_duration_seconds",
    "Seconds from request start until the response finished (until it started, for Flask streams).",
    ["method", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)


def metrics_port() -> int:
    raw = os.getenv("METRICS_PORT", str(DEFAULT_PORT)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def observe(method: str, route: Optional[str], status: int, seconds: Optional[float]) -> None:
    route = route or UNMATCHED_ROUTE
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    if seconds is not None:
        HTTP_DURATION.labels(method=method, route=route).observe(seconds)


def serve(port: Optional[int] = None) -> bool:
    """Start the metrics HTTP server in a daemon thread. Returns False when disabled."""
    port = metrics_port() if port is None else port
    if port <= 0:
        return False
    registry = REGISTRY
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    try:
        start_http_server(port, registry=registry)
    except OSError as exc:
        # Port taken, e.g. by uvicorn's reload child in local development.
        # Metrics are diagnostic: never let them stop the service from starting.
        logger.warning("metrics: cannot serve on port %s (%s); metrics disabled", port, exc)
        return False
    return True


class PrometheusMiddleware:
    """ASGI middleware that records one sample per HTTP request.

    Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses pass through
    untouched and the duration covers the whole stream.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None)
            observe(scope.get("method", "GET"), route, status, time.perf_counter() - started)


def instrument_flask(app: Any) -> None:
    """Record one sample per Flask request.

    Call before registering any other ``before_request`` hook: Flask skips the
    remaining hooks once one returns a response, and the start time must be
    taken first.
    """
    from flask import g, request

    @app.before_request
    def _metrics_start() -> None:
        g._metrics_started = time.perf_counter()

    @app.after_request
    def _metrics_record(response: Any) -> Any:
        started = g.pop("_metrics_started", None)
        route = request.url_rule.rule if request.url_rule is not None else None
        observe(request.method, route, response.status_code,
                time.perf_counter() - started if started is not None else None)
        g._metrics_recorded = True
        return response

    @app.teardown_request
    def _metrics_unhandled(exc: Optional[BaseException]) -> None:
        if exc is None or g.pop("_metrics_recorded", False):
            return
        started = g.pop("_metrics_started", None)
        route = request.url_rule.rule if request.url_rule is not None else None
        observe(request.method, route, 500, time.perf_counter() - started if started is not None else None)
