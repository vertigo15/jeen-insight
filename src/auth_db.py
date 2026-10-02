"""Synchronous auth DB helpers for the Flask UI layer.

Uses psycopg (v3) sync API so it works cleanly inside Flask's synchronous
request handlers without needing an asyncio event-loop wrapper.

All functions open a short-lived connection and close it on return, which
is fine for the relatively low request rate of a settings/admin UI.
"""
from __future__ import annotations

import os
import secrets
from typing import Any, Dict, List, Optional

import bcrypt
import psycopg


# Insights authorizes from insights_user_app_roles. auth_users.role is the
# Schema Modeler role on the shared account and is not an Insights permission.
APP_ROLES = ("admin", "editor", "viewer")
ROLE_APPS = ("insights", "metadata")


# ── Connection ────────────────────────────────────────────────────────────────

def _connect():
    """Open a psycopg3 sync connection using the standard metadata-DB env vars."""
    missing = [
        key for key in (
            "METADATA_DB_HOST",
            "METADATA_DB_NAME",
            "METADATA_DB_USER",
            "METADATA_DB_PASSWORD",
        )
        if not os.environ.get(key)
    ]
    if missing:
        raise RuntimeError(
            "Missing metadata DB env vars for UI auth: "
            + ", ".join(missing)
        )

    ssl = os.environ.get("METADATA_DB_SSL", "true").strip().lower() in ("1", "true", "yes")
    return psycopg.connect(
        host=os.environ["METADATA_DB_HOST"],
        port=int(os.environ.get("METADATA_DB_PORT", "5432")),
        dbname=os.environ["METADATA_DB_NAME"],
        user=os.environ["METADATA_DB_USER"],
        password=os.environ["METADATA_DB_PASSWORD"],
        sslmode="require" if ssl else "prefer",
    )


def friendly_db_error_key(exc: Exception) -> str:
    """Map a DB exception to the catalog key of a user-facing login message.

    Returns a key under ``login.errors.*`` so the Flask layer can render it in
    the request's UI language (``src.i18n.translate``).
    """
    msg = str(exc).lower()
    if any(
        token in msg
        for token in (
            "timeout",
            "timed out",
            "connection refused",
            "could not connect",
            "closed the connection",
            "server closed the connection",
            "network is unreachable",
        )
    ):
        return "login.errors.dbUnreachable"
    if "password authentication failed" in msg:
        return "login.errors.dbAuthFailed"
    if "auth_users" in msg and "does not exist" in msg:
        return "login.errors.dbTablesMissing"
    return "login.errors.dbUnavailable"


def friendly_db_error(exc: Exception) -> str:
    """English user-facing message for a DB exception (operator/health paths)."""
    from src.i18n import translate

    return translate("en", friendly_db_error_key(exc))


def check_connection() -> tuple[bool, str | None]:
    """Return (ok, error_message) for auth DB connectivity."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, friendly_db_error(exc)


# ── Queries ───────────────────────────────────────────────────────────────────

def normalize_app_role(role: Optional[str]) -> str:
    """Map a stored role onto the three roles both applications use.

    Schema Modeler's legacy ``user`` role is an editor. Anything else is a
    viewer, so an unknown value never grants access.
    """
    if role in APP_ROLES:
        return role
    if role == "user":
        return "editor"
    return "viewer"


def _account_from_row(row, *, with_password: bool) -> Dict[str, Any]:
    """Map a login SELECT row. Column 4 is the Insights role; the last column is auth_users.role."""
    account = {
        "id":            row[0],
        "name":          row[1],
        "email":         row[2],
        "role":          normalize_app_role(row[4]),
        "status":        row[5],
        "avatar_hue":    row[6],
        "last_active_at": row[7].isoformat() if row[7] else None,
        "created_at":    row[8].isoformat() if row[8] else None,
        "locale":        row[9],
        "date_format":   row[10],
        "metadata_role": normalize_app_role(row[11]),
    }
    if with_password:
        account["password_hash"] = row[3]
    return account


def _listed_user(row) -> Dict[str, Any]:
    return {
        "id":             row[0],
        "name":           row[1],
        "email":          row[2],
        "role":           normalize_app_role(row[3]),
        "status":         row[4],
        "avatar_hue":     row[5],
        "created_at":     row[6].isoformat() if row[6] else None,
        "last_active_at": row[7].isoformat() if row[7] else None,
        "locale":         row[8],
        "metadata_role":  normalize_app_role(row[9]),
    }


def _insert_insights_role(conn, user_id: int, role: str) -> None:
    conn.execute(
        """
        INSERT INTO insights_user_app_roles (user_id, app, role)
        VALUES (%s, 'insights', %s)
        ON CONFLICT (user_id, app) DO UPDATE
            SET role = EXCLUDED.role, updated_at = NOW()
        """,
        (user_id, role),
    )


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Return the account for *email*, or ``None``.

    ``role`` is the Insights role. A person with no Insights row (created in
    Metadata after the split) is a viewer here. ``metadata_role`` is
    ``auth_users.role``, which Schema Modeler still enforces.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.name, u.email, u.password_hash,
                   COALESCE(ir.role, 'viewer'),
                   u.status, u.avatar_hue, u.last_active_at, u.created_at,
                   u.locale, u.date_format, u.role
            FROM auth_users u
            LEFT JOIN insights_user_app_roles ir
                   ON ir.user_id = u.id AND ir.app = 'insights'
            WHERE u.email = %s
            LIMIT 1
            """,
            (email,),
        ).fetchone()
    if not row:
        return None
    return _account_from_row(row, with_password=True)


def get_user_preferences(user_id: int) -> Optional[Dict[str, Any]]:
    """Return the account preferences refreshed by the parallel startup request."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT date_format FROM auth_users WHERE id = %s LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {"date_format": row[0]}


def verify_password(plain: str, hashed: str) -> bool:
    """Return True when *plain* matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def touch_last_active(user_id: int) -> None:
    """Update last_active_at to NOW() for *user_id* and record a ``login`` event
    in the usage ledger (migration 036) in the same transaction.

    The ledger insert runs inside a savepoint so a missing table (migration not
    yet applied) never costs the ``last_active_at`` update.
    """
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE auth_users SET last_active_at = NOW() WHERE id = %s",
                (user_id,),
            )
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO insights_usage_events (event_type, user_id) "
                        "VALUES ('login', %s)",
                        (str(user_id),),
                    )
            except Exception:  # noqa: BLE001 — analytics is best-effort
                pass
            conn.commit()
    except Exception:  # noqa: BLE001
        pass  # non-critical; never block login


def list_users() -> List[Dict[str, Any]]:
    """Return every account, ordered by id.

    ``role`` is the Insights role. ``metadata_role`` is the Schema Modeler role
    stored on ``auth_users.role``.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.name, u.email,
                   COALESCE(ir.role, 'viewer'),
                   u.status, u.avatar_hue, u.created_at, u.last_active_at, u.locale,
                   u.role
            FROM auth_users u
            LEFT JOIN insights_user_app_roles ir
                   ON ir.user_id = u.id AND ir.app = 'insights'
            ORDER BY u.id
            """
        ).fetchall()
    return [_listed_user(r) for r in rows]


def set_user_locale(user_id: int, locale: str) -> None:
    """Persist the account's UI language (a shipped BCP 47 tag; see src.i18n)."""
    with _connect() as conn:
        conn.execute("UPDATE auth_users SET locale = %s WHERE id = %s", (locale, user_id))
        conn.commit()


def set_user_date_format(user_id: int, date_format: str) -> bool:
    """Persist the account's result-table calendar date format.

    Returns ``False`` if the authenticated account was deleted concurrently.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE auth_users SET date_format = %s WHERE id = %s",
            (date_format, user_id),
        )
        conn.commit()
        return cursor.rowcount == 1


def create_user(
    name: str,
    email: str,
    password: str,
    role: str = "viewer",
    metadata_role: str = "viewer",
) -> Dict[str, Any]:
    """Insert a shared account and its Insights role.

    ``role`` is the Insights role. ``metadata_role`` is written to
    ``auth_users.role`` for Schema Modeler and defaults to viewer, so a new
    Insights member is not given the same authority in Metadata.
    """
    insights_role = normalize_app_role(role) if role in APP_ROLES else "viewer"
    metadata = metadata_role if metadata_role in APP_ROLES else "viewer"
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")
    # Deterministic hue so avatars are stable (0–359).
    avatar_hue = abs(hash(email)) % 360
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO auth_users (name, email, password_hash, role, status, avatar_hue)
            VALUES (%s, %s, %s, %s, 'active', %s)
            RETURNING id, name, email, status, avatar_hue, created_at
            """,
            (name, email, hashed, metadata, avatar_hue),
        ).fetchone()
        _insert_insights_role(conn, row[0], insights_role)
        conn.commit()
    return {
        "id":             row[0],
        "name":           row[1],
        "email":          row[2],
        "role":           insights_role,
        "metadata_role":  metadata,
        "status":         row[3],
        "avatar_hue":     row[4],
        "created_at":     row[5].isoformat() if row[5] else None,
    }


def get_or_create_sso_user(email: str, name: str, *, role: str = "viewer") -> Dict[str, Any]:
    """Return an existing user or JIT-provision one for Microsoft SSO.

    The supplied role is the Insights role. A new account is a Metadata viewer.
    """
    normalized = email.strip().lower()
    existing = get_user_by_email(normalized)
    if existing:
        return existing
    # Unusable local password — SSO users sign in via Entra only.
    return create_user(name, normalized, secrets.token_urlsafe(32), role=role)


def update_user_role(user_id: int, role: str, app: str = "insights") -> None:
    """Change one application's role for *user_id*.

    ``insights`` updates ``insights_user_app_roles`` only. ``metadata`` updates
    ``auth_users.role``, which Schema Modeler reads. Neither write touches the
    other application's role.
    """
    if app not in ROLE_APPS:
        raise ValueError(f"app must be one of: {', '.join(ROLE_APPS)}")
    if role not in APP_ROLES:
        raise ValueError(f"role must be one of: {', '.join(APP_ROLES)}")
    with _connect() as conn:
        if app == "insights":
            _insert_insights_role(conn, user_id, role)
        else:
            conn.execute(
                "UPDATE auth_users SET role = %s WHERE id = %s",
                (role, user_id),
            )
        conn.commit()


def delete_user(user_id: int) -> None:
    """Hard-delete *user_id* from auth_users."""
    with _connect() as conn:
        conn.execute("DELETE FROM auth_users WHERE id = %s", (user_id,))
        conn.commit()


def email_exists(email: str) -> bool:
    """Return True when *email* is already registered."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM auth_users WHERE email = %s LIMIT 1", (email,)
        ).fetchone()
    return row is not None


_INSIGHTS_ADMIN_SQL = """
    SELECT 1
    FROM auth_users u
    JOIN insights_user_app_roles r
      ON r.user_id = u.id AND r.app = 'insights'
    WHERE r.role = 'admin' AND u.status = 'active'
    LIMIT 1
"""


def active_admin_exists() -> bool:
    """Return True when at least one usable (active) Insights admin exists.

    A Metadata admin does not count. Used to drive the first-run admin setup
    screen. Fails closed (returns True) on DB errors so a transient outage
    can't expose the unauthenticated setup flow — the operator will just see
    the normal login error instead.
    """
    try:
        with _connect() as conn:
            row = conn.execute(_INSIGHTS_ADMIN_SQL).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return True


# Fixed advisory-lock key that serializes concurrent first-run admin creation
# across sessions/processes. Any constant works; it just needs to be stable.
_SETUP_ADVISORY_LOCK_KEY = 0x4A45454E5F535550  # "JEEN_SUP"


def create_first_admin(name: str, email: str, password: str) -> Dict[str, Any]:
    """Create the very first admin. Refuses if any active admin already exists.

    Concurrency-safe: a transaction-scoped Postgres advisory lock serializes
    concurrent setup requests so only ONE admin can ever be created via this
    bootstrap path — the re-check inside the lock is authoritative even under a
    burst of simultaneous /setup POSTs.
    """
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")
    avatar_hue = abs(hash(email)) % 360
    with _connect() as conn:
        with conn.transaction():
            # Serialize with every other setup attempt for the lock's lifetime
            # (released automatically at transaction end).
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SETUP_ADVISORY_LOCK_KEY,))
            exists = conn.execute(_INSIGHTS_ADMIN_SQL).fetchone()
            if exists:
                raise RuntimeError("An admin account already exists")
            # Metadata role stays viewer. This account is an Insights admin only.
            row = conn.execute(
                """
                INSERT INTO auth_users (name, email, password_hash, role, status, avatar_hue)
                VALUES (%s, %s, %s, 'viewer', 'active', %s)
                RETURNING id, name, email, status, avatar_hue, created_at
                """,
                (name, email, hashed, avatar_hue),
            ).fetchone()
            _insert_insights_role(conn, row[0], "admin")
    return {
        "id":            row[0],
        "name":          row[1],
        "email":         row[2],
        "role":          "admin",
        "metadata_role": "viewer",
        "status":        row[3],
        "avatar_hue":    row[4],
        "created_at":    row[5].isoformat() if row[5] else None,
    }
