"""Flask routes for generic OIDC sign-in (Keycloak / Zitadel buttons and callback).

The provider module is monkeypatched at the boundary (authorization URL, code
exchange, ID-token validation) and the database calls are faked, so these
tests cover state/nonce handling, provisioning, role application and errors.
"""

from __future__ import annotations

import pytest

from src import oidc_auth, ui_app

ISSUER = "https://keycloak.example.test/realms/defence"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ui_app, "_needs_first_run_setup", lambda: False)
    monkeypatch.setattr(ui_app, "_entra_sso_enabled", lambda: False)
    monkeypatch.setenv("PUBLIC_APP_URL", "http://app.test")
    monkeypatch.setenv("OIDC_KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_KEYCLOAK_CLIENT_ID", "jeen-insights")
    monkeypatch.setenv("OIDC_KEYCLOAK_LABEL", "Keycloak (Entra ID)")
    monkeypatch.setenv("OIDC_ZITADEL_ISSUER", "https://zitadel.example.test")
    monkeypatch.setenv("OIDC_ZITADEL_CLIENT_ID", "3885961")
    monkeypatch.setattr(
        oidc_auth, "build_authorization_url",
        lambda provider, *, redirect_uri, state, nonce, code_verifier:
            f"{ISSUER}/auth?client_id={provider.client_id}&redirect_uri={redirect_uri}&state={state}",
    )
    ui_app.app.config["WTF_CSRF_ENABLED"] = False
    ui_app.app.config["TESTING"] = True
    with ui_app.app.test_client() as c:
        yield c
    ui_app.app.config["WTF_CSRF_ENABLED"] = True


@pytest.fixture()
def db(monkeypatch):
    """Fake auth_db: one existing viewer, records provisioning and role changes."""
    store = {"users": {"dana@example.com": {"id": 7, "name": "Dana", "email": "dana@example.com", "role": "viewer",
                                              "status": "active", "avatar_hue": 10, "locale": None}},
             "created": [], "roles": [], "touched": []}

    def get_or_create(email, name, *, role="viewer"):
        user = store["users"].get(email)
        if user is None:
            user = {"id": 99, "name": name, "email": email, "role": role, "status": "active", "avatar_hue": 1, "locale": None}
            store["users"][email] = user
            store["created"].append((email, name, role))
        return dict(user)

    from src import auth_db
    monkeypatch.setattr(auth_db, "get_or_create_sso_user", get_or_create)
    monkeypatch.setattr(auth_db, "update_user_role", lambda uid, role: store["roles"].append((uid, role)))
    monkeypatch.setattr(auth_db, "touch_last_active", lambda uid: store["touched"].append(uid))
    return store


def _start(client, provider="keycloak", next_url="/"):
    res = client.get(f"/auth/{provider}?next={next_url}")
    with client.session_transaction() as sess:
        return res, sess.get("oidc_state"), sess.get("oidc_nonce")


def _fake_tokens(monkeypatch, claims):
    monkeypatch.setattr(oidc_auth, "exchange_code", lambda p, *, code, redirect_uri, code_verifier: {"id_token": "jwt"})
    monkeypatch.setattr(oidc_auth, "validate_id_token", lambda p, token, *, nonce: dict(claims, nonce=nonce))


def test_login_page_lists_every_configured_provider(client):
    body = client.get("/login").get_data(as_text=True)
    assert 'id="oidc-login-keycloak"' in body and "Continue with Keycloak (Entra ID)" in body
    assert 'id="oidc-login-zitadel"' in body and "Continue with Zitadel" in body
    assert "microsoft-login-btn" not in body


def test_start_redirects_to_the_provider_with_session_state(client):
    res, state, nonce = _start(client, next_url="/settings")
    assert res.status_code == 302
    assert res.headers["Location"].startswith(f"{ISSUER}/auth?client_id=jeen-insights")
    assert "redirect_uri=http://app.test/auth/keycloak/callback" in res.headers["Location"]
    assert state and nonce
    with client.session_transaction() as sess:
        assert sess["oauth_next"] == "/settings" and sess["oidc_provider"] == "keycloak"


def test_unknown_provider_is_404(client):
    assert client.get("/auth/okta").status_code == 404
    assert client.get("/auth/okta/callback?code=x&state=y").status_code == 404


def test_callback_rejects_wrong_state(client, db):
    _start(client)
    res = client.get("/auth/keycloak/callback?code=abc&state=forged")
    assert res.status_code == 401
    assert "Invalid sign-in state" in res.get_data(as_text=True)


def test_callback_rejects_provider_switch(client, db, monkeypatch):
    _, state, _ = _start(client, provider="keycloak")
    _fake_tokens(monkeypatch, {"email": "dana@example.com", "name": "Dana", "sub": "s"})
    res = client.get(f"/auth/zitadel/callback?code=abc&state={state}")
    assert res.status_code == 401


def test_callback_signs_in_an_existing_user_and_keeps_db_role(client, db, monkeypatch):
    _, state, _ = _start(client, next_url="/settings")
    _fake_tokens(monkeypatch, {"email": "Dana@Example.com", "name": "Dana", "sub": "s"})
    res = client.get(f"/auth/keycloak/callback?code=abc&state={state}")
    assert res.status_code == 302 and res.headers["Location"].endswith("/settings")
    with client.session_transaction() as sess:
        assert sess["user_id"] == 7 and sess["user_role"] == "viewer" and sess["auth_provider"] == "keycloak"
        assert "oidc_state" not in sess and "oidc_verifier" not in sess
    assert db["created"] == [] and db["roles"] == [] and db["touched"] == [7]


def test_callback_provisions_a_new_viewer(client, db, monkeypatch):
    _, state, _ = _start(client, provider="zitadel")
    _fake_tokens(monkeypatch, {"email": "new@example.com", "name": "New Person", "sub": "s"})
    res = client.get(f"/auth/zitadel/callback?code=abc&state={state}")
    assert res.status_code == 302
    assert db["created"] == [("new@example.com", "New Person", "viewer")]
    with client.session_transaction() as sess:
        assert sess["user_id"] == 99 and sess["auth_provider"] == "zitadel"


def test_callback_applies_mapped_roles_when_configured(client, db, monkeypatch):
    monkeypatch.setenv("OIDC_KEYCLOAK_ADMIN_ROLES", "insights-admin")
    _, state, _ = _start(client)
    _fake_tokens(monkeypatch, {"email": "dana@example.com", "name": "Dana", "sub": "s",
                               "realm_access": {"roles": ["insights-admin"]}})
    res = client.get(f"/auth/keycloak/callback?code=abc&state={state}")
    assert res.status_code == 302
    assert db["roles"] == [(7, "admin")]
    with client.session_transaction() as sess:
        assert sess["user_role"] == "admin"


def test_callback_without_email_or_with_idp_error_fails_cleanly(client, db, monkeypatch):
    _, state, _ = _start(client)
    _fake_tokens(monkeypatch, {"sub": "s", "preferred_username": "no-email"})
    res = client.get(f"/auth/keycloak/callback?code=abc&state={state}")
    assert res.status_code == 401 and "has no email address" in res.get_data(as_text=True)

    _start(client)
    res = client.get("/auth/keycloak/callback?error=access_denied&error_description=User+declined")
    assert res.status_code == 401 and "User declined" in res.get_data(as_text=True)


def test_callback_reports_token_validation_failure(client, db, monkeypatch):
    _, state, _ = _start(client)
    monkeypatch.setattr(oidc_auth, "exchange_code", lambda p, **kw: {"id_token": "jwt"})

    def _boom(p, token, *, nonce):
        raise oidc_auth.OidcError("Keycloak ID token nonce mismatch")

    monkeypatch.setattr(oidc_auth, "validate_id_token", _boom)
    res = client.get(f"/auth/keycloak/callback?code=abc&state={state}")
    assert res.status_code == 401 and "nonce mismatch" in res.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_disabled_account_cannot_sign_in(client, db, monkeypatch):
    db["users"]["dana@example.com"]["status"] = "disabled"
    _, state, _ = _start(client)
    _fake_tokens(monkeypatch, {"email": "dana@example.com", "name": "Dana", "sub": "s"})
    res = client.get(f"/auth/keycloak/callback?code=abc&state={state}")
    assert res.status_code == 401
    with client.session_transaction() as sess:
        assert "user_id" not in sess
