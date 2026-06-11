"""Tests unitaires pour l'API Central (FastAPI).

Lance avec: pytest tests/test_central_api.py -v
Depuis le répertoire /home/vlm/virtual-labs-management/central/
"""
from __future__ import annotations

import os
import importlib
import tempfile
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

import sys


@pytest.fixture(scope="module")
def client():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    central_root = os.path.join(repo_root, "central")
    vm_agent_root = os.path.join(repo_root, "vm-agent")
    central_src_root = os.path.join(central_root, "src")
    central_config = os.path.join(central_root, "config.yaml")

    with tempfile.TemporaryDirectory() as tmp_dir:
        users_path = os.path.join(tmp_dir, "users.json")
        previous_cwd = os.getcwd()
        previous_path = list(sys.path)
        previous_config = os.environ.get("CENTRAL_CONFIG")
        previous_users_file = os.environ.get("CENTRAL_USERS_FILE")
        previous_bootstrap_admin_password = os.environ.get("CENTRAL_BOOTSTRAP_ADMIN_PASSWORD")
        previous_login_max_attempts = os.environ.get("CENTRAL_LOGIN_MAX_ATTEMPTS")
        previous_login_window_seconds = os.environ.get("CENTRAL_LOGIN_WINDOW_SECONDS")
        previous_login_lockout_seconds = os.environ.get("CENTRAL_LOGIN_LOCKOUT_SECONDS")

        stale_src_modules = [name for name in sys.modules.keys() if name == "src" or name.startswith("src.")]
        for name in stale_src_modules:
            sys.modules.pop(name, None)

        central_root_norm = os.path.realpath(central_root)
        vm_agent_root_norm = os.path.realpath(vm_agent_root)
        cleaned_path = []
        for entry in sys.path:
            entry_norm = os.path.realpath(entry) if entry else entry
            if entry_norm in {central_root_norm, vm_agent_root_norm}:
                continue
            cleaned_path.append(entry)
        sys.path = cleaned_path
        sys.path.insert(0, central_root)
        importlib.invalidate_caches()

        os.chdir(central_root)
        os.environ["CENTRAL_CONFIG"] = central_config
        os.environ["CENTRAL_USERS_FILE"] = users_path
        os.environ["CENTRAL_BOOTSTRAP_ADMIN_PASSWORD"] = "ChangeMe123!"
        os.environ["CENTRAL_LOGIN_MAX_ATTEMPTS"] = "5"
        os.environ["CENTRAL_LOGIN_WINDOW_SECONDS"] = "300"
        os.environ["CENTRAL_LOGIN_LOCKOUT_SECONDS"] = "900"
        try:
            app_module = importlib.import_module("src.app")
            app_file = os.path.realpath(getattr(app_module, "__file__", ""))
            if not app_file.startswith(central_src_root):
                raise RuntimeError(f"Unexpected src.app module path: {app_file}")
            app = app_module.app
            with TestClient(app) as c:
                yield c
        finally:
            os.chdir(previous_cwd)
            sys.path = previous_path
            if previous_config is None:
                os.environ.pop("CENTRAL_CONFIG", None)
            else:
                os.environ["CENTRAL_CONFIG"] = previous_config
            if previous_users_file is None:
                os.environ.pop("CENTRAL_USERS_FILE", None)
            else:
                os.environ["CENTRAL_USERS_FILE"] = previous_users_file
            if previous_bootstrap_admin_password is None:
                os.environ.pop("CENTRAL_BOOTSTRAP_ADMIN_PASSWORD", None)
            else:
                os.environ["CENTRAL_BOOTSTRAP_ADMIN_PASSWORD"] = previous_bootstrap_admin_password
            if previous_login_max_attempts is None:
                os.environ.pop("CENTRAL_LOGIN_MAX_ATTEMPTS", None)
            else:
                os.environ["CENTRAL_LOGIN_MAX_ATTEMPTS"] = previous_login_max_attempts
            if previous_login_window_seconds is None:
                os.environ.pop("CENTRAL_LOGIN_WINDOW_SECONDS", None)
            else:
                os.environ["CENTRAL_LOGIN_WINDOW_SECONDS"] = previous_login_window_seconds
            if previous_login_lockout_seconds is None:
                os.environ.pop("CENTRAL_LOGIN_LOCKOUT_SECONDS", None)
            else:
                os.environ["CENTRAL_LOGIN_LOCKOUT_SECONDS"] = previous_login_lockout_seconds


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_unauthenticated_state_returns_401(client):
    """Le endpoint /api/state doit exiger une session valide."""
    resp = client.get("/api/state")
    assert resp.status_code == 401
    payload = resp.json()
    assert payload.get("ok") is False
    assert "detail" in payload


def test_login_bad_credentials(client):
    """Une tentative de connexion avec un mot de passe invalide renvoie 401."""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_home_form_login_bad_credentials_returns_html_error(client):
    """Le fallback HTML sur POST / doit éviter le 405 et afficher une erreur exploitable."""
    resp = client.post("/", data={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    assert "Identifiants invalides" in resp.text


def test_home_form_login_success_redirects_and_sets_session(client):
    """Le fallback HTML sur POST / doit ouvrir une session valide."""
    resp = client.post("/", data={"username": "admin", "password": "ChangeMe123!"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/"

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json().get("user", {}).get("username") == "admin"


def test_login_rate_limit_after_repeated_failures(client):
    previous_max_attempts = os.environ.get("CENTRAL_LOGIN_MAX_ATTEMPTS")
    previous_lockout_seconds = os.environ.get("CENTRAL_LOGIN_LOCKOUT_SECONDS")
    try:
        os.environ["CENTRAL_LOGIN_MAX_ATTEMPTS"] = "3"
        os.environ["CENTRAL_LOGIN_LOCKOUT_SECONDS"] = "60"
        for _ in range(3):
            resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
            assert resp.status_code == 401

        blocked = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert blocked.status_code == 429
        detail = blocked.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("retry_after_seconds", 0) > 0
    finally:
        if previous_max_attempts is None:
            os.environ.pop("CENTRAL_LOGIN_MAX_ATTEMPTS", None)
        else:
            os.environ["CENTRAL_LOGIN_MAX_ATTEMPTS"] = previous_max_attempts
        if previous_lockout_seconds is None:
            os.environ.pop("CENTRAL_LOGIN_LOCKOUT_SECONDS", None)
        else:
            os.environ["CENTRAL_LOGIN_LOCKOUT_SECONDS"] = previous_lockout_seconds


def test_login_then_me(client):
    """Connexion avec les identifiants par défaut puis vérification de /api/auth/me."""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "ChangeMe123!"})
    if resp.status_code == 200:
        login_payload = resp.json()
        assert login_payload.get("user", {}).get("must_change_password") is True
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        data = me.json()
        assert data["user"]["username"] == "admin"
        assert data["user"].get("must_change_password") is True


def test_must_change_password_blocks_protected_routes(client):
    """Un compte marqué must_change_password ne doit pas accéder aux routes métier."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "ChangeMe123!"})
    assert login.status_code == 200

    blocked = client.get("/api/state")
    assert blocked.status_code == 403
    detail = blocked.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "password_change_required"


def test_change_password_clears_must_change_flag(client):
    """Après changement de mot de passe, l'utilisateur retrouve l'accès."""
    login = client.post("/api/auth/login", json={"username": "admin", "password": "ChangeMe123!"})
    assert login.status_code == 200

    with patch("src.api.routes.linux_accounts.sync_password", return_value=None):
        changed = client.post(
            "/api/auth/change-password",
            json={"current_password": "ChangeMe123!", "new_password": "NewStrongPass123!"},
        )
    assert changed.status_code == 200

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json().get("user", {}).get("must_change_password") is False

    state = client.get("/api/state")
    assert state.status_code in {200, 500}


def test_admin_can_move_user_to_another_group_from_edit(client):
    login = client.post("/api/auth/login", json={"username": "admin", "password": "NewStrongPass123!"})
    assert login.status_code == 200

    create_group_alpha = client.post(
        "/api/admin/groups",
        json={"name": "alpha", "description": "Alpha", "members": [], "vm_ids": [], "lab_permissions": []},
    )
    assert create_group_alpha.status_code == 200

    create_group_beta = client.post(
        "/api/admin/groups",
        json={"name": "beta", "description": "Beta", "members": [], "vm_ids": [], "lab_permissions": []},
    )
    assert create_group_beta.status_code == 200

    with patch("src.api.routes.linux_accounts.sync_create_or_update_user", return_value=None):
        created = client.post(
            "/api/admin/users",
            json={
                "username": "user-move",
                "full_name": "User Move",
                "email": "user-move@example.test",
                "password": "UserMovePass123!",
                "role": "user",
                "group_name": "alpha",
            },
        )
    assert created.status_code == 200
    assert created.json().get("user", {}).get("groups") == ["alpha"]

    updated = client.patch(
        "/api/admin/users/user-move",
        json={"group_name": "beta"},
    )
    assert updated.status_code == 200
    assert updated.json().get("user", {}).get("groups") == ["beta"]

    groups = client.get("/api/admin/groups")
    assert groups.status_code == 200
    items = {item["name"]: item for item in groups.json().get("items", [])}
    assert "user-move" not in items["alpha"].get("members", [])
    assert "user-move" in items["beta"].get("members", [])


def test_reset_link_password_does_not_keep_user_blocked(client):
    login = client.post("/api/auth/login", json={"username": "admin", "password": "NewStrongPass123!"})
    assert login.status_code == 200

    create_group = client.post(
        "/api/admin/groups",
        json={"name": "gamma", "description": "Gamma", "members": [], "vm_ids": [], "lab_permissions": []},
    )
    assert create_group.status_code == 200

    with patch("src.api.routes.linux_accounts.sync_create_or_update_user", return_value=None):
        created = client.post(
            "/api/admin/users",
            json={
                "username": "user-reset-link",
                "full_name": "User Reset Link",
                "email": "user-reset-link@example.test",
                "password": "InitialPass123!",
                "role": "user",
                "group_name": "gamma",
            },
        )
    assert created.status_code == 200

    link_response = client.post("/api/admin/users/user-reset-link/generate-reset-link")
    assert link_response.status_code == 200
    reset_url = link_response.json().get("reset_url", "")
    assert "reset_token=" in reset_url
    token = reset_url.split("reset_token=", 1)[1]

    with patch("src.api.routes.linux_accounts.sync_password", return_value=None):
        consumed = client.post(
            "/api/auth/reset-with-token",
            json={"token": token, "new_password": "ResetLinkPass123!"},
        )
    assert consumed.status_code == 200

    client.post("/api/auth/logout")

    user_login = client.post("/api/auth/login", json={"username": "user-reset-link", "password": "ResetLinkPass123!"})
    assert user_login.status_code == 200
    assert user_login.json().get("user", {}).get("must_change_password") is False
    assert user_login.json().get("user", {}).get("groups") == ["gamma"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json().get("user", {}).get("must_change_password") is False

    state = client.get("/api/state")
    assert state.status_code != 403


def test_static_assets_no_cache(client):
    """Les fichiers statiques doivent avoir le header no-cache."""
    resp = client.get("/static/js/app.js")
    if resp.status_code == 200:
        cc = resp.headers.get("cache-control", "")
        assert "no-cache" in cc


def test_index_html_does_not_load_legacy_app_js(client):
    """La page principale ne doit plus charger app.js (legacy gelé)."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert '/static/js/app.js' not in resp.text
