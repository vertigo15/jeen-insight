"""Internal-auth boundary for FastAPI (default-deny).

Every request except a small exempt set (health, docs) must carry a valid
internal token minted by Flask. The verified :class:`Principal` is attached to
``request.state.principal`` for downstream dependencies. Identity, role and
group facts are taken ONLY from the token — never from the request body/query.

Enforcement is active whenever ``INTERNAL_AUTH_ENABLED`` is true (the default).
When a request lacks a valid token the middleware returns 401 immediately.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.logging_config import (
    REQUEST_ID_HEADER,
    bind_request_id,
    get_request_id,
    reset_request_id,
)
from src.security.internal_auth import (
    AUDIENCE_API,
    InternalAuthConfigError,
    PrincipalError,
    is_enforced,
    verify_internal_token,
)

logger = logging.getLogger(__name__)

# Paths that never require an internal token.
_EXEMPT_EXACT = {"/health", "/", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}
_EXEMPT_PREFIXES = ("/docs", "/redoc", "/static")


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-internal-token") or "").strip()


class InternalAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.principal = None

        if _is_exempt(request.url.path):
            return await call_next(request)

        token = _extract_token(request)
        if token:
            try:
                request.state.principal = verify_internal_token(
                    token, audience=AUDIENCE_API
                )
            except InternalAuthConfigError as exc:
                # Server misconfiguration (weak/missing signing secret). Fail
                # closed with a 503 rather than trusting the request.
                logger.error("internal-auth: misconfigured signing secret: %s", exc)
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Internal auth is misconfigured", "code": "MISCONFIGURED"},
                )
            except PrincipalError as exc:
                if is_enforced():
                    return JSONResponse(
                        status_code=401,
                        content={"detail": f"Invalid internal token: {exc}", "code": "UNAUTHENTICATED"},
                    )
                logger.warning("internal-auth: token rejected but enforcement off (%s)", exc)

        if request.state.principal is None and is_enforced():
            return JSONResponse(
                status_code=401,
                content={"detail": "Authenticated internal token required", "code": "UNAUTHENTICATED"},
            )

        return await call_next(request)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a per-request correlation id for the lifetime of the request.

    Reuses an inbound ``X-Request-ID`` (minted by the Flask UI) when present so a
    single id ties the UI and API log lines together; otherwise it generates
    one. The id is echoed back on the response and stamped onto every log record
    via :class:`src.logging_config.RequestIdFilter`.

    Registered as the outermost middleware so the id is already bound before the
    auth boundary runs and its own log lines carry it too.
    """

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        token = bind_request_id(incoming)
        request_id = get_request_id() or ""
        # Expose on request.state so exception handlers / dependencies can read
        # it even after the contextvar is reset below.
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
