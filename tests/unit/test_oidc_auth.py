"""Generic OIDC sign-in helpers: configuration, PKCE, ID-token validation, roles.

No network: discovery and JWKS are monkeypatched; tokens are signed with a
throwaway RSA key so validation runs the real PyJWT path.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src import oidc_auth

ISSUER = "https://idp.example.test/realms/defence"
CLIENT_ID = "jeen-insights"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("OIDC_"):
            monkeypatch.delenv(name, raising=False)
    oidc_auth.reset_caches()
    yield
    oidc_auth.reset_caches()


@pytest.fixture()
def keycloak(monkeypatch):
    monkeypatch.setenv("OIDC_KEYCLOAK_ISSUER", ISSUER + "/")
    monkeypatch.setenv("OIDC_KEYCLOAK_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_KEYCLOAK_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("OIDC_KEYCLOAK_IDP_HINT", "entra-id")
    return oidc_auth.get_provider("keycloak")


@pytest.fixture()
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def fake_idp(monkeypatch, rsa_key):
    """Serve discovery + JWKS from memory."""
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    meta = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
        "token_endpoint": ISSUER + "/protocol/openid-connect/token",
        "jwks_uri": ISSUER + "/protocol/openid-connect/certs",
    }

    def _get_json(url):
        if url.endswith("/.well-known/openid-configuration"):
            return meta
        if url == meta["jwks_uri"]:
            return {"keys": [public_jwk]}
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(oidc_auth, "_get_json", _get_json)
    return meta


def _id_token(rsa_key, **claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u-1", "iat": now, "exp": now + 300,
               "nonce": "n0nce", "email": "Dana@Example.com", "name": "Dana"}
    payload.update(claims)
    return jwt.encode(payload, rsa_key, algorithm="RS256", headers={"kid": "k1"})


# ── configuration ────────────────────────────────────────────────────────────

def test_provider_requires_issuer_and_client_id(monkeypatch):
    assert oidc_auth.configured_providers() == []
    monkeypatch.setenv("OIDC_ZITADEL_ISSUER", "https://zitadel.example.test")
    assert oidc_auth.configured_providers() == []
    monkeypatch.setenv("OIDC_ZITADEL_CLIENT_ID", "123")
    [p] = oidc_auth.configured_providers()
    assert p.key == "zitadel" and p.label == "Zitadel" and not p.confidential
    assert oidc_auth.ZITADEL_ROLES_SCOPE in p.scopes


def test_provider_parsing_strips_slash_and_reads_roles(keycloak):
    assert keycloak.issuer == ISSUER
    assert keycloak.confidential and keycloak.idp_hint == "entra-id"
    assert keycloak.redirect_uri("http://app.test/") == "http://app.test/auth/keycloak/callback"
    assert not keycloak.maps_roles


def test_plain_http_issuer_is_refused(monkeypatch):
    monkeypatch.setenv("OIDC_KEYCLOAK_ISSUER", "http://keycloak.internal/realms/x")
    monkeypatch.setenv("OIDC_KEYCLOAK_CLIENT_ID", "c")
    assert oidc_auth.get_provider("keycloak") is None


def test_unknown_or_unsafe_provider_keys_are_ignored(monkeypatch):
    monkeypatch.setenv("OIDC_PROVIDERS", "keycloak,../etc,Bad Key")
    monkeypatch.setenv("OIDC_KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_KEYCLOAK_CLIENT_ID", "c")
    assert [p.key for p in oidc_auth.configured_providers()] == ["keycloak"]
    assert oidc_auth.get_provider("../etc") is None


# ── authorization request ────────────────────────────────────────────────────

def test_authorization_url_carries_pkce_state_nonce_and_hint(keycloak, fake_idp):
    verifier = oidc_auth.new_code_verifier()
    url = oidc_auth.build_authorization_url(
        keycloak, redirect_uri="http://app.test/auth/keycloak/callback", state="st", nonce="nn", code_verifier=verifier,
    )
    assert url.startswith(fake_idp["authorization_endpoint"] + "?")
    for needle in ("response_type=code", f"client_id={CLIENT_ID}", "state=st", "nonce=nn",
                   "code_challenge_method=S256", "kc_idp_hint=entra-id", "scope=openid+profile+email",
                   f"code_challenge={oidc_auth.code_challenge(verifier)}"):
        assert needle in url


def test_discovery_issuer_mismatch_is_an_error(keycloak, monkeypatch):
    monkeypatch.setattr(oidc_auth, "_get_json", lambda url: {"issuer": "https://other", "authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"})
    with pytest.raises(oidc_auth.OidcError, match="issuer mismatch"):
        oidc_auth.discovery(keycloak)


# ── ID token validation ──────────────────────────────────────────────────────

def test_valid_id_token_yields_claims(keycloak, fake_idp, rsa_key):
    claims = oidc_auth.validate_id_token(keycloak, _id_token(rsa_key), nonce="n0nce")
    assert claims["sub"] == "u-1"
    assert oidc_auth.profile_from_claims(claims) == {"email": "dana@example.com", "name": "Dana", "sub": "u-1"}


@pytest.mark.parametrize("bad", [
    {"nonce": "other"},
    {"aud": "someone-else"},
    {"iss": "https://evil.test"},
    {"exp": int(time.time()) - 600},
])
def test_tampered_or_stale_id_tokens_are_rejected(keycloak, fake_idp, rsa_key, bad):
    with pytest.raises(oidc_auth.OidcError):
        oidc_auth.validate_id_token(keycloak, _id_token(rsa_key, **bad), nonce="n0nce")


def test_token_signed_by_another_key_is_rejected(keycloak, fake_idp):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(oidc_auth.OidcError):
        oidc_auth.validate_id_token(keycloak, _id_token(other), nonce="n0nce")


def test_unsigned_tokens_are_rejected(keycloak, fake_idp):
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "u", "iat": now, "exp": now + 60, "nonce": "n0nce"}, key="", algorithm="none")
    with pytest.raises(oidc_auth.OidcError, match="unsupported algorithm"):
        oidc_auth.validate_id_token(keycloak, token, nonce="n0nce")


# ── profile and roles ────────────────────────────────────────────────────────

def test_profile_falls_back_to_preferred_username_and_given_family_names():
    claims = {"preferred_username": "Ben@Example.com", "given_name": "Ben", "family_name": "B", "sub": "s"}
    assert oidc_auth.profile_from_claims(claims) == {"email": "ben@example.com", "name": "Ben B", "sub": "s"}
    assert oidc_auth.profile_from_claims({"preferred_username": "ben", "sub": "s"})["email"] == ""


def test_roles_are_collected_from_keycloak_and_zitadel_shapes(keycloak):
    claims = {
        "realm_access": {"roles": ["offline_access", "insights-admin"]},
        "resource_access": {CLIENT_ID: {"roles": ["editor"]}, "other": {"roles": ["nope"]}},
        "groups": ["/analysts"],
        "urn:zitadel:iam:org:project:roles": {"viewer": {"1": "org"}},
        "urn:zitadel:iam:org:project:123:roles": {"zitadel-admin": {}},
    }
    roles = oidc_auth.roles_from_claims(keycloak, claims)
    assert roles == {"offline_access", "insights-admin", "editor", "analysts", "viewer", "zitadel-admin"}


def test_role_mapping_is_authoritative_only_when_configured(monkeypatch):
    monkeypatch.setenv("OIDC_KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_KEYCLOAK_CLIENT_ID", CLIENT_ID)
    provider = oidc_auth.get_provider("keycloak")
    assert oidc_auth.mapped_role(provider, {"realm_access": {"roles": ["admin"]}}) is None

    monkeypatch.setenv("OIDC_KEYCLOAK_ADMIN_ROLES", "insights-admin, admin")
    monkeypatch.setenv("OIDC_KEYCLOAK_EDITOR_ROLES", "insights-editor")
    provider = oidc_auth.get_provider("keycloak")
    assert oidc_auth.mapped_role(provider, {"realm_access": {"roles": ["admin"]}}) == "admin"
    assert oidc_auth.mapped_role(provider, {"realm_access": {"roles": ["insights-editor"]}}) == "editor"
    assert oidc_auth.mapped_role(provider, {"realm_access": {"roles": ["nothing"]}}) == "viewer"
