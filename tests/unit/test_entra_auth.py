"""Unit tests for Microsoft Entra ID OAuth helpers."""

from __future__ import annotations

from src import entra_auth


def test_is_configured_false_when_env_missing(monkeypatch):
    monkeypatch.delenv("AZURE_AD_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_AD_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AZURE_AD_TENANT_ID", raising=False)
    assert entra_auth.is_configured() is False


def test_is_configured_true_when_env_present(monkeypatch):
    monkeypatch.setenv("AZURE_AD_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", "secret")
    monkeypatch.setenv("AZURE_AD_TENANT_ID", "tenant-id")
    assert entra_auth.is_configured() is True


def test_redirect_uri_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("AZURE_AD_REDIRECT_URI", "https://app.example/auth/microsoft/callback")
    assert entra_auth.redirect_uri("http://localhost:8501") == (
        "https://app.example/auth/microsoft/callback"
    )


def test_redirect_uri_derives_from_base_url(monkeypatch):
    monkeypatch.delenv("AZURE_AD_REDIRECT_URI", raising=False)
    assert entra_auth.redirect_uri("http://localhost:8501") == (
        "http://localhost:8501/auth/microsoft/callback"
    )


def test_profile_from_token_result():
    result = {
        "id_token_claims": {
            "preferred_username": "User@Contoso.com",
            "name": "Contoso User",
        }
    }
    profile = entra_auth.profile_from_token_result(result)
    assert profile["email"] == "user@contoso.com"
    assert profile["name"] == "Contoso User"
