"""Insights and Metadata roles are stored and updated independently."""

from __future__ import annotations

import pytest

from src import auth_db, ui_app


def test_legacy_metadata_user_role_is_an_editor():
    assert auth_db.normalize_app_role("user") == "editor"
    assert auth_db.normalize_app_role("admin") == "admin"
    assert auth_db.normalize_app_role("nope") == "viewer"
    assert auth_db.normalize_app_role(None) == "viewer"


def test_insights_role_write_does_not_touch_the_shared_column():
    import inspect

    source = inspect.getsource(auth_db._insert_insights_role)
    assert "insights_user_app_roles" in source
    assert "auth_users" not in source


def test_metadata_role_write_updates_only_auth_users():
    import inspect

    source = inspect.getsource(auth_db.update_user_role)
    insights_branch, metadata_branch = source.split("else:", 1)
    assert "insights_user_app_roles" not in metadata_branch
    assert "UPDATE auth_users SET role" in metadata_branch
    assert "UPDATE auth_users SET role" not in insights_branch


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ui_app, "_needs_first_run_setup", lambda: False)
    ui_app.app.config["WTF_CSRF_ENABLED"] = False
    ui_app.app.config["TESTING"] = True
    with ui_app.app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["user_role"] = "admin"
            sess["user_email"] = "admin@example.com"
        yield test_client
    ui_app.app.config["WTF_CSRF_ENABLED"] = True


def test_role_patch_targets_one_application(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        auth_db,
        "update_user_role",
        lambda uid, role, app="insights": calls.append((uid, app, role)),
    )

    insights = client.patch("/api/users/5/role", json={"role": "editor"})
    metadata = client.patch("/api/users/5/role", json={"app": "metadata", "role": "viewer"})
    rejected = client.patch("/api/users/5/role", json={"app": "billing", "role": "admin"})

    assert insights.status_code == 200
    assert metadata.status_code == 200
    assert rejected.status_code == 400
    assert calls == [(5, "insights", "editor"), (5, "metadata", "viewer")]


def test_create_user_sends_separate_roles(client, monkeypatch):
    seen = {}

    def create(name, email, password, role="viewer", metadata_role="viewer"):
        seen.update(role=role, metadata_role=metadata_role, email=email)
        return {
            "id": 9, "name": name, "email": email, "role": role,
            "metadata_role": metadata_role, "status": "active",
            "avatar_hue": 1, "created_at": None,
        }

    monkeypatch.setattr(auth_db, "email_exists", lambda email: False)
    monkeypatch.setattr(auth_db, "create_user", create)

    res = client.post("/api/users", json={
        "name": "Ada",
        "email": "ada@example.com",
        "password": "long-enough",
        "role": "admin",
        "metadata_role": "viewer",
    })

    assert res.status_code == 201
    assert seen == {
        "role": "admin",
        "metadata_role": "viewer",
        "email": "ada@example.com",
    }
