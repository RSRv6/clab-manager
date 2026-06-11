from __future__ import annotations

import logging
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_DEFAULT_SUDO_GROUP = os.getenv("CENTRAL_LINUX_ADMIN_GROUP", "sudo").strip() or "sudo"
_DEFAULT_MAX_SESSIONS = int(os.getenv("CENTRAL_LINUX_MAX_SESSIONS", "3"))
_LIMITS_FILE = Path(os.getenv("CENTRAL_LINUX_LIMITS_FILE", "/etc/security/limits.d/vlm-users.conf"))


def _sync_enabled() -> bool:
    raw = os.getenv("CENTRAL_LINUX_ACCOUNT_SYNC", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _require_valid_username(username: str) -> str:
    candidate = str(username or "").strip().lower()
    if not _USERNAME_RE.match(candidate):
        raise RuntimeError("Nom de compte Linux invalide")
    return candidate


def _privileged_prefix() -> list[str]:
    configured = os.getenv("CENTRAL_LINUX_PRIVILEGED_CMD", "").strip()
    if configured:
        return shlex.split(configured)
    if os.geteuid() == 0:
        return []
    return ["sudo", "-n"]


def _run_raw(cmd: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    full_cmd = [*_privileged_prefix(), *cmd]
    logger.info("linux_accounts run cmd=%s", " ".join(shlex.quote(part) for part in full_cmd))
    result = subprocess.run(
        full_cmd,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    return result


def _run(cmd: list[str], *, stdin: str | None = None) -> None:
    result = _run_raw(cmd, stdin=stdin)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(f"Commande Linux échouée: {' '.join(cmd)} :: {stderr or stdout or 'exit != 0'}")


def _user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def _ensure_home_ssh_permissions(username: str) -> None:
    account = pwd.getpwnam(username)
    home = Path(account.pw_dir)
    ssh_dir = home / ".ssh"
    auth_keys = ssh_dir / "authorized_keys"

    _run(["mkdir", "-p", str(ssh_dir)])
    _run(["touch", str(auth_keys)])
    _run(["chown", f"{account.pw_uid}:{account.pw_gid}", str(ssh_dir), str(auth_keys)])
    _run(["chmod", "700", str(ssh_dir)])
    _run(["chmod", "600", str(auth_keys)])


def _set_password(username: str, password: str) -> None:
    _run(["chpasswd"], stdin=f"{username}:{password}\n")


def _set_admin_role(username: str, is_admin: bool) -> None:
    if is_admin:
        _run(["usermod", "-aG", _DEFAULT_SUDO_GROUP, username])
        return

    # Ignore failures when user is not currently in sudo group.
    result = _run_raw(["gpasswd", "-d", username, _DEFAULT_SUDO_GROUP])
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".lower()
        if "is not a member" not in output:
            raise RuntimeError(f"Impossible de retirer {username} du groupe {_DEFAULT_SUDO_GROUP}")


def _read_managed_limits() -> dict[str, int]:
    if not _LIMITS_FILE.exists():
        return {}

    out: dict[str, int] = {}
    for raw in _LIMITS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 4:
            continue
        username, _soft_hard, item, value = parts
        if item != "maxlogins":
            continue
        try:
            out[username] = int(value)
        except ValueError:
            continue
    return out


def _write_managed_limits(rules: dict[str, int]) -> None:
    _run(["mkdir", "-p", str(_LIMITS_FILE.parent)])
    lines = [
        "# Managed by VLM central",
        "# Format: <user> hard maxlogins <N>",
    ]
    for username in sorted(rules.keys()):
        lines.append(f"{username} hard maxlogins {int(rules[username])}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(lines) + "\n")
        tmp_path = tmp.name
    try:
        _run(["install", "-m", "0644", tmp_path, str(_LIMITS_FILE)])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _set_user_max_sessions(username: str, limit: int) -> None:
    current = _read_managed_limits()
    current[username] = max(1, int(limit))
    _write_managed_limits(current)


def _remove_user_max_sessions(username: str) -> None:
    current = _read_managed_limits()
    if username in current:
        current.pop(username, None)
        _write_managed_limits(current)


def sync_create_or_update_user(username: str, password: str, *, is_admin: bool) -> None:
    if not _sync_enabled():
        return

    user = _require_valid_username(username)
    if not _user_exists(user):
        _run(["useradd", "-m", "-s", "/bin/bash", user])

    _set_password(user, password)
    _set_admin_role(user, is_admin)
    _ensure_home_ssh_permissions(user)
    _set_user_max_sessions(user, _DEFAULT_MAX_SESSIONS)


def sync_password(username: str, password: str) -> None:
    if not _sync_enabled():
        return

    user = _require_valid_username(username)
    if not _user_exists(user):
        raise RuntimeError("Compte Linux introuvable")
    _set_password(user, password)


def sync_role(username: str, *, is_admin: bool) -> None:
    if not _sync_enabled():
        return

    user = _require_valid_username(username)
    if not _user_exists(user):
        raise RuntimeError("Compte Linux introuvable")
    _set_admin_role(user, is_admin)


def sync_delete_user(username: str) -> None:
    if not _sync_enabled():
        return

    user = _require_valid_username(username)
    _remove_user_max_sessions(user)

    if not _user_exists(user):
        return
    _run(["userdel", "-r", user])


def _authorized_keys_path(username: str) -> Path:
    account = pwd.getpwnam(username)
    return Path(account.pw_dir) / ".ssh" / "authorized_keys"


def list_ssh_keys(username: str) -> list[str]:
    if not _sync_enabled():
        return []

    user = _require_valid_username(username)
    if not _user_exists(user):
        raise RuntimeError("Compte Linux introuvable")

    _ensure_home_ssh_permissions(user)
    auth_path = _authorized_keys_path(user)
    result = _run_raw(["cat", str(auth_path)])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Lecture authorized_keys impossible").strip())
    content = result.stdout or ""
    return [line.strip() for line in content.splitlines() if line.strip()]


def replace_ssh_keys(username: str, keys: list[str]) -> list[str]:
    if not _sync_enabled():
        return []

    user = _require_valid_username(username)
    if not _user_exists(user):
        raise RuntimeError("Compte Linux introuvable")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in keys:
        key = str(raw or "").strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)

    _ensure_home_ssh_permissions(user)
    auth_path = _authorized_keys_path(user)
    payload = "\n".join(normalized) + ("\n" if normalized else "")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    try:
        _run(["install", "-m", "0600", tmp_path, str(auth_path)])
        account = pwd.getpwnam(user)
        _run(["chown", f"{account.pw_uid}:{account.pw_gid}", str(auth_path)])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return normalized


def status() -> dict[str, Any]:
    return {
        "enabled": _sync_enabled(),
        "privileged_cmd": " ".join(_privileged_prefix()) or "direct",
        "admin_group": _DEFAULT_SUDO_GROUP,
        "max_sessions": _DEFAULT_MAX_SESSIONS,
        "limits_file": str(_LIMITS_FILE),
    }
