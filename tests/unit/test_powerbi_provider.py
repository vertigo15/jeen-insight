"""Unit tests for the PowerBiAdapter OAuth provider + catalog entry."""

from __future__ import annotations

import time

import pytest

from src.connectors.catalog import CATALOG, get_catalog_entry
from src.connectors.providers import get_provider
from src.connectors.providers.base import TokenResult

_CONFIG = {"tenant_id": "tid-123", "client_id": "client-abc"}
_MANIFEST = {
    "scopes": [
        "openid",
        "profile",
        "email",
        "offline_access",
        "https://analysis.windows.net/powerbi/api/Dataset.Read.All",
    ]
}


def _adapter():
    return get_provider("power_bi")


class TestRegistration:
    def test_registered_as_oauth(self):
        p = _adapter()
        assert p is not None
        assert p.auth_kind == "oauth"
        assert p.allowed_origins == ("https://api.powerbi.com",)

    def test_catalog_entry(self):
        entry = get_catalog_entry("power-bi")
        assert entry is not None
        assert "power-bi" in CATALOG
        assert entry.provider == "power_bi"
        assert entry.category == "analytics"
        assert entry.actions == []  # read-only data source
        assert "offline_access" in entry.scopes
        assert any("Dataset.Read.All" in s for s in entry.scopes)


class TestAuthorizeUrl:
    def test_builds_entra_authorize_url(self):
        url = _adapter().authorize_url(
            config=_CONFIG, manifest=_MANIFEST, redirect_uri="https://app/cb",
            state="st-1", code_challenge="cc-1", nonce="nonce-1",
        )
        assert "login.microsoftonline.com/tid-123/oauth2/v2.0/authorize" in url
        assert "client_id=client-abc" in url
        assert "state=st-1" in url

    def test_requires_tenant(self, monkeypatch):
        monkeypatch.delenv("CONNECTORS_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_AD_TENANT_ID", raising=False)
        with pytest.raises(ValueError):
            _adapter().authorize_url(
                config={"client_id": "c"}, manifest=_MANIFEST,
                redirect_uri="https://app/cb", state="s", code_challenge="c", nonce="n",
            )


class TestValidateAndBind:
    def _token(self, **claim_overrides):
        claims = {
            "aud": "client-abc",
            "tid": "tid-123",
            "iss": "https://login.microsoftonline.com/tid-123/v2.0",
            "nonce": "nonce-1",
            "exp": time.time() + 3600,
            "oid": "oid-999",
            "preferred_username": "u@contoso.com",
        }
        claims.update(claim_overrides)
        return TokenResult(
            access_token="at", refresh_token="rt", expires_in=3600,
            scope="s", id_token="header.payload.sig", claims=claims,
        )

    def test_valid_token_binds(self):
        bound = _adapter().validate_and_bind(
            self._token(), config=_CONFIG, expected_nonce="nonce-1",
            expected_tenant="tid-123", expected_object_id="oid-999",
        )
        assert bound["tenant_id"] == "tid-123"
        assert bound["object_id"] == "oid-999"
        assert bound["upn"] == "u@contoso.com"

    def test_audience_mismatch_rejected(self):
        with pytest.raises(ValueError):
            _adapter().validate_and_bind(
                self._token(aud="someone-else"), config=_CONFIG,
                expected_nonce="nonce-1", expected_tenant="tid-123",
                expected_object_id="oid-999",
            )

    def test_nonce_mismatch_rejected(self):
        with pytest.raises(ValueError):
            _adapter().validate_and_bind(
                self._token(nonce="other"), config=_CONFIG,
                expected_nonce="nonce-1", expected_tenant="tid-123",
                expected_object_id="oid-999",
            )

    def test_expired_token_rejected(self):
        with pytest.raises(ValueError):
            _adapter().validate_and_bind(
                self._token(exp=time.time() - 3600), config=_CONFIG,
                expected_nonce="nonce-1", expected_tenant="tid-123",
                expected_object_id="oid-999",
            )

    def test_object_id_mismatch_rejected(self):
        with pytest.raises(ValueError):
            _adapter().validate_and_bind(
                self._token(oid="different"), config=_CONFIG,
                expected_nonce="nonce-1", expected_tenant="tid-123",
                expected_object_id="oid-999",
            )


class TestNoOutboundActions:
    async def test_execute_is_unsupported(self):
        with pytest.raises(ValueError):
            await _adapter().execute(
                action="x", params={}, snapshot_payload={}, config=_CONFIG,
            )
