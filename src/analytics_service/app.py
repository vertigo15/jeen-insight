"""FastAPI app for the ML-skills sandbox (``jeen-insights-analytics``).

Endpoints
---------
GET  /health   liveness + engine versions
POST /run      ``{skill, params, series, override_guards, context, contract_version}``
               → ``RunOutcome.to_json()``

Every ``/run`` executes :func:`src.analysis.runner.execute_skill` in a child
process (see :mod:`src.analytics_service.executor`) so a runaway fit is killed
by the kernel and the wall clock, never by good intentions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.analysis.contracts import CONTRACT_VERSION, SKILLS
from src.analytics_service.executor import ExecutorConfig, ForkExecutor
from src.security.internal_auth import PrincipalError, verify_internal_token

logger = logging.getLogger(__name__)

AUDIENCE = os.getenv("ANALYSIS_SANDBOX_AUDIENCE", "jeen-insights-analytics")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# 50k entity rows × 8 numeric features is ~8 MB of JSON; leave headroom.
MAX_BODY_BYTES = _env_int("ANALYSIS_MAX_BODY_BYTES", 24 * 1024 * 1024)
MAX_SERIES_ROWS = _env_int("ANALYSIS_MAX_SERIES_ROWS", 1500)
MAX_ENTITY_ROWS = _env_int("ANALYSIS_MAX_ENTITY_ROWS", 50_000)
MAX_CONCURRENT = _env_int("ANALYSIS_MAX_CONCURRENT", 2)


class _BodyTooLarge(HTTPException):
    """Raised from the receive channel. Subclassing HTTPException matters:
    FastAPI turns any other exception raised while reading a body into a
    generic 400, whereas an HTTPException is re-raised and rendered as-is."""

    def __init__(self, max_bytes: int):
        super().__init__(status_code=413, detail=f"payload too large (>{max_bytes} bytes)")


class BodyLimitMiddleware:
    """Pure-ASGI request body cap that counts the bytes actually received.

    A Content-Length check alone is bypassed by chunked or length-less
    requests; here every ``http.request`` chunk is tallied and the connection
    is answered with 413 the moment the cap is crossed, before the JSON is
    ever parsed.
    """

    def __init__(self, app, max_bytes: Optional[int] = None):
        self.app = app
        self._max_bytes = int(max_bytes) if max_bytes is not None else None

    @property
    def max_bytes(self) -> int:
        # Read at request time so the env-configured module value governs.
        return self._max_bytes if self._max_bytes is not None else MAX_BODY_BYTES

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap = self.max_bytes
        declared = next((v for k, v in scope.get("headers") or [] if k == b"content-length"), b"")
        if declared.isdigit() and int(declared) > cap:
            return await self._reject(send)
        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > cap:
                    raise _BodyTooLarge(cap)
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await self._reject(send)

    async def _reject(self, send):
        body = (f'{{"detail":"payload too large (>{self.max_bytes} bytes)"}}').encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
TIMEOUT_SECONDS = _env_int("ANALYSIS_TIMEOUT_SECONDS", 30)
MEMORY_MB = _env_int("ANALYSIS_MEMORY_MB", 1024)
AUTH_ENABLED = (os.getenv("ANALYSIS_AUTH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on"))


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    params: Dict[str, Any]
    series: Dict[str, Any]
    override_guards: bool = False
    context: Dict[str, Any] = Field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION


def create_app(executor: Optional[ForkExecutor] = None) -> FastAPI:
    app = FastAPI(title="jeen-insights-analytics", version=CONTRACT_VERSION, docs_url=None, redoc_url=None)
    app.state.executor = executor or ForkExecutor(ExecutorConfig(timeout_seconds=TIMEOUT_SECONDS, memory_mb=MEMORY_MB))
    app.state.semaphore = asyncio.Semaphore(max(1, MAX_CONCURRENT))
    app.add_middleware(BodyLimitMiddleware)

    def _auth(request: Request) -> None:
        if not AUTH_ENABLED:
            return
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        try:
            verify_internal_token(token, audience=AUDIENCE)
        except PrincipalError as exc:
            raise HTTPException(status_code=401, detail=str(exc))

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "contract_version": CONTRACT_VERSION,
            "skills": sorted(SKILLS),
            "engines": app.state.executor.engine_versions(),
            "limits": {"timeout_seconds": TIMEOUT_SECONDS, "memory_mb": MEMORY_MB,
                       "max_series_rows": MAX_SERIES_ROWS, "max_entity_rows": MAX_ENTITY_ROWS,
                       "max_body_bytes": MAX_BODY_BYTES},
        }

    @app.post("/run")
    async def run(body: RunRequest, request: Request, _: None = Depends(_auth)):
        if body.contract_version != CONTRACT_VERSION:
            raise HTTPException(status_code=409, detail=f"contract version {body.contract_version!r} != {CONTRACT_VERSION!r}")
        if body.skill not in SKILLS:
            raise HTTPException(status_code=400, detail=f"unknown skill {body.skill!r}")
        rows = body.series.get("rows") or []
        # Tier A sends aggregates, tier B row-level data; each has its own cap.
        cap = MAX_ENTITY_ROWS if SKILLS[body.skill].family == "entity" else MAX_SERIES_ROWS
        if len(rows) > cap:
            raise HTTPException(status_code=413, detail=f"input too large: {len(rows)} rows (max {cap} for {body.skill})")
        context = {k: v for k, v in body.context.items()
                   if k in ("sql", "query_ts", "filters_summary", "low_confidence", "data_end")}
        context["runner"] = "sandbox"
        t0 = time.monotonic()
        async with app.state.semaphore:
            outcome = await app.state.executor.run(
                body.skill, body.params, body.series,
                override_guards=body.override_guards, context=context,
            )
        outcome["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info("run skill=%s status=%s rows=%d elapsed_ms=%d", body.skill, outcome.get("status"), len(rows), outcome["elapsed_ms"])
        return outcome

    return app


app = create_app()
