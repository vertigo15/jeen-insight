"""Health + root information endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from src.api import state

router = APIRouter(tags=["health"])


@router.get("/")
async def root():
    return {
        "service": "Jeen Insights",
        "version": "2.0.0",
        "status": "running",
    }


@router.get("/health")
async def health_check():
    # Public, unauthenticated endpoint — report only coarse status. Do NOT leak
    # infra identifiers (DB host/name, deployment names, connection strings).
    return {
        "status": "healthy",
        "registry_ready": state.agent_registry is not None,
        "services": {
            "llm": "configured",
            "metadata_db": "configured",
        },
    }
