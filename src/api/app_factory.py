"""FastAPI app factory.

Centralises construction so tests can build a fresh app per test session
without colliding on module-level state. Production callers just import
`app` from `src.api`.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.lifespan import lifespan
from src.api.routes import (
    autocomplete,
    charts,
    connections,
    health,
    history,
    insights,
    mcp as mcp_routes,
    query,
    runtime_settings as runtime_settings_routes,
    settings as settings_routes,
)
from src.config import settings


def create_app() -> FastAPI:
    """Build the FastAPI app with all routers and middleware attached."""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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

    # Routers — order doesn't matter, but grouping mirrors the file layout.
    app.include_router(health.router)
    app.include_router(connections.router)
    app.include_router(query.router)
    app.include_router(history.router)
    app.include_router(autocomplete.router)
    app.include_router(insights.router)
    app.include_router(charts.router)
    app.include_router(settings_routes.router)
    app.include_router(runtime_settings_routes.router)
    app.include_router(mcp_routes.router)

    return app
