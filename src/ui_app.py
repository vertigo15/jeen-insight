"""Flask UI for Jeen Insights.

Acts as a thin pass-through to the FastAPI backend. The browser sends a
`connection` (source_key) along with every data-related request; this UI
forwards it on without inspecting it.

Browser authentication is handled here in the Flask layer using a signed
session cookie. The FastAPI backend is not exposed to browser users and does
not authenticate them directly, but it is not unauthenticated: this layer mints
a short-lived internal token per request, which the backend verifies (see
``src/security/internal_auth.py``) and refuses to serve without.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Dict

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var safely — ``"false"``/``"0"``/``"no"`` are False."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "t")


# Development / POC mode is the DEFAULT so a fresh copy of the app + shared DB
# "just works" anywhere with zero secret provisioning. Harden a real deployment
# with JEEN_DEV_MODE=false, which then requires strong, non-default secrets.
DEV_MODE = _env_bool("JEEN_DEV_MODE", default=True)

# Send cookies over HTTPS only. Defaults to ON in production, OFF in dev so local
# http://localhost:8501 still logs in.
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", default=not DEV_MODE)

app = Flask(__name__, template_folder="templates", static_folder="static")

# Session-signing secret. A predictable key lets anyone forge an (admin) session
# or an internal API token, so known/placeholder keys are rejected OUTRIGHT in
# production — regardless of the cookie flag — and only tolerated in dev mode.
_DEV_SECRET = "jeen-insights-dev-only-insecure-secret"  # noqa: S105 (dev only)
_INSECURE_SECRETS = {
    _DEV_SECRET,
    "jeen-insights-change-me-in-production",           # old shipped fallback
    "change-me-generate-a-random-48+-char-value",      # .env.example placeholder
}
_secret_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("AUTH_SECRET")
_secret_weak = (not _secret_key) or (_secret_key in _INSECURE_SECRETS)
if _secret_weak and not DEV_MODE:
    raise RuntimeError(
        "A strong, non-default FLASK_SECRET_KEY (or AUTH_SECRET) is required. "
        "Refusing to start with a missing or known placeholder session key. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
        "(or set JEEN_DEV_MODE=true for local development only)."
    )
if _secret_weak:
    _secret_key = _secret_key or _DEV_SECRET
    logger.warning(
        "JEEN_DEV_MODE: using a weak/known session key. NEVER do this in production."
    )
app.secret_key = _secret_key

if not DEV_MODE and not SESSION_COOKIE_SECURE:
    logger.warning(
        "Production mode with SESSION_COOKIE_SECURE=false — session/CSRF cookies "
        "will be sent over plain HTTP. Terminate TLS and set SESSION_COOKIE_SECURE=true."
    )

# Fail fast if the internal Flask→API signing secret is unsafe for production.
from src.security.internal_auth import assert_configured as _assert_internal_auth
_assert_internal_auth()

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
app.config["PERMANENT_SESSION_LIFETIME"] = 86_400  # 24 h

# ── CSRF protection ──────────────────────────────────────────────────────────
# Flask-WTF validates a per-session token on every mutating request
# (POST/PUT/PATCH/DELETE; GET/HEAD/OPTIONS are exempt). The token reaches the
# browser via the login form field and the index.html <meta> tag; static/csrf.js
# echoes it back in the X-CSRFToken header on same-origin fetches. Tie the token
# lifetime to the session (24 h) instead of Flask-WTF's 1 h default so a long
# session never fails mid-use. WTF_CSRF_SSL_STRICT tracks the cookie's Secure
# flag so the HTTPS referer check only applies once we're actually on HTTPS.
app.config["WTF_CSRF_TIME_LIMIT"] = None
app.config["WTF_CSRF_SSL_STRICT"] = SESSION_COOKIE_SECURE
csrf = CSRFProtect(app)

# ── Login brute-force throttling ─────────────────────────────────────────────
# In-memory storage suits the single-process `flask run` container
# (Dockerfile.ui). Behind multiple workers/instances or a reverse proxy, set
# RATELIMIT_STORAGE_URI to a shared store (e.g. redis://…) and configure trusted
# proxy handling so get_remote_address sees the real client IP (not the proxy).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=[],
)


@app.errorhandler(CSRFError)
def _handle_csrf_error(exc: CSRFError):
    """Return JSON for API callers; bounce page requests back to /login."""
    logger.warning("CSRF validation failed for %s %s", request.method, request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": "CSRF validation failed", "code": "CSRF"}), 400
    return redirect(url_for("login"))


@app.errorhandler(429)
def _handle_rate_limit(exc):
    """Friendly 429: re-render the login page for /login, JSON elsewhere.

    Retry-After reflects the breached limiter's actual reset time (falling back
    to 60s) rather than a hardcoded guess, so it's correct whether the per-minute
    or per-hour bucket tripped.
    """
    retry_after = 60
    try:
        current = getattr(limiter, "current_limit", None)
        reset_at = getattr(current, "reset_at", None) if current else None
        if reset_at:
            retry_after = max(1, int(reset_at - time.time()))
    except Exception:  # noqa: BLE001
        pass
    if request.path == "/login":
        body = render_template(
            "login.html",
            error="Too many sign-in attempts. Please wait a minute and try again.",
            entra_sso_enabled=_entra_sso_enabled(),
        )
        response = app.make_response((body, 429))
    else:
        response = jsonify({"error": "Too many requests", "code": "RATE_LIMITED"})
        response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


API_BASE_URL = os.getenv("API_BASE_URL", "http://jeen-insights-api:8000")

# Paths that never require a login check.
_PUBLIC_PREFIXES = ("/static/", "/favicon")
_PUBLIC_EXACT    = {
    "/login",
    "/logout",
    "/health",
    "/setup",
    "/auth/microsoft",
    "/auth/microsoft/callback",
}

# Cached once an admin exists so we don't hit the DB on every request forever.
_admin_bootstrapped = False

# First-run setup requires an out-of-band token so a fresh, admin-less install
# cannot be taken over by any anonymous visitor who reaches /setup. The token is
# operator-provided (SETUP_BOOTSTRAP_TOKEN) or, if unset, generated once and
# printed to the server log — so only someone with server/log access (an
# operator) can complete bootstrap.
_generated_setup_token: str | None = None


def _setup_bootstrap_token() -> str:
    """Return the token required to complete first-run admin setup."""
    global _generated_setup_token
    env = (os.getenv("SETUP_BOOTSTRAP_TOKEN") or "").strip()
    if env:
        return env
    if _generated_setup_token is None:
        _generated_setup_token = secrets.token_urlsafe(32)
        logger.warning(
            "FIRST-RUN SETUP TOKEN — enter this on the /setup page to create the "
            "first admin account (set SETUP_BOOTSTRAP_TOKEN to pin your own): %s",
            _generated_setup_token,
        )
    return _generated_setup_token


def _needs_first_run_setup() -> bool:
    """True when no usable admin exists yet (drives the /setup screen)."""
    global _admin_bootstrapped
    if _admin_bootstrapped:
        return False
    try:
        from src.auth_db import active_admin_exists

        if active_admin_exists():
            _admin_bootstrapped = True
            return False
        return True
    except Exception:  # noqa: BLE001 - fail closed to normal login on DB error
        return False


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _session_claims() -> Dict[str, Any]:
    """Identity claims embedded in the internal token minted for the API."""
    return {
        "user_id": str(session.get("user_id") or ""),
        "role": session.get("user_role") or "viewer",
        "name": session.get("user_name") or "",
        "email": session.get("user_email") or "",
        "tenant_id": session.get("tenant_id") or "",
        "object_id": session.get("object_id") or "",
        "groups": session.get("groups") or [],
        "groups_complete": session.get("groups_complete", True),
        "auth_provider": session.get("auth_provider") or "local",
        "auth_time": int(session.get("auth_time") or 0),
    }


def _internal_headers() -> Dict[str, str]:
    """Mint a short-lived, audience-bound internal token for upstream API calls.

    Flask is the SOLE issuer; FastAPI verifies this into a Principal and derives
    all identity/role/group facts from it (never from the request body).
    """
    if "user_id" not in session:
        return {}
    from src.security.internal_auth import issue_internal_token

    return {"Authorization": f"Bearer {issue_internal_token(_session_claims())}"}


def _proxy_get(path: str, params: Dict[str, Any] | None = None, timeout: float = 30) -> Any:
    try:
        response = requests.get(
            f"{API_BASE_URL}{path}", params=params, timeout=timeout, headers=_internal_headers()
        )
    except requests.exceptions.RequestException as e:
        logger.error("Backend GET %s failed: %s", path, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    if response.status_code == 200:
        return jsonify(response.json())
    return jsonify({"error": response.text}), response.status_code


def _proxy_post(path: str, payload: Dict[str, Any], timeout: float = 60) -> Any:
    try:
        response = requests.post(
            f"{API_BASE_URL}{path}", json=payload, timeout=timeout, headers=_internal_headers()
        )
    except requests.exceptions.RequestException as e:
        logger.error("Backend POST %s failed: %s", path, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    if response.status_code == 200:
        return jsonify(response.json())
    return jsonify({"error": response.text}), response.status_code


def _proxy_patch(path: str, payload: Dict[str, Any], timeout: float = 30) -> Any:
    try:
        response = requests.patch(
            f"{API_BASE_URL}{path}", json=payload, timeout=timeout, headers=_internal_headers()
        )
    except requests.exceptions.RequestException as e:
        logger.error("Backend PATCH %s failed: %s", path, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    if response.status_code == 200:
        return jsonify(response.json())
    return jsonify({"error": response.text}), response.status_code


def _proxy_delete(path: str, params: Dict[str, Any] | None = None, timeout: float = 30) -> Any:
    try:
        response = requests.delete(
            f"{API_BASE_URL}{path}", params=params, timeout=timeout, headers=_internal_headers()
        )
    except requests.exceptions.RequestException as e:
        logger.error("Backend DELETE %s failed: %s", path, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    if response.status_code == 200:
        return jsonify(response.json())
    return jsonify({"error": response.text}), response.status_code


def _session_user_id() -> str:
    """Return the logged-in user id as a string for history/audit APIs."""
    return str(session["user_id"])


def _session_user_context() -> Dict[str, str]:
    """User context forwarded to the API for query audit logging."""
    return {
        "user_id": _session_user_id(),
        "user_name": session.get("user_name") or "",
        "user_email": session.get("user_email") or "",
    }


def _entra_sso_enabled() -> bool:
    from src import entra_auth

    return entra_auth.is_configured()


def _public_base_url() -> str:
    """Public URL of the UI (for OAuth redirect). Prefer PUBLIC_APP_URL behind proxies."""
    explicit = os.getenv("PUBLIC_APP_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return request.url_root.rstrip("/")


def _write_user_session(
    user: Dict[str, Any],
    *,
    provider: str,
    directory: Dict[str, Any] | None = None,
) -> None:
    session.permanent = True
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]
    session["avatar_hue"] = user["avatar_hue"]
    session["auth_provider"] = provider
    # Authoritative "as-of" time for the identity's directory claims. Group
    # membership captured below is only trusted for a bounded TTL measured from
    # here, so removing a user from an Entra group revokes group-gated connector
    # access once the window lapses (and re-login re-reads current claims).
    session["auth_time"] = int(time.time())
    # Entra directory context (for the connector platform). Empty for local login.
    directory = directory or {}
    session["tenant_id"] = directory.get("tenant_id") or ""
    session["object_id"] = directory.get("object_id") or ""
    session["groups"] = directory.get("groups") or []
    session["groups_complete"] = bool(directory.get("groups_complete", True))


def _admin_required():
    """Return an error response when the session user is not an admin, else None."""
    if session.get("user_role") != "admin":
        if request.path.startswith("/api/"):
            return jsonify({"error": "Admin role required", "code": "FORBIDDEN"}), 403
        return redirect(url_for("index"))
    return None


# ── Auth guard ───────────────────────────────────────────────────────────────

@app.before_request
def _require_login():
    """Block unauthenticated access.

    * Static files and public paths are always permitted.
    * API routes return 401 JSON (so the JS can react).
    * All other routes redirect to /login.
    """
    path = request.path
    if path.startswith(_PUBLIC_PREFIXES) or path in _PUBLIC_EXACT:
        return None
    # First-run: force admin setup before anything else is reachable.
    if _needs_first_run_setup():
        if path.startswith("/api/"):
            return jsonify({"error": "First-run setup required", "code": "SETUP_REQUIRED"}), 503
        return redirect(url_for("setup"))
    if "user_id" in session:
        return None
    # Unauthenticated
    if path.startswith("/api/"):
        return jsonify({"error": "Authentication required", "code": "UNAUTHENTICATED"}), 401
    return redirect(url_for("login", next=request.path))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def login():
    """Render the login page (GET) or process credentials (POST)."""
    if request.method == "GET":
        # Already logged in — skip the login page.
        if "user_id" in session:
            return redirect(request.args.get("next") or "/")
        return render_template(
            "login.html",
            error=None,
            entra_sso_enabled=_entra_sso_enabled(),
        )

    # POST: validate credentials
    from src.auth_db import get_user_by_email, verify_password, touch_last_active

    email    = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""

    error = None
    if not email or not password:
        error = "Email and password are required."
    else:
        try:
            user = get_user_by_email(email)
        except Exception as exc:  # noqa: BLE001
            logger.exception("login: auth DB lookup failed for %s", email)
            from src.auth_db import friendly_db_error

            error = friendly_db_error(exc)
            if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
                error = f"{error} ({exc})"
        else:
            if user is None or not verify_password(password, user["password_hash"]):
                error = "Invalid email or password."
            elif user["status"] != "active":
                error = "This account is disabled. Contact your administrator."
            else:
                _write_user_session(user, provider="local")
                touch_last_active(user["id"])
                logger.info("login: %s (%s) authenticated", user["email"], user["role"])
                return redirect(request.form.get("next") or "/")

    status = 503 if error and "unavailable" in error else 401
    return render_template(
        "login.html",
        error=error,
        entra_sso_enabled=_entra_sso_enabled(),
    ), status


@app.route("/setup", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def setup():
    """One-time first-run admin creation. Only reachable while no admin exists."""
    import hmac

    global _admin_bootstrapped
    if not _needs_first_run_setup():
        return redirect(url_for("login"))

    if request.method == "GET":
        # Ensure a token exists and is logged for the operator to read.
        _setup_bootstrap_token()
        return render_template("setup.html", error=None)

    from src.auth_db import create_first_admin, friendly_db_error

    # Out-of-band bootstrap token: blocks anonymous takeover of a fresh install.
    provided_token = request.form.get("setup_token") or ""
    if not hmac.compare_digest(provided_token, _setup_bootstrap_token()):
        logger.warning("setup: rejected first-run attempt with an invalid setup token")
        return render_template(
            "setup.html",
            error="Invalid setup token. Check the server logs (or set SETUP_BOOTSTRAP_TOKEN).",
        ), 403

    name     = (request.form.get("name") or "").strip()
    email    = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm  = request.form.get("confirm") or ""

    error = None
    if not name or not email or not password:
        error = "All fields are required."
    elif len(password) < 12:
        error = "Password must be at least 12 characters."
    elif password != confirm:
        error = "Passwords do not match."
    if error:
        return render_template("setup.html", error=error), 400

    try:
        user = create_first_admin(name, email, password)
    except RuntimeError:
        # An admin appeared concurrently — send them to login.
        _admin_bootstrapped = True
        return redirect(url_for("login"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("setup: failed to create first admin")
        return render_template("setup.html", error=friendly_db_error(exc)), 503

    _admin_bootstrapped = True
    _write_user_session(user, provider="local")
    logger.info("setup: first admin created (%s)", user["email"])
    return redirect("/")


@app.route("/auth/microsoft")
def microsoft_login():
    """Start Microsoft Entra ID OAuth authorization-code flow."""
    from src import entra_auth

    if not entra_auth.is_configured():
        return redirect(url_for("login"))

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_next"] = request.args.get("next") or "/"
    callback_uri = entra_auth.redirect_uri(_public_base_url())
    auth_url = entra_auth.build_auth_url(redirect_uri=callback_uri, state=state)
    return redirect(auth_url)


@app.route("/auth/microsoft/callback")
def microsoft_callback():
    """Complete Microsoft Entra ID sign-in and establish a Flask session."""
    from src import entra_auth
    from src.auth_db import get_or_create_sso_user, touch_last_active

    if not entra_auth.is_configured():
        return redirect(url_for("login"))

    ms_error = request.args.get("error_description") or request.args.get("error")
    if ms_error:
        return render_template(
            "login.html",
            error=f"Microsoft sign-in failed: {ms_error}",
            entra_sso_enabled=True,
        ), 401

    state = request.args.get("state")
    expected = session.pop("oauth_state", None)
    if not state or state != expected:
        return render_template(
            "login.html",
            error="Invalid sign-in state. Please try again.",
            entra_sso_enabled=True,
        ), 401

    code = request.args.get("code")
    if not code:
        return render_template(
            "login.html",
            error="Microsoft sign-in was cancelled.",
            entra_sso_enabled=True,
        ), 401

    next_url = session.pop("oauth_next", "/")
    callback_uri = entra_auth.redirect_uri(_public_base_url())
    result, err = entra_auth.exchange_code(code, redirect_uri=callback_uri)
    if err or not result:
        return render_template(
            "login.html",
            error=f"Microsoft sign-in failed: {err or 'unknown error'}",
            entra_sso_enabled=True,
        ), 401

    profile = entra_auth.profile_from_token_result(result)
    directory = entra_auth.directory_claims_from_token_result(result)
    if not profile["email"]:
        return render_template(
            "login.html",
            error="Your Microsoft account has no email address.",
            entra_sso_enabled=True,
        ), 401

    try:
        user = get_or_create_sso_user(profile["email"], profile["name"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("microsoft login: user provision failed for %s", profile["email"])
        error = "Sign-in is temporarily unavailable (database error)."
        if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
            error = f"{error} ({exc})"
        return render_template(
            "login.html",
            error=error,
            entra_sso_enabled=True,
        ), 503

    if user["status"] != "active":
        return render_template(
            "login.html",
            error="This account is disabled. Contact your administrator.",
            entra_sso_enabled=True,
        ), 401

    _write_user_session(user, provider="microsoft", directory=directory)
    touch_last_active(user["id"])
    logger.info("microsoft login: %s (%s) authenticated", user["email"], user["role"])
    return redirect(next_url)


@app.route("/logout", methods=["POST"])
def logout():
    """Clear the session. POST-only + CSRF-protected so it can't be triggered
    cross-site via a bare link/image. The topbar sign-out control in auth.js
    issues a token-bearing POST and then redirects."""
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    """Return the current session user (200) or 401 if not logged in."""
    if "user_id" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    from src.security.app_flags import (
        get_agent_tools_enabled_sync,
        get_connectors_enabled_sync,
    )

    return jsonify({
        "id":         session["user_id"],
        "name":       session["user_name"],
        "email":      session["user_email"],
        "role":       session["user_role"],
        "avatar_hue": session["avatar_hue"],
        # Surface flags the UI uses to gate connector surfaces.
        "is_entra":   bool(session.get("object_id")),
        "connectors_enabled": get_connectors_enabled_sync(),
        # Independent switch for agent-initiated tool calls (requires connectors
        # to also be enabled). Surfaced so the UI can reflect/gate the feature.
        "agent_tools_enabled": get_agent_tools_enabled_sync(),
    })


# ── User management routes (— served by Flask, not proxied) ──────────────────
# All mutate/list operations require an admin session (defense-in-depth on top
# of the FastAPI Principal checks for API-served routes).

@app.route("/api/users", methods=["GET"])
def users_list():
    guard = _admin_required()
    if guard:
        return guard
    from src.auth_db import list_users
    try:
        return jsonify(list_users())
    except Exception as exc:  # noqa: BLE001
        logger.exception("users_list failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/users", methods=["POST"])
def users_create():
    guard = _admin_required()
    if guard:
        return guard
    from src.auth_db import create_user, email_exists
    data = request.get_json() or {}
    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role     = data.get("role") or "viewer"
    if not name or not email or not password:
        return jsonify({"error": "name, email, and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if role not in ("admin", "editor", "viewer"):
        return jsonify({"error": "role must be admin, editor, or viewer"}), 400
    try:
        if email_exists(email):
            return jsonify({"error": "An account with this email already exists"}), 409
        user = create_user(name, email, password, role)
        logger.info("user created: %s (%s) by %s", email, role, session.get("user_email"))
        return jsonify(user), 201
    except Exception as exc:  # noqa: BLE001
        logger.exception("users_create failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/users/<int:user_id>/role", methods=["PATCH"])
def users_update_role(user_id: int):
    guard = _admin_required()
    if guard:
        return guard
    from src.auth_db import update_user_role
    data = request.get_json() or {}
    role = data.get("role") or ""
    if role not in ("admin", "editor", "viewer"):
        return jsonify({"error": "role must be admin, editor, or viewer"}), 400
    try:
        update_user_role(user_id, role)
        logger.info("user %s role → %s by %s", user_id, role, session.get("user_email"))
        return jsonify({"id": user_id, "role": role})
    except Exception as exc:  # noqa: BLE001
        logger.exception("users_update_role failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def users_delete(user_id: int):
    guard = _admin_required()
    if guard:
        return guard
    from src.auth_db import delete_user
    if user_id == session.get("user_id"):
        return jsonify({"error": "You cannot delete your own account"}), 400
    try:
        delete_user(user_id)
        logger.info("user %s deleted by %s", user_id, session.get("user_email"))
        return jsonify({"id": user_id, "deleted": True})
    except Exception as exc:  # noqa: BLE001
        logger.exception("users_delete failed")
        return jsonify({"error": str(exc)}), 500


# ── Settings prompts/model proxy (for per-prompt model assignment) ────────────

@app.route("/api/settings/prompts/<name>/model", methods=["PUT"])
def settings_set_prompt_model(name: str):
    guard = _admin_required()
    if guard:
        return guard
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/prompts/{name}/model",
            json=request.get_json() or {},
            timeout=10,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    """Public liveness probe. Reports only coarse status booleans — never
    upstream error text, DB host/name, or other infra identifiers, since this
    endpoint is unauthenticated."""
    from src.auth_db import check_connection

    try:
        response = requests.get(f"{API_BASE_URL}/health", timeout=5)
        backend_healthy = (
            response.status_code == 200
            and response.json().get("status") == "healthy"
        )
    except Exception:  # noqa: BLE001
        backend_healthy = False

    auth_ok, _auth_error = check_connection()  # error detail intentionally dropped
    ui_ok = backend_healthy and auth_ok
    return jsonify({
        "ui_status": "healthy" if ui_ok else "degraded",
        "backend_status": "healthy" if backend_healthy else "unhealthy",
        "auth_db_status": "healthy" if auth_ok else "unhealthy",
    })


# ----------------------------------------------------------------------
# Connections
# ----------------------------------------------------------------------
@app.route("/api/connections", methods=["GET"])
def list_connections():
    return _proxy_get("/api/connections", timeout=15)


@app.route("/api/connections/<source_key>", methods=["GET"])
def get_connection(source_key: str):
    return _proxy_get(f"/api/connections/{source_key}", timeout=15)


@app.route("/api/connections/<source_key>/refresh-metadata", methods=["POST"])
def refresh_metadata(source_key: str):
    return _proxy_post(f"/api/connections/{source_key}/refresh-metadata", payload={}, timeout=15)


@app.route("/api/connections/<source_key>/warm-cache", methods=["POST"])
def warm_cache(source_key: str):
    """Fire-and-forget metadata pre-warm for a connection."""
    return _proxy_post(f"/api/connections/{source_key}/warm-cache", payload={}, timeout=30)


# ----------------------------------------------------------------------
# Query / data exploration
# ----------------------------------------------------------------------
def _query_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Build a backend query payload with identity sourced from the session."""

    payload: Dict[str, Any] = {
        "question": (data.get("question") or "").strip(),
        "connection": data.get("connection"),
        "user_context": _session_user_context(),
    }
    for field in (
        "session_id",
        "limit",
        "temperature",
        "eval_analytics",
        "llm_timeout",
    ):
        if data.get(field) is not None:
            payload[field] = data[field]
    return payload


@app.route("/api/ask", methods=["POST"])
def ask_question():
    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    connection = data.get("connection")

    if not question:
        return jsonify({"error": "Question cannot be empty"}), 400
    if not connection:
        return jsonify({"error": "No connection selected"}), 400

    payload = _query_payload(data)
    return _proxy_post("/api/query", payload, timeout=120)


@app.route("/api/ask/stream", methods=["POST"])
def ask_question_stream():
    """Relay authenticated LangGraph progress without buffering."""

    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    connection = data.get("connection")
    if not question:
        return jsonify({"error": "Question cannot be empty"}), 400
    if not connection:
        return jsonify({"error": "No connection selected"}), 400

    try:
        upstream = requests.post(
            f"{API_BASE_URL}/api/query/stream",
            json=_query_payload(data),
            stream=True,
            timeout=(10, 300),
            headers={
                **_internal_headers(),
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Backend query stream failed: %s", exc)
        return jsonify({"error": f"Backend unavailable: {exc}"}), 503

    if upstream.status_code != 200:
        body = upstream.text
        status = upstream.status_code
        upstream.close()
        return jsonify({"error": body}), status

    def relay():
        try:
            for chunk in upstream.iter_content(chunk_size=64):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(relay()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/tables", methods=["GET"])
def get_tables():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_get("/api/tables", params={"connection": connection}, timeout=15)


@app.route("/api/tables-rich", methods=["GET"])
def get_tables_rich():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_get("/api/tables-rich", params={"connection": connection}, timeout=15)


@app.route("/api/schema/<table_name>", methods=["GET"])
def get_schema(table_name: str):
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_get(f"/api/schema/{table_name}", params={"connection": connection}, timeout=15)


# ----------------------------------------------------------------------
# Recent / pinned questions
# ----------------------------------------------------------------------
@app.route("/api/user/recent-questions", methods=["GET"])
def get_recent_questions():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"questions": []})
    return _proxy_get(
        "/api/user/recent-questions",
        params={
            "connection": connection,
            "user_id": _session_user_id(),
            "limit": request.args.get("limit", "15"),
        },
    )


@app.route("/api/user/pinned-questions", methods=["GET"])
def get_pinned_questions():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"questions": []})
    return _proxy_get(
        "/api/user/pinned-questions",
        params={
            "connection": connection,
            "user_id": _session_user_id(),
        },
    )


@app.route("/api/user/pin-question", methods=["POST"])
def pin_question():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/user/pin-question", data)


@app.route("/api/user/unpin-question", methods=["POST"])
def unpin_question():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/user/unpin-question", data)


@app.route("/api/user/history-log", methods=["GET"])
def get_history_log():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"entries": []})
    return _proxy_get(
        "/api/user/history-log",
        params={
            "connection": connection,
            "user_id": _session_user_id(),
            "limit": request.args.get("limit", "100"),
        },
    )


@app.route("/api/saved-analyses", methods=["GET"])
def list_saved_analyses():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"items": []})
    return _proxy_get(
        "/api/saved-analyses",
        params={
            "connection": connection,
            "user_id": _session_user_id(),
            "limit": request.args.get("limit", "50"),
        },
    )


@app.route("/api/saved-analyses", methods=["POST"])
def save_analysis():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/saved-analyses", data, timeout=120)


@app.route("/api/saved-analyses/<saved_id>", methods=["GET"])
def get_saved_analysis(saved_id: str):
    return _proxy_get(
        f"/api/saved-analyses/{saved_id}",
        params={"user_id": _session_user_id()},
        timeout=30,
    )


@app.route("/api/saved-analyses/<saved_id>", methods=["PATCH"])
def update_saved_analysis(saved_id: str):
    data = request.get_json() or {}
    data["user_id"] = _session_user_id()
    return _proxy_patch(f"/api/saved-analyses/{saved_id}", data, timeout=30)


@app.route("/api/saved-analyses/<saved_id>", methods=["DELETE"])
def delete_saved_analysis(saved_id: str):
    return _proxy_delete(
        f"/api/saved-analyses/{saved_id}",
        params={"user_id": _session_user_id()},
        timeout=30,
    )


# ----------------------------------------------------------------------
# Insights / charts / profile
# ----------------------------------------------------------------------
@app.route("/api/generate-chart", methods=["POST"])
def generate_chart():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    # Stamp the authenticated user so the server-side result cache is scoped
    # to this user (never trust a client-supplied id).
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/generate-chart", data)


@app.route("/api/generate-insights", methods=["POST"])
def generate_insights():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/generate-insights", data)


@app.route("/api/generate-insights/stream", methods=["POST"])
def generate_insights_stream():
    """Forward a Server-Sent Events stream from the API to the browser.

    `requests` with stream=True keeps the connection open; we relay raw
    bytes through a Flask streaming response so SSE framing is preserved.
    """
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    data["user_id"] = _session_user_id()

    upstream = requests.post(
        f"{API_BASE_URL}/api/generate-insights/stream",
        json=data,
        stream=True,
        timeout=120,
        headers=_internal_headers(),
    )

    if upstream.status_code != 200:
        # Surface the upstream error verbatim; don't try to re-stream.
        body = upstream.text
        upstream.close()
        return jsonify({"error": body}), upstream.status_code

    def relay():
        try:
            # Small chunk size so the first byte arrives ASAP.
            for chunk in upstream.iter_content(chunk_size=64):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(relay()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/generate-profile", methods=["POST"])
def generate_profile():
    data = request.get_json() or {}
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/generate-profile", data, timeout=120)


@app.route("/api/enhance-chart", methods=["POST"])
def enhance_chart():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_post("/api/enhance-chart", data)


@app.route("/api/edit-chart", methods=["POST"])
def edit_chart():
    """Proxy for the chat-driven chart editor.

    Forwards the user's instruction + current ECharts config to the API.
    The API call hits the LLM, so allow a generous timeout.
    """
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    if not (data.get("instruction") or "").strip():
        return jsonify({"error": "`instruction` is required"}), 400
    return _proxy_post("/api/edit-chart", data, timeout=120)


# ----------------------------------------------------------------------
# Autocomplete (Tier 2 catalog + Tier 3 LLM)
# ----------------------------------------------------------------------
@app.route("/api/knowledge-questions", methods=["GET"])
def get_knowledge_questions():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_get(
        "/api/knowledge-questions",
        params={"connection": connection},
        timeout=15,
    )


@app.route("/api/knowledge-columns", methods=["GET"])
def get_knowledge_columns():
    connection = request.args.get("connection")
    if not connection:
        return jsonify({"error": "No connection selected"}), 400
    params = {"connection": connection}
    table = request.args.get("table")
    if table:
        params["table"] = table
    return _proxy_get(
        "/api/knowledge-columns",
        params=params,
        timeout=15,
    )


@app.route("/api/suggest-questions", methods=["POST"])
def suggest_questions():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_post("/api/suggest-questions", data, timeout=15)


# ----------------------------------------------------------------------
# Settings (prompts + models + app-info)
# ----------------------------------------------------------------------

@app.route("/api/settings/prompts", methods=["GET"])
def settings_list_prompts():
    return _proxy_get("/api/settings/prompts", timeout=10)


@app.route("/api/settings/prompts/reload", methods=["POST"])
def settings_reload_prompts():
    guard = _admin_required()
    if guard:
        return guard
    return _proxy_post("/api/settings/prompts/reload", payload={}, timeout=10)


@app.route("/api/settings/prompts/<name>", methods=["GET"])
def settings_get_prompt(name: str):
    return _proxy_get(f"/api/settings/prompts/{name}", timeout=10)


@app.route("/api/settings/prompts/<name>", methods=["PUT"])
def settings_save_prompt(name: str):
    guard = _admin_required()
    if guard:
        return guard
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/prompts/{name}",
            json=request.get_json() or {},
            timeout=10,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/settings/prompts/<name>", methods=["DELETE"])
def settings_reset_prompt(name: str):
    guard = _admin_required()
    if guard:
        return guard
    try:
        resp = requests.delete(
            f"{API_BASE_URL}/api/settings/prompts/{name}",
            timeout=10,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/settings/models", methods=["GET"])
def settings_list_models():
    return _proxy_get("/api/settings/models", timeout=10)


@app.route("/api/settings/models/health", methods=["GET"])
def settings_models_health():
    # ?refresh=true re-probes every provider, which can take a while.
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    return _proxy_get(
        "/api/settings/models/health",
        params={"refresh": "true"} if refresh else None,
        timeout=120,
    )


@app.route("/api/settings/models/active", methods=["GET"])
def settings_get_active_model():
    return _proxy_get("/api/settings/models/active", timeout=10)


@app.route("/api/settings/models/active", methods=["PUT"])
def settings_set_active_model():
    guard = _admin_required()
    if guard:
        return guard
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/models/active",
            json=request.get_json() or {},
            timeout=10,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/settings/app-info", methods=["GET"])
def settings_app_info():
    return _proxy_get("/api/settings/app-info", timeout=10)


@app.route("/api/settings/runtime", methods=["GET"])
def settings_get_runtime():
    return _proxy_get("/api/settings/runtime", timeout=10)


@app.route("/api/settings/runtime", methods=["PUT"])
def settings_update_runtime():
    guard = _admin_required()
    if guard:
        return guard
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/runtime",
            json=request.get_json() or {},
            timeout=10,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


# ----------------------------------------------------------------------
# MCP catalog management (generic catch-all proxy)
# Forwards all /api/mcp/* requests verbatim to the FastAPI backend.
# ----------------------------------------------------------------------

def _forward(api_path: str, *, timeout: float = 30) -> Any:
    """Forward the current request (method/json/query) to the FastAPI backend."""
    started_at = time.monotonic()
    is_map_tile = api_path.startswith("/api/map-tiles/")
    target = f"{API_BASE_URL}{api_path}"
    qs = request.query_string.decode()
    if qs:
        target = f"{target}?{qs}"
    try:
        resp = requests.request(
            method=request.method,
            url=target,
            json=request.get_json(silent=True),
            timeout=timeout,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        logger.error("proxy %s %s failed: %s", request.method, target, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    try:
        return jsonify(resp.json()), resp.status_code
    except Exception:  # noqa: BLE001
        response_headers = {}
        if is_map_tile:
            for header_name in ("Cache-Control", "ETag", "Last-Modified", "Server-Timing"):
                header_value = resp.headers.get(header_name)
                if header_value:
                    response_headers[header_name] = header_value
            logger.info(
                "osm_tile_forward status=%d elapsed_ms=%d bytes=%d",
                resp.status_code,
                round((time.monotonic() - started_at) * 1000),
                len(resp.content),
            )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/json"),
            headers=response_headers,
        )


@app.route("/api/chart-capabilities", methods=["GET"])
def chart_capabilities_proxy():
    """Expose non-sensitive chart feature flags to the browser."""
    return _forward("/api/chart-capabilities", timeout=10)


@app.route("/api/map-tiles/<int:z>/<int:x>/<int:y>", methods=["GET"])
def map_tiles_proxy(z: int, x: int, y: int):
    """Keep configured map-tile credentials on the API side of the proxy."""
    return _forward(f"/api/map-tiles/{z}/{x}/{y}", timeout=30)


@app.route("/api/map-tiles/<layer_id>/<int:z>/<int:x>/<int:y>", methods=["GET"])
def configured_map_tiles_proxy(layer_id: str, z: int, x: int, y: int):
    """Proxy a server-approved named map layer without exposing its credentials."""
    return _forward(f"/api/map-tiles/{layer_id}/{z}/{x}/{y}", timeout=30)


@app.route("/api/map-search", methods=["GET"])
def map_search_proxy():
    """Proxy managed place search without exposing map-provider credentials."""
    return _forward("/api/map-search", timeout=15)


@app.route("/api/mcp/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
def mcp_proxy(subpath: str):
    """Generic proxy for all /api/mcp/* routes (admin-only settings surface)."""
    guard = _admin_required()
    if guard:
        return guard
    return _forward(f"/api/mcp/{subpath}")


# ----------------------------------------------------------------------
# Connector / integration platform proxies
# ----------------------------------------------------------------------

@app.route("/api/connectors", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/api/connectors/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def connectors_admin_proxy(subpath: str = ""):
    """Admin connector registry (gated in Flask AND enforced by the API)."""
    guard = _admin_required()
    if guard:
        return guard
    tail = f"/{subpath}" if subpath else ""
    return _forward(f"/api/connectors{tail}")


@app.route("/api/me/connections", methods=["GET"])
@app.route("/api/me/connections/<path:subpath>", methods=["GET", "POST", "DELETE"])
def me_connections_proxy(subpath: str = ""):
    """Per-user connection management (any authenticated user)."""
    tail = f"/{subpath}" if subpath else ""
    return _forward(f"/api/me/connections{tail}", timeout=45)


@app.route("/api/actions/<path:subpath>", methods=["POST"])
def actions_proxy(subpath: str):
    """Server-authorized action proposal/execution (any authenticated user)."""
    return _forward(f"/api/actions/{subpath}", timeout=60)


# ----------------------------------------------------------------------
# OAuth connect flow (browser round-trip for per-user connector consent)
# ----------------------------------------------------------------------

@app.route("/integrations/<connector_id>/connect", methods=["GET"])
def integration_connect(connector_id: str):
    """Kick off per-user OAuth consent for a connector, then redirect to provider."""
    redirect_uri = f"{_public_base_url()}/integrations/callback"
    try:
        resp = requests.post(
            f"{API_BASE_URL}/api/me/connections/{connector_id}/authorize",
            json={"redirect_uri": redirect_uri},
            timeout=30,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return _integration_result_redirect("error", str(e))
    if resp.status_code != 200:
        detail = _safe_detail(resp)
        return _integration_result_redirect("error", detail)
    authorize_url = resp.json().get("authorize_url")
    if not authorize_url:
        return _integration_result_redirect("error", "No authorize URL returned")
    return redirect(authorize_url)


@app.route("/integrations/callback", methods=["GET"])
def integration_callback():
    """Complete OAuth consent: exchange the code via the API, then bounce back."""
    provider_error = request.args.get("error_description") or request.args.get("error")
    if provider_error:
        return _integration_result_redirect("error", provider_error)
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _integration_result_redirect("error", "Missing code/state")
    try:
        resp = requests.post(
            f"{API_BASE_URL}/api/me/connections/oauth/callback",
            json={"code": code, "state": state},
            timeout=30,
            headers=_internal_headers(),
        )
    except requests.exceptions.RequestException as e:
        return _integration_result_redirect("error", str(e))
    if resp.status_code != 200:
        return _integration_result_redirect("error", _safe_detail(resp))
    return _integration_result_redirect("connected", "")


def _safe_detail(resp) -> str:
    try:
        return str(resp.json().get("detail") or resp.json().get("error") or resp.status_code)
    except Exception:  # noqa: BLE001
        return str(resp.status_code)


def _integration_result_redirect(status: str, message: str) -> Any:
    from urllib.parse import quote

    return redirect(f"/?connector_result={quote(status)}&connector_msg={quote(message or '')}")


# ----------------------------------------------------------------------
# Feedback / history
# ----------------------------------------------------------------------
@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    data = request.get_json() or {}
    data["user_id"] = _session_user_id()
    return _proxy_post("/api/feedback", data)


@app.route("/api/conversation/<session_id>", methods=["GET"])
def get_conversation_history(session_id: str):
    return _proxy_get(
        f"/api/conversation/{session_id}",
        params={
            "include_insights": request.args.get("include_insights", "true"),
            "user_id": _session_user_id(),
        },
        timeout=30,
    )


if __name__ == "__main__":
    port = int(os.getenv("UI_PORT", "8501"))
    app.run(host="0.0.0.0", port=port, debug=True)
