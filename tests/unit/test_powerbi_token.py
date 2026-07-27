"""Unit tests for PowerBiTokenProvider (identity -> grant -> access token).

Covers the happy path (cached token reuse), the refresh-once path (expired or
forced), and the ``needs_connect`` classification when the user has no identity /
grant. Services are async fakes; no DB or network is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.connectors.oauth import OAuthError
from src.connectors.powerbi_token import PowerBiTokenError, PowerBiTokenProvider


class _Identities:
    def __init__(self, identity, *, entitled=True):
        self._identity = identity
        self._entitled = entitled
        self.entitlement_checks = []

    async def get_by_auth_user(self, auth_user_id):
        return self._identity

    async def can_use_connector(self, identity_id, connector_id, *, group_grant_ids):
        self.entitlement_checks.append((identity_id, connector_id, group_grant_ids))
        return (True, "ok") if self._entitled else (False, "not_in_group")


class _Registry:
    def __init__(self, connector, secret="secret"):
        self._connector = connector
        self._secret = secret

    async def get_by_key(self, key):
        return self._connector

    async def get_client_secret(self, connector_id):
        return self._secret


class _Grants:
    def __init__(self, grant, access=None, refresh="rt"):
        self._grant = grant
        self._access = access
        self._refresh = refresh
        self.stored = []
        self.touched = []

    async def get_grant(self, identity_id, connector_id):
        return self._grant

    async def get_access_token(self, grant_id):
        return self._access

    async def get_refresh_token(self, grant_id):
        return self._refresh

    async def store_access_token(self, grant_id, value, expires_at):
        self._access = {"value": value, "expires_at": expires_at}
        self.stored.append((grant_id, value, expires_at))

    async def store_refreshed_tokens(
        self, grant_id, *, access_token, access_expires_at, refresh_token=None
    ):
        self._access = {"value": access_token, "expires_at": access_expires_at}
        self.stored.append((grant_id, access_token, access_expires_at))
        if refresh_token:
            self._refresh = refresh_token

    async def touch_used(self, grant_id):
        self.touched.append(grant_id)


def _provider(identities, registry, grants):
    return PowerBiTokenProvider(
        identity_service=identities, registry_service=registry, grant_service=grants
    )


_CONNECTOR = {
    "id": "conn-1",
    "is_enabled": True,
    "group_grants": ["grp-1"],
    "current_version": {"manifest": {"provider": "power_bi", "config": {}}},
}


class TestNeedsConnect:
    async def test_non_numeric_auth_user(self):
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), _Grants({"id": "g"}))
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user("not-an-int")
        assert ei.value.needs_connect is True

    async def test_no_identity(self):
        p = _provider(_Identities(None), _Registry(_CONNECTOR), _Grants({"id": "g"}))
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is True

    async def test_no_active_grant(self):
        grants = _Grants({"id": "g", "status": "revoked"})
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is True

    async def test_missing_connector_is_hard_error(self):
        p = _provider(_Identities({"id": "i"}), _Registry(None), _Grants({"id": "g"}))
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is False


class TestEntitlement:
    """An existing grant must not outlive the connector being disabled or the
    user losing group access."""

    async def test_disabled_connector_blocks_existing_grant(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        grants = _Grants(
            {"id": "g", "status": "active"}, access={"value": "at", "expires_at": future}
        )
        registry = _Registry({**_CONNECTOR, "is_enabled": False})
        p = _provider(_Identities({"id": "i"}), registry, grants)
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is False

    async def test_revoked_entitlement_blocks_existing_grant(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        grants = _Grants(
            {"id": "g", "status": "active"}, access={"value": "at", "expires_at": future}
        )
        identities = _Identities({"id": "i"}, entitled=False)
        p = _provider(identities, _Registry(_CONNECTOR), grants)
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        # Reconnecting cannot restore entitlement, so never prompt to connect.
        assert ei.value.needs_connect is False

    async def test_entitlement_checked_with_connector_group_grants(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        grants = _Grants(
            {"id": "g", "status": "active"}, access={"value": "at", "expires_at": future}
        )
        identities = _Identities({"id": "i"})
        p = _provider(identities, _Registry(_CONNECTOR), grants)
        await p.get_token_for_auth_user(42)
        assert identities.entitlement_checks == [("i", "conn-1", ["grp-1"])]


class TestTokenResolution:
    async def test_reuses_valid_cached_token(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        grants = _Grants(
            {"id": "g", "status": "active", "external_account": "u@c.com"},
            access={"value": "cached-at", "expires_at": future},
        )
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)
        tok = await p.get_token_for_auth_user(42)
        assert tok.access_token == "cached-at"
        assert grants.stored == []  # no refresh happened

    async def test_refreshes_expired_token(self, monkeypatch):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        grants = _Grants(
            {"id": "g", "status": "active"},
            access={"value": "old-at", "expires_at": past},
            refresh="rt",
        )
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)

        class _FakeToken:
            access_token = "fresh-at"
            expires_in = 3600

        class _FakeProvider:
            async def refresh(self, **kwargs):
                return _FakeToken()

        import src.connectors.providers as providers
        monkeypatch.setattr(providers, "get_provider", lambda pid: _FakeProvider())

        tok = await p.get_token_for_auth_user(42)
        assert tok.access_token == "fresh-at"
        assert grants.stored  # stored the refreshed token

    async def test_stores_rotated_refresh_token(self, monkeypatch):
        grants = _Grants({"id": "g", "status": "active"}, access=None, refresh="old-rt")
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)

        class _FakeToken:
            access_token = "fresh-at"
            refresh_token = "rotated-rt"
            expires_in = 3600

        class _RotatingProvider:
            async def refresh(self, **kwargs):
                return _FakeToken()

        import src.connectors.providers as providers
        monkeypatch.setattr(providers, "get_provider", lambda pid: _RotatingProvider())

        await p.get_token_for_auth_user(42)
        assert await grants.get_refresh_token("g") == "rotated-rt"

    async def test_invalid_grant_requires_reconnect(self, monkeypatch):
        grants = _Grants(
            {"id": "g", "status": "active"},
            access=None,   # forces refresh path
            refresh="rt",
        )
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)

        class _FailProvider:
            async def refresh(self, **kwargs):
                raise OAuthError("Token endpoint error", error_code="invalid_grant")

        import src.connectors.providers as providers
        monkeypatch.setattr(providers, "get_provider", lambda pid: _FailProvider())

        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is True

    @pytest.mark.parametrize(
        "exc",
        [
            OAuthError("Token endpoint error", error_code="invalid_client"),
            OAuthError("Token endpoint error", error_code="temporarily_unavailable"),
            RuntimeError("connection reset"),
        ],
    )
    async def test_infrastructure_failure_does_not_prompt_reconnect(self, monkeypatch, exc):
        grants = _Grants({"id": "g", "status": "active"}, access=None, refresh="rt")
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)

        class _FailProvider:
            async def refresh(self, **kwargs):
                raise exc

        import src.connectors.providers as providers
        monkeypatch.setattr(providers, "get_provider", lambda pid: _FailProvider())

        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is False

    async def test_no_refresh_token_requires_reconnect(self):
        grants = _Grants(
            {"id": "g", "status": "active"}, access=None, refresh=None
        )
        p = _provider(_Identities({"id": "i"}), _Registry(_CONNECTOR), grants)
        with pytest.raises(PowerBiTokenError) as ei:
            await p.get_token_for_auth_user(42)
        assert ei.value.needs_connect is True
