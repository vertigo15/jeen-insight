"""Synchronous auth DB helpers for the Flask UI layer.

Uses psycopg (v3) sync API so it works cleanly inside Flask's synchronous
request handlers without needing an asyncio event-loop wrapper.

All functions open a short-lived connection and close it on return, which
is fine for the relatively low request rate of a settings/admin UI.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import bcrypt
import psycopg


# ── Connection ────────────────────────────────────────────────────────────────

def _connect():
    """Open a psycopg3 sync connection using the standard metadata-DB env vars."""
    ssl = os.environ.get("METADATA_DB_SSL", "true").strip().lower() in ("1", "true", "yes")
    return psycopg.connect(
        host=os.environ["METADATA_DB_HOST"],
        port=int(os.environ.get("METADATA_DB_PORT", "5432")),
        dbname=os.environ["METADATA_DB_NAME"],
        user=os.environ["METADATA_DB_USER"],
        password=os.environ["METADATA_DB_PASSWORD"],
        sslmode="require" if ssl else "prefer",
    )


# ── Queries ───────────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Return the auth_users row for *email*, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, name, email, password_hash, role, status, avatar_hue,
                   last_active_at, created_at
            FROM auth_users WHERE email = %s LIMIT 1
            """,
            (email,),
        ).fetchone()
    if not row:
        return None
    return {
        "id":            row[0],
        "name":          row[1],
        "email":         row[2],
        "password_hash": row[3],
        "role":          row[4] or "viewer",
        "status":        row[5],
        "avatar_hue":    row[6],
        "last_active_at": row[7].isoformat() if row[7] else None,
        "created_at":    row[8].isoformat() if row[8] else None,
    }


def verify_password(plain: str, hashed: str) -> bool:
    """Return True when *plain* matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def touch_last_active(user_id: int) -> None:
    """Update last_active_at to NOW() for *user_id*."""
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE auth_users SET last_active_at = NOW() WHERE id = %s",
                (user_id,),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass  # non-critical; never block login


def list_users() -> List[Dict[str, Any]]:
    """Return all auth_users rows, ordered by id."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, email, role, status, avatar_hue,
                   created_at, last_active_at
            FROM auth_users ORDER BY id
            """
        ).fetchall()
    return [
        {
            "id":            r[0],
            "name":          r[1],
            "email":         r[2],
            "role":          r[3] or "viewer",
            "status":        r[4],
            "avatar_hue":    r[5],
            "created_at":    r[6].isoformat() if r[6] else None,
            "last_active_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def create_user(
    name: str,
    email: str,
    password: str,
    role: str = "viewer",
) -> Dict[str, Any]:
    """Insert a new user and return the created row."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")
    # Deterministic hue so avatars are stable (0–359).
    avatar_hue = abs(hash(email)) % 360
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO auth_users (name, email, password_hash, role, status, avatar_hue)
            VALUES (%s, %s, %s, %s, 'active', %s)
            RETURNING id, name, email, role, status, avatar_hue, created_at
            """,
            (name, email, hashed, role, avatar_hue),
        ).fetchone()
        conn.commit()
    return {
        "id":         row[0],
        "name":       row[1],
        "email":      row[2],
        "role":       row[3],
        "status":     row[4],
        "avatar_hue": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
    }


def update_user_role(user_id: int, role: str) -> None:
    """Change the role of *user_id*."""
    with _connect() as conn:
        conn.execute("UPDATE auth_users SET role = %s WHERE id = %s", (role, user_id))
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
