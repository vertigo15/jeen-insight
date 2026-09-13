"""FastAPI app factory.

Centralises construction so tests can build a fresh app per test session
without colliding on module-level state. Production callers just import
`app` from `src.api`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.lifespan import lifespan
from src.api.middleware import InternalAuthMiddleware, RequestContextMiddleware
from src.api.routes import (
    actions as actions_routes,
    autocomplete,
    charts,
    connections,
    connectors as connectors_routes,
    health,
    history,
    insights,
    mcp as mcp_routes,
    me_connections,
    onboarding,
    query,
    runtime_settings as runtime_settings_routes,
    saved_analyses,
    settings as settings_routes,
)
from src.config import settings
from src.logging_config import configure_logging


def create_app() -> FastAPI:
    """Build the FastAPI app with all routers and middleware attached."""
    configure_logging(
        level=settings.LOG_LEVEL,
        fmt=settings.LOG_FORMAT,
        dev_mode=settings.JEEN_DEV_MODE,
    )

    app = FastAPI(
        title="Jeen Insights",
        description=(
            "Natural-language analytics over registered data connections, powered by "
            "Azure OpenAI and curated metadata from the shared Jeen metadata DB."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Internal auth boundary: verify the Flask-minted token into a Principal and
    # default-deny non-exempt routes.
    app.add_middleware(InternalAuthMiddleware)

    # Correlation id: added last so it runs first (outermost), binding the
    # request id before the auth boundary logs anything.
    app.add_middleware(RequestContextMiddleware)

    # Routers — order doesn't matter, but grouping mirrors the file layout.
    app.include_router(health.router)
    app.include_router(connections.router)
    app.include_router(query.router)
    app.include_router(history.router)
    app.include_router(onboarding.router)
    app.include_router(autocomplete.router)
    app.include_router(insights.router)
    app.include_router(charts.router)
    app.include_router(settings_routes.router)
    app.include_router(runtime_settings_routes.router)
    app.include_router(mcp_routes.router)
    app.include_router(saved_analyses.router)
    # Connector / integration platform.
    app.include_router(connectors_routes.router)
    app.include_router(me_connections.router)
    app.include_router(actions_routes.router)

    return app
