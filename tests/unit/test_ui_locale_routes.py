"""Flask side of the interface language: resolution precedence, document
attributes/headers, the account preference endpoint and the login cookie sync.

Everything that touches the database or the FastAPI backend is monkeypatched;
CSRF is disabled for the client because the token normally reaches the
browser through the rendered page.
"""

from __future__ import annotations

import json
import re

import pytest

from src import ui_app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ui_app, "_needs_first_run_setup", lambda: False)
    monkeypatch.setattr(ui_app, "_entra_sso_enabled", lambda: False)
    ui_app.app.config["WTF_CSRF_ENABLED"] = False
    ui_app.app.config["TESTING"] = True
    with ui_app.app.test_client() as c:
        yield c
    ui_app.app.config["WTF_CSRF_ENABLED"] = True


def _login(client, locale=None):
    with client.session_transaction() as sess:
        sess["user_id"] = 7
        sess["user_name"] = "Dana"
        sess["user_email"] = "dana@example.com"
        sess["user_role"] = "admin"
        sess["avatar_hue"] = 120
        sess["locale"] = locale


def _html_attrs(body: str) -> tuple[str, str]:
    match = re.search(r'<html lang="([^"]+)" dir="([^"]+)"', body)
    assert match, "html tag must carry lang and dir"
    return match.group(1), match.group(2)


def test_login_page_defaults_to_english_ltr_with_language_headers(client):
    res = client.get("/login")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert _html_attrs(body) == ("en", "ltr")
    assert res.headers["Content-Language"] == "en"
    vary = {v.strip() for v in res.headers.get("Vary", "").split(",")}
    assert {"Cookie", "Accept-Language"} <= vary
    assert "Sign in to your workspace" in body
    # No language control before sign-in: the choice lives in the account menu.
    assert "login-lang" not in body and 'data-locale="he"' not in body
    # Anonymous pages never receive the workspace catalog payload.
    assert 'id="i18n-bootstrap"' not in body


def test_anonymous_page_negotiates_accept_language(client):
    res = client.get("/login", headers={"Accept-Language": "he-IL,he;q=0.9,en;q=0.5"})
    body = res.get_data(as_text=True)
    assert _html_attrs(body) == ("he", "rtl")
    assert res.headers["Content-Language"] == "he"
    assert "התחברו לסביבת העבודה שלכם" in body


def test_anonymous_cookie_beats_accept_language(client):
    client.set_cookie("locale", "en")
    res = client.get("/login", headers={"Accept-Language": "he"})
    assert _html_attrs(res.get_data(as_text=True)) == ("en", "ltr")


def test_unknown_cookie_value_is_ignored(client):
    client.set_cookie("locale", "<script>alert(1)</script>")
    res = client.get("/login")
    assert _html_attrs(res.get_data(as_text=True)) == ("en", "ltr")
    assert "<script>alert(1)" not in res.get_data(as_text=True)


def test_signed_in_account_language_beats_a_stale_cookie(client, monkeypatch):
    _login(client, locale="he")
    client.set_cookie("locale", "en")  # another user's choice on a shared browser
    res = client.get("/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert _html_attrs(body) == ("he", "rtl")
    assert res.headers["Content-Language"] == "he"
    # The workspace page embeds the effective locale's catalog as inert JSON.
    match = re.search(r'<script type="application/json" id="i18n-bootstrap">(.*?)</script>', body, re.S)
    assert match
    boot = json.loads(match.group(1))
    assert boot["locale"] == "he" and boot["dir"] == "rtl" and boot["formatLocale"] == "he-IL"
    assert boot["messages"]["common"]["save"] == "שמירה"
    assert [l["tag"] for l in boot["locales"]] == ["en", "he"]


def test_signed_in_without_saved_language_uses_cookie_then_header(client):
    _login(client, locale=None)
    client.set_cookie("locale", "he")
    assert _html_attrs(client.get("/").get_data(as_text=True)) == ("he", "rtl")
    client.delete_cookie("locale")
    res = client.get("/", headers={"Accept-Language": "he"})
    assert _html_attrs(res.get_data(as_text=True)) == ("he", "rtl")


def test_auth_me_exposes_saved_locale(client, monkeypatch):
    import src.security.app_flags as flags

    monkeypatch.setattr(flags, "get_connectors_enabled_sync", lambda: False)
    monkeypatch.setattr(flags, "get_agent_tools_enabled_sync", lambda: False)
    _login(client, locale="he")
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.get_json()["locale"] == "he"


def test_patch_locale_persists_updates_session_and_sets_cookie(client, monkeypatch):
    import src.auth_db as auth_db

    saved = {}
    monkeypatch.setattr(auth_db, "set_user_locale", lambda uid, loc: saved.update({uid: loc}))
    _login(client, locale=None)

    res = client.patch("/api/auth/me/locale", json={"locale": "he"})
    assert res.status_code == 200
    assert res.get_json() == {"locale": "he"}
    assert saved == {7: "he"}
    cookie = res.headers.get("Set-Cookie", "")
    assert cookie.startswith("locale=he")
    assert "Path=/" in cookie and "SameSite=Lax" in cookie and "Max-Age=31536000" in cookie
    assert "HttpOnly" not in cookie  # readable by the client runtime on purpose
    with client.session_transaction() as sess:
        assert sess["locale"] == "he"
    # The very next page render already uses the new language.
    assert _html_attrs(client.get("/").get_data(as_text=True)) == ("he", "rtl")


def test_patch_locale_rejects_unknown_tags_and_normalizes_regions(client, monkeypatch):
    import src.auth_db as auth_db

    saved = {}
    monkeypatch.setattr(auth_db, "set_user_locale", lambda uid, loc: saved.update({uid: loc}))
    _login(client)
    bad = client.patch("/api/auth/me/locale", json={"locale": "fr"})
    assert bad.status_code == 400 and bad.get_json()["code"] == "INVALID_LOCALE"
    assert saved == {}
    ok = client.patch("/api/auth/me/locale", json={"locale": "he-IL"})
    assert ok.status_code == 200 and saved == {7: "he"}


def test_patch_locale_requires_a_session(client):
    res = client.patch("/api/auth/me/locale", json={"locale": "he"})
    assert res.status_code == 401
    assert res.get_json()["code"] == "UNAUTHENTICATED"


def test_local_login_syncs_the_account_language_into_the_cookie(client, monkeypatch):
    import src.auth_db as auth_db

    user = {
        "id": 7, "name": "Dana", "email": "dana@example.com", "password_hash": "x",
        "role": "viewer", "status": "active", "avatar_hue": 10, "locale": "he",
    }
    monkeypatch.setattr(auth_db, "get_user_by_email", lambda email: user)
    monkeypatch.setattr(auth_db, "verify_password", lambda plain, hashed: True)
    monkeypatch.setattr(auth_db, "touch_last_active", lambda uid: None)
    res = client.post("/login", data={"email": "dana@example.com", "password": "pw"})
    assert res.status_code == 302
    assert res.headers.get("Set-Cookie", "").startswith("locale=he")
    with client.session_transaction() as sess:
        assert sess["locale"] == "he"


def test_local_login_without_saved_language_keeps_the_prelogin_cookie(client, monkeypatch):
    import src.auth_db as auth_db

    user = {
        "id": 8, "name": "Noa", "email": "noa@example.com", "password_hash": "x",
        "role": "viewer", "status": "active", "avatar_hue": 10, "locale": None,
    }
    monkeypatch.setattr(auth_db, "get_user_by_email", lambda email: user)
    monkeypatch.setattr(auth_db, "verify_password", lambda plain, hashed: True)
    monkeypatch.setattr(auth_db, "touch_last_active", lambda uid: None)
    client.set_cookie("locale", "he")
    res = client.post("/login", data={"email": "noa@example.com", "password": "pw"})
    assert res.status_code == 302
    assert "locale=" not in res.headers.get("Set-Cookie", "")
    # Display still follows the cookie the visitor chose before signing in.
    assert _html_attrs(client.get("/").get_data(as_text=True)) == ("he", "rtl")


def test_setup_page_renders_in_hebrew_with_ltr_credential_fields(client, monkeypatch):
    monkeypatch.setattr(ui_app, "_needs_first_run_setup", lambda: True)
    monkeypatch.setattr(ui_app, "_setup_bootstrap_token", lambda: "tok")
    client.set_cookie("locale", "he")
    res = client.get("/setup")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert _html_attrs(body) == ("he", "rtl")
    assert "ברוכים הבאים ל-Jeen Insights" in body
    # Email/password/token inputs stay LTR islands inside the RTL page.
    assert 'id="email" name="email" type="email" class="login-input" required dir="ltr"' in body


def test_login_errors_render_in_the_request_language(client, monkeypatch):
    import src.auth_db as auth_db

    monkeypatch.setattr(auth_db, "get_user_by_email", lambda email: None)
    client.set_cookie("locale", "he")
    res = client.post("/login", data={"email": "x@example.com", "password": "pw"})
    assert res.status_code == 401
    assert "אימייל או סיסמה שגויים." in res.get_data(as_text=True)


def test_patch_locale_does_not_leak_database_errors(client, monkeypatch):
    import src.auth_db as auth_db

    def boom(uid, loc):
        raise RuntimeError("FATAL: password authentication failed for user \"insights\" @ 10.0.0.9")

    monkeypatch.setattr(auth_db, "set_user_locale", boom)
    _login(client)
    res = client.patch("/api/auth/me/locale", json={"locale": "he"})
    assert res.status_code == 500
    body = res.get_json()
    assert body["code"] == "DB_ERROR"
    assert "10.0.0.9" not in body["error"] and "insights" not in body["error"]
    with client.session_transaction() as sess:
        assert sess.get("locale") is None  # nothing changed on failure


def test_patch_locale_is_csrf_protected(monkeypatch):
    """With CSRF enabled (as in production) the endpoint needs the token that
    the rendered page hands to csrf.js; a bare cross-site PATCH is refused."""
    import src.auth_db as auth_db
    from flask_wtf.csrf import generate_csrf

    monkeypatch.setattr(ui_app, "_needs_first_run_setup", lambda: False)
    monkeypatch.setattr(ui_app, "_entra_sso_enabled", lambda: False)
    saved = {}
    monkeypatch.setattr(auth_db, "set_user_locale", lambda uid, loc: saved.update({uid: loc}))
    ui_app.app.config["WTF_CSRF_ENABLED"] = True
    ui_app.app.config["TESTING"] = True
    with ui_app.app.test_client() as c:
        _login(c)
        denied = c.patch("/api/auth/me/locale", json={"locale": "he"})
        assert denied.status_code == 400
        assert saved == {}
        with c.session_transaction() as sess:
            with ui_app.app.test_request_context():
                from flask import session as ctx_session
                ctx_session.update(sess)
                token = generate_csrf()
                sess.update(ctx_session)
        ok = c.patch("/api/auth/me/locale", json={"locale": "he"}, headers={"X-CSRFToken": token})
        assert ok.status_code == 200, ok.get_data(as_text=True)
        assert saved == {7: "he"}


@pytest.mark.parametrize(
    "target, expected",
    [
        ("/settings", "/settings"),
        ("/?q=1", "/?q=1"),
        ("https://evil.example/", "/"),
        ("//evil.example/", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_safe_next_only_allows_local_paths(target, expected):
    assert ui_app._safe_next(target) == expected


def test_login_next_cannot_redirect_off_site(client, monkeypatch):
    import src.auth_db as auth_db

    user = {
        "id": 7, "name": "Dana", "email": "dana@example.com", "password_hash": "x",
        "role": "viewer", "status": "active", "avatar_hue": 10, "locale": None,
    }
    monkeypatch.setattr(auth_db, "get_user_by_email", lambda email: user)
    monkeypatch.setattr(auth_db, "verify_password", lambda plain, hashed: True)
    monkeypatch.setattr(auth_db, "touch_last_active", lambda uid: None)
    res = client.post(
        "/login", data={"email": "dana@example.com", "password": "pw", "next": "https://evil.example/"}
    )
    assert res.status_code == 302
    assert res.headers["Location"] in ("/", "http://localhost/")
    # An already signed-in visitor hitting /login?next=... is equally constrained.
    res = client.get("/login?next=//evil.example/x")
    assert res.status_code == 302
    assert res.headers["Location"] in ("/", "http://localhost/")


def test_workspace_user_menu_carries_the_language_picker(client):
    """The switcher lives in the avatar dropdown, server-rendered so the active
    option is right on first paint, each label in its own script/direction."""
    _login(client, locale="he")
    body = client.get("/").get_data(as_text=True)
    assert 'id="user-lang-picker"' in body
    assert 'lang="en" dir="ltr"' in body and 'lang="he" dir="rtl"' in body
    he_option = re.search(r'<button[^>]*data-locale="he"[^>]*>', body).group(0)
    en_option = re.search(r'<button[^>]*data-locale="en"[^>]*>', body).group(0)
    assert 'aria-checked="true"' in he_option and 'aria-checked="false"' in en_option
    assert "<bdi>עברית</bdi>" in body and "<bdi>English</bdi>" in body
