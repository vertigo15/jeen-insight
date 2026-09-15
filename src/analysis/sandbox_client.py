"""``HttpSandboxRunner`` — the production ``AnalysisRunner``.

Posts ``{skill, params, series}`` to the ``jeen-insights-analytics`` service
over the internal network with a short-lived HMAC token bound to that
service's audience, and maps the JSON outcome back into :class:`RunOutcome`.
A small circuit breaker keeps a dead sandbox from turning every question into
a 30-second wait.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx

from src.analysis.contracts import CONTRACT_VERSION
from src.analysis.runner import RunOutcome
from src.security.internal_auth import issue_internal_token

logger = logging.getLogger(__name__)

DEFAULT_AUDIENCE = "jeen-insights-analytics"
BREAKER_FAILURES = 3
BREAKER_OPEN_SECONDS = 30.0


class HttpSandboxRunner:
    name = "sandbox"

    def __init__(
        self,
        base_url: str,
        *,
        audience: str = DEFAULT_AUDIENCE,
        timeout_seconds: float = 45.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self.timeout_seconds = float(timeout_seconds)
        self._client = client
        self._failures = 0
        self._open_until = 0.0

    @classmethod
    def from_settings(cls, settings: Any) -> "HttpSandboxRunner":
        return cls(
            str(getattr(settings, "ANALYSIS_SANDBOX_URL", "http://jeen-insights-analytics:8100")),
            audience=str(getattr(settings, "ANALYSIS_SANDBOX_AUDIENCE", DEFAULT_AUDIENCE) or DEFAULT_AUDIENCE),
            # The sandbox kills at ANALYSIS_TIMEOUT_SECONDS; give the HTTP call headroom.
            timeout_seconds=float(getattr(settings, "ANALYSIS_TIMEOUT_SECONDS", 30) or 30) + 15.0,
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._client

    def _token(self, user_id: str) -> str:
        return issue_internal_token({"user_id": user_id or "api", "role": "service", "auth_provider": "internal"},
                                    audience=self.audience)

    def _breaker_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _record(self, ok: bool) -> None:
        if ok:
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= BREAKER_FAILURES:
            self._open_until = time.monotonic() + BREAKER_OPEN_SECONDS
            logger.error("sandbox: %d consecutive failures — pausing calls for %.0fs", self._failures, BREAKER_OPEN_SECONDS)

    async def run(self, skill, params, series, *, override_guards=False, context=None) -> RunOutcome:
        if self._breaker_open():
            return RunOutcome(status="error", error="The analysis service is temporarily unavailable. Try again in a moment.")
        ctx = dict(context or {})
        user_id = str(ctx.pop("user_id", "") or "")
        body = {
            "skill": skill, "params": params, "series": series,
            "override_guards": bool(override_guards), "context": ctx,
            "contract_version": CONTRACT_VERSION,
        }
        try:
            response = await self._http().post(
                "/run", json=body, headers={"Authorization": f"Bearer {self._token(user_id)}"},
            )
        except httpx.TimeoutException:
            self._record(False)
            return RunOutcome(status="error", error=f"analysis timed out after {int(self.timeout_seconds)}s")
        except httpx.HTTPError as exc:
            self._record(False)
            return RunOutcome(status="error", error=f"analysis service unreachable: {type(exc).__name__}")
        if response.status_code >= 500:
            self._record(False)
            return RunOutcome(status="error", error=f"analysis service error ({response.status_code})")
        self._record(True)
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = response.text[:200]
            return RunOutcome(status="error", error=f"analysis service refused the run: {detail}")
        try:
            return RunOutcome.from_json(response.json())
        except Exception as exc:  # noqa: BLE001
            return RunOutcome(status="error", error=f"analysis service returned an unreadable outcome: {exc}")

    async def health(self) -> Dict[str, Any]:
        response = await self._http().get("/health")
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
