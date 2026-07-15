"""Tests for `src.api.routes.health`."""

from __future__ import annotations


def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"service": "Jeen Insights", "version": "2.0.0", "status": "running"}


def test_health_when_registry_ready(client, fake_state):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["registry_ready"] is True
    assert "llm" in body["services"]
    assert "metadata_db" in body["services"]


def test_health_does_not_leak_infra_identifiers(client, fake_state):
    """/health is public — it must not expose DB host/name or deployment names."""
    from src.config import settings

    resp = client.get("/health")
    body = resp.json()
    services = body["services"]
    # Values must be coarse status strings, not host/db/deployment identifiers.
    assert services["metadata_db"] == "configured"
    assert services["llm"] == "configured"
    blob = str(body)
    assert settings.METADATA_DB_HOST not in blob
    assert settings.METADATA_DB_NAME not in blob
    assert settings.AZURE_OPENAI_DEPLOYMENT_NAME not in blob


def test_health_when_registry_missing(client, empty_state):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # `/health` is intentionally still 200 even when the registry isn't ready,
    # so probes can distinguish "service alive but not ready" from "down".
    assert body["status"] == "healthy"
    assert body["registry_ready"] is False
