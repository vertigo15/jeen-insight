"""Flask UI for Jeen Insights.

Acts as a thin pass-through to the FastAPI backend. The browser sends a
`connection` (source_key) along with every data-related request; this UI
forwards it on without inspecting it.

Authentication is handled here in the Flask layer using a signed session
cookie.  The FastAPI backend runs internally and is not directly exposed to
browser users; it has no auth of its own.
"""

from __future__ import annotations

import logging
import os
import secrets
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = (
    os.getenv("FLASK_SECRET_KEY")
    or os.getenv("AUTH_SECRET")
    or "jeen-insights-change-me-in-production"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 86_400  # 24 h

API_BASE_URL = os.getenv("API_BASE_URL", "http://jeen-insights-api:8000")

# Paths that never require a login check.
_PUBLIC_PREFIXES = ("/static/", "/favicon")
_PUBLIC_EXACT    = {
    "/login",
    "/logout",
    "/health",
    "/auth/microsoft",
    "/auth/microsoft/callback",
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _proxy_get(path: str, params: Dict[str, Any] | None = None, timeout: float = 30) -> Any:
    try:
        response = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        logger.error("Backend GET %s failed: %s", path, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    if response.status_code == 200:
        return jsonify(response.json())
    return jsonify({"error": response.text}), response.status_code


def _proxy_post(path: str, payload: Dict[str, Any], timeout: float = 60) -> Any:
    try:
        response = requests.post(f"{API_BASE_URL}{path}", json=payload, timeout=timeout)
    except requests.exceptions.RequestException as e:
        logger.error("Backend POST %s failed: %s", path, e)
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


def _write_user_session(user: Dict[str, Any], *, provider: str) -> None:
    session.permanent = True
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]
    session["avatar_hue"] = user["avatar_hue"]
    session["auth_provider"] = provider


# ── Auth guard ───────────────────────────────────────────────────────────────

@app.before_request
def _require_login():
    """Block unauthenticated access.

    * Static files and public paths are always permitted.
    * API routes return 401 JSON (so the JS can react).
    * All other routes redirect to /login.
    """
    path = request.path
    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return None
    if "user_id" in session:
        return None
    # Unauthenticated
    if path.startswith("/api/"):
        return jsonify({"error": "Authentication required", "code": "UNAUTHENTICATED"}), 401
    return redirect(url_for("login", next=request.path))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
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

    _write_user_session(user, provider="microsoft")
    touch_last_active(user["id"])
    logger.info("microsoft login: %s (%s) authenticated", user["email"], user["role"])
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    """Return the current session user (200) or 401 if not logged in."""
    if "user_id" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({
        "id":         session["user_id"],
        "name":       session["user_name"],
        "email":      session["user_email"],
        "role":       session["user_role"],
        "avatar_hue": session["avatar_hue"],
    })


# ── User management routes (— served by Flask, not proxied) ──────────────────

@app.route("/api/users", methods=["GET"])
def users_list():
    from src.auth_db import list_users
    try:
        return jsonify(list_users())
    except Exception as exc:  # noqa: BLE001
        logger.exception("users_list failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/users", methods=["POST"])
def users_create():
    from src.auth_db import create_user, email_exists
    data = request.get_json() or {}
    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role     = data.get("role") or "viewer"
    if not name or not email or not password:
        return jsonify({"error": "name, email, and password are required"}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400
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
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/prompts/{name}/model",
            json=request.get_json() or {},
            timeout=10,
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
    from src.auth_db import check_connection

    try:
        response = requests.get(f"{API_BASE_URL}/health", timeout=5)
        backend_status = response.json() if response.status_code == 200 else {"status": "unhealthy"}
    except Exception as e:  # noqa: BLE001
        backend_status = {"status": "unhealthy", "error": str(e)}

    auth_ok, auth_error = check_connection()
    auth_status = {"status": "healthy" if auth_ok else "unhealthy"}
    if auth_error:
        auth_status["error"] = auth_error

    ui_ok = backend_status.get("status") == "healthy" and auth_ok
    return jsonify({
        "ui_status": "healthy" if ui_ok else "degraded",
        "backend_status": backend_status,
        "auth_db_status": auth_status,
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
@app.route("/api/ask", methods=["POST"])
def ask_question():
    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    connection = data.get("connection")
    session_id = data.get("session_id")

    if not question:
        return jsonify({"error": "Question cannot be empty"}), 400
    if not connection:
        return jsonify({"error": "No connection selected"}), 400

    payload: Dict[str, Any] = {
        "question": question,
        "connection": connection,
        "user_context": _session_user_context(),
    }
    if session_id:
        payload["session_id"] = session_id
    # Optional user-preferences overrides. The API enforces bounds; we just
    # forward the values verbatim if the client sent them.
    if data.get("limit") is not None:
        payload["limit"] = data["limit"]
    if data.get("temperature") is not None:
        payload["temperature"] = data["temperature"]
    return _proxy_post("/api/query", payload, timeout=120)


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


# ----------------------------------------------------------------------
# Insights / charts / profile
# ----------------------------------------------------------------------
@app.route("/api/generate-chart", methods=["POST"])
def generate_chart():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
    return _proxy_post("/api/generate-chart", data)


@app.route("/api/generate-insights", methods=["POST"])
def generate_insights():
    data = request.get_json() or {}
    if not data.get("connection"):
        return jsonify({"error": "No connection selected"}), 400
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

    upstream = requests.post(
        f"{API_BASE_URL}/api/generate-insights/stream",
        json=data,
        stream=True,
        timeout=120,
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
    return _proxy_post("/api/settings/prompts/reload", payload={}, timeout=10)


@app.route("/api/settings/prompts/<name>", methods=["GET"])
def settings_get_prompt(name: str):
    return _proxy_get(f"/api/settings/prompts/{name}", timeout=10)


@app.route("/api/settings/prompts/<name>", methods=["PUT"])
def settings_save_prompt(name: str):
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/prompts/{name}",
            json=request.get_json() or {},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/settings/prompts/<name>", methods=["DELETE"])
def settings_reset_prompt(name: str):
    try:
        resp = requests.delete(
            f"{API_BASE_URL}/api/settings/prompts/{name}",
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/settings/models", methods=["GET"])
def settings_list_models():
    return _proxy_get("/api/settings/models", timeout=10)


@app.route("/api/settings/models/active", methods=["GET"])
def settings_get_active_model():
    return _proxy_get("/api/settings/models/active", timeout=10)


@app.route("/api/settings/models/active", methods=["PUT"])
def settings_set_active_model():
    try:
        resp = requests.put(
            f"{API_BASE_URL}/api/settings/models/active",
            json=request.get_json() or {},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/settings/app-info", methods=["GET"])
def settings_app_info():
    return _proxy_get("/api/settings/app-info", timeout=10)


# ----------------------------------------------------------------------
# MCP catalog management (generic catch-all proxy)
# Forwards all /api/mcp/* requests verbatim to the FastAPI backend.
# ----------------------------------------------------------------------

@app.route("/api/mcp/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
def mcp_proxy(subpath: str):
    """Generic proxy for all /api/mcp/* routes."""
    target = f"{API_BASE_URL}/api/mcp/{subpath}"
    qs     = request.query_string.decode()
    if qs:
        target = f"{target}?{qs}"
    try:
        resp = requests.request(
            method  = request.method,
            url     = target,
            json    = request.get_json(silent=True),
            timeout = 30,
        )
    except requests.exceptions.RequestException as e:
        logger.error("MCP proxy %s %s failed: %s", request.method, target, e)
        return jsonify({"error": f"Backend unavailable: {e}"}), 503
    try:
        return jsonify(resp.json()), resp.status_code
    except Exception:  # noqa: BLE001
        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get("Content-Type", "application/json"))


# ----------------------------------------------------------------------
# Feedback / history
# ----------------------------------------------------------------------
@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    return _proxy_post("/api/feedback", request.get_json() or {})


@app.route("/api/conversation/<session_id>", methods=["GET"])
def get_conversation_history(session_id: str):
    return _proxy_get(f"/api/conversation/{session_id}", timeout=30)


if __name__ == "__main__":
    port = int(os.getenv("UI_PORT", "8501"))
    app.run(host="0.0.0.0", port=port, debug=True)
