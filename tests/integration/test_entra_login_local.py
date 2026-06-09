"""Local integration tests for Microsoft Entra SSO on the Flask UI.

Requires the stack running at http://localhost:8501 with AZURE_AD_* in .env:
    docker compose up -d --build
    pytest tests/integration/test_entra_login_local.py -m integration -v
"""

from __future__ import annotations

import os

import pytest
import requests

APP_URL = os.getenv("JEEN_UI_URL", "http://localhost:8501")
TIMEOUT = 10


pytestmark = pytest.mark.integration


def _ui_up() -> bool:
    try:
        r = requests.get(f"{APP_URL}/health", timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_local_ui():
    if not _ui_up():
        pytest.skip(f"UI not running at {APP_URL} — start with: docker compose up -d")


def test_login_page_shows_microsoft_button():
    html = requests.get(f"{APP_URL}/login", timeout=TIMEOUT).text
    assert "Sign in with Microsoft" in html
    assert 'href="/auth/microsoft' in html
    assert "Sign in with password" in html


def test_microsoft_login_starts_oauth_redirect():
    session = requests.Session()
    resp = session.get(f"{APP_URL}/auth/microsoft", timeout=TIMEOUT, allow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "login.microsoftonline.com" in location
    assert "65703eb9-76d8-4d4a-a438-2750a1401174" in location
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8501%2Fauth%2Fmicrosoft%2Fcallback" in location


def test_password_login_still_works():
    session = requests.Session()
    resp = session.post(
        f"{APP_URL}/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers.get("Location", "").endswith("/")
    home = session.get(f"{APP_URL}/", timeout=TIMEOUT, allow_redirects=False)
    assert home.status_code == 200
