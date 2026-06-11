from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from threading import Lock
from typing import Any

from fastapi import HTTPException, Request, status

from src.config import get_settings

_USERS_LOCK = Lock()
_HASH_ITERATIONS = 310_000

# ── Session timeout (3 hours of inactivity) ────────────────────────────────
_SESSION_TIMEOUT_SECONDS = 3 * 60 * 60  # 3 hours

# ── Password-reset token store (in-memory, single-use, 2h TTL) ──────────────
_RESET_TOKENS: dict[str, dict[str, Any]] = {}
_RESET_TOKENS_LOCK = Lock()
_RESET_TOKEN_TTL = 7200  # seconds


def generate_reset_token(username: str) -> str:
    """Create a single-use reset token for *username* (valid 2 h).
    Any previous token issued for this user is revoked.
    Raises RuntimeError if the user does not exist.
    """
    normalized = _normalize_username(username)
    with _USERS_LOCK:
        storage = _load_storage()
        if normalized not in storage["users"]:
            raise RuntimeError("Utilisateur introuvable")

    token = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + _RESET_TOKEN_TTL

    with _RESET_TOKENS_LOCK:
        # Purge expired entries
        now = time.monotonic()
        expired = [k for k, v in _RESET_TOKENS.items() if v["expires_at"] < now]
        for k in expired:
            del _RESET_TOKENS[k]
        # Revoke any previous token for the same user
        previous = [k for k, v in _RESET_TOKENS.items() if v["username"] == normalized]
        for k in previous:
            del _RESET_TOKENS[k]
        _RESET_TOKENS[token] = {"username": normalized, "expires_at": expires_at}

    return token


def consume_reset_token(token: str) -> str:
    """Validate and consume *token*. Returns the username.
    Raises RuntimeError if the token is unknown or expired.
    """
    with _RESET_TOKENS_LOCK:
        entry = _RESET_TOKENS.get(str(token or ""))
        if not entry:
            raise RuntimeError("Lien invalide ou déjà utilisé")
        if time.monotonic() > entry["expires_at"]:
            del _RESET_TOKENS[token]
            raise RuntimeError("Lien expiré (validité : 2 heures)")
        username = entry["username"]
        del _RESET_TOKENS[token]
    return username
_DEFAULT_ADMIN_USERNAME = "admin"
_DEFAULT_ADMIN_PASSWORD = str(os.getenv("CENTRAL_BOOTSTRAP_ADMIN_PASSWORD") or "").strip()
_DEFAULT_ADMIN_NAME = "Administrateur VLM"
_DEFAULT_ADMIN_EMAIL = "admin@local"
_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AuthenticatedUser:
    username: str
    full_name: str
    email: str
    role: str
    active: bool = True
    must_change_password: bool = False
    groups: list[str] = field(default_factory=list)
    full_vm_access: set[str] = field(default_factory=set)
    lab_access: dict[str, set[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "full_name": self.full_name,
            "email": self.email,
            "role": self.role,
            "active": self.active,
            "must_change_password": self.must_change_password,
            "groups": sorted(self.groups),
        }

    def can_access_vm(self, vm_id: str) -> bool:
        if self.role == "admin":
            return True
        return vm_id in self.full_vm_access or vm_id in self.lab_access

    def can_access_lab(self, vm_id: str, lab_name: str) -> bool:
        if self.role == "admin":
            return True
        if vm_id in self.full_vm_access:
            return True
        return lab_name in self.lab_access.get(vm_id, set())

    def can_redeploy_lab(self) -> bool:
        return self.role in {"admin", "group-admin"}


class PasswordHasher:
    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS)
        return "pbkdf2_sha256${iterations}${salt}${digest}".format(
            iterations=_HASH_ITERATIONS,
            salt=base64.b64encode(salt).decode("ascii"),
            digest=base64.b64encode(digest).decode("ascii"),
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        except ValueError:
            return False

        if algorithm != "pbkdf2_sha256":
            return False

        try:
            iterations = int(iterations_text)
            salt = base64.b64decode(salt_text.encode("ascii"))
            expected = base64.b64decode(digest_text.encode("ascii"))
        except (ValueError, TypeError):
            return False

        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)


def _default_users_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "users.json"


def _users_path() -> Path:
    configured = os.getenv("CENTRAL_USERS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()

    auth_settings = get_settings().get("auth", {})
    configured_path = str(auth_settings.get("users_file") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()

    return _default_users_path()


def _ensure_storage_dir() -> Path:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_admin_record() -> dict[str, Any]:
    bootstrap_password = _DEFAULT_ADMIN_PASSWORD
    if not bootstrap_password:
        if _env_flag("CENTRAL_ALLOW_INSECURE_DEFAULT_ADMIN_PASSWORD", False):
            bootstrap_password = "ChangeMe123!"
        else:
            raise RuntimeError(
                "Configuration invalide: CENTRAL_BOOTSTRAP_ADMIN_PASSWORD est obligatoire pour l'initialisation admin. "
                "Activez CENTRAL_ALLOW_INSECURE_DEFAULT_ADMIN_PASSWORD=1 uniquement pour une migration temporaire."
            )

    return {
        "username": _DEFAULT_ADMIN_USERNAME,
        "full_name": _DEFAULT_ADMIN_NAME,
        "email": _DEFAULT_ADMIN_EMAIL,
        "role": "admin",
        "active": True,
        "created_at": _utc_now(),
        "password_hash": PasswordHasher.hash_password(bootstrap_password),
        "must_change_password": True,
    }


def _normalize_username(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_group_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_user(record: dict[str, Any]) -> dict[str, Any] | None:
    username = _normalize_username(record.get("username"))
    full_name = str(record.get("full_name") or "").strip()
    email = str(record.get("email") or "").strip().lower()
    role = str(record.get("role") or "user").strip().lower()
    password_hash = str(record.get("password_hash") or "").strip()
    active = bool(record.get("active", True))

    if not username or not full_name or not email or not password_hash:
        return None
    if role not in {"user", "admin", "group-admin"}:
        return None

    return {
        "username": username,
        "full_name": full_name,
        "email": email,
        "role": role,
        "active": active,
        "created_at": str(record.get("created_at") or _utc_now()),
        "password_hash": password_hash,
        "must_change_password": bool(record.get("must_change_password", False)),
    }


def _normalize_lab_permission(record: dict[str, Any]) -> dict[str, Any] | None:
    vm_id = str(record.get("vm_id") or "").strip()
    labs = sorted({str(item).strip() for item in record.get("labs") or [] if str(item).strip()})
    if not vm_id or not labs:
        return None
    return {"vm_id": vm_id, "labs": labs}


def _normalize_group(record: dict[str, Any]) -> dict[str, Any] | None:
    name = _normalize_group_name(record.get("name"))
    if not name or not _GROUP_NAME_RE.match(name):
        return None

    members = sorted({_normalize_username(item) for item in record.get("members") or [] if _normalize_username(item)})
    vm_ids = sorted({str(item).strip() for item in record.get("vm_ids") or [] if str(item).strip()})

    raw_permissions = record.get("lab_permissions") or []
    permissions_map: dict[str, set[str]] = defaultdict(set)
    for item in raw_permissions:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_lab_permission(item)
        if not normalized:
            continue
        permissions_map[normalized["vm_id"]].update(normalized["labs"])

    lab_permissions = [
        {"vm_id": vm_id, "labs": sorted(labs)}
        for vm_id, labs in sorted(permissions_map.items())
        if labs
    ]

    return {
        "name": name,
        "description": str(record.get("description") or "").strip(),
        "members": members,
        "vm_ids": vm_ids,
        "lab_permissions": lab_permissions,
        "created_at": str(record.get("created_at") or _utc_now()),
    }


def _load_storage() -> dict[str, dict[str, dict[str, Any]]]:
    path = _ensure_storage_dir()
    if not path.exists():
        storage = {
            "users": {_DEFAULT_ADMIN_USERNAME: _default_admin_record()},
            "groups": {},
        }
        _save_storage(storage)
        return storage

    try:
        with path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except json.JSONDecodeError:
        storage = {
            "users": {_DEFAULT_ADMIN_USERNAME: _default_admin_record()},
            "groups": {},
        }
        _save_storage(storage)
        return storage

    raw_users = loaded.get("users") if isinstance(loaded, dict) else loaded
    raw_groups = loaded.get("groups") if isinstance(loaded, dict) else []

    users: dict[str, dict[str, Any]] = {}
    if isinstance(raw_users, list):
        for item in raw_users:
            if not isinstance(item, dict):
                continue
            user = _normalize_user(item)
            if user:
                users[user["username"]] = user

    groups: dict[str, dict[str, Any]] = {}
    if isinstance(raw_groups, list):
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            group = _normalize_group(item)
            if group:
                groups[group["name"]] = group

    if not users:
        users[_DEFAULT_ADMIN_USERNAME] = _default_admin_record()

    for group in groups.values():
        group["members"] = [member for member in group["members"] if member in users]

    storage = {"users": users, "groups": groups}

    needs_save = False
    if not isinstance(loaded, dict):
        needs_save = True
    elif "groups" not in loaded:
        needs_save = True

    if needs_save:
        _save_storage(storage)

    return storage


def _save_storage(storage: dict[str, dict[str, dict[str, Any]]]) -> None:
    path = _ensure_storage_dir()
    payload = {
        "users": [storage["users"][key] for key in sorted(storage["users"].keys())],
        "groups": [storage["groups"][key] for key in sorted(storage["groups"].keys())],
    }
    text = json.dumps(payload, indent=2, ensure_ascii=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _active_admin_count(users: dict[str, dict[str, Any]]) -> int:
    return sum(
        1
        for user in users.values()
        if user.get("role") == "admin" and bool(user.get("active", True))
    )


def _groups_for_user(username: str, groups: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [group for group in groups.values() if username in group.get("members", [])]


def _validate_single_group_membership(
    members: list[str],
    groups: dict[str, dict[str, Any]],
    *,
    ignore_group: str | None = None,
) -> None:
    already_assigned: dict[str, str] = {}
    for group_name, group in groups.items():
        if ignore_group and group_name == ignore_group:
            continue
        for member in group.get("members", []):
            if member in members:
                already_assigned[member] = group_name

    if already_assigned:
        details = ", ".join(f"{user}({group})" for user, group in sorted(already_assigned.items()))
        raise ValueError(f"Un utilisateur ne peut appartenir qu'a un seul groupe: {details}")


def _assign_user_to_group(
    username: str,
    group_name: str,
    groups: dict[str, dict[str, Any]],
) -> None:
    normalized_group = _normalize_group_name(group_name)
    if not normalized_group:
        return

    target_group = groups.get(normalized_group)
    if not target_group:
        raise ValueError("Groupe introuvable")

    _validate_single_group_membership([username], groups, ignore_group=normalized_group)

    members = sorted({_normalize_username(member) for member in target_group.get("members", []) if _normalize_username(member)} | {username})
    target_group["members"] = members


def _remove_user_from_groups(
    username: str,
    groups: dict[str, dict[str, Any]],
) -> None:
    for group in groups.values():
        group["members"] = [member for member in group.get("members", []) if member != username]


def _set_user_group_membership(
    username: str,
    group_name: str | None,
    groups: dict[str, dict[str, Any]],
) -> None:
    _remove_user_from_groups(username, groups)
    if group_name is None:
        return

    normalized_group = str(group_name).strip()
    if not normalized_group:
        return

    _assign_user_to_group(username, normalized_group, groups)


def _build_authenticated_user(record: dict[str, Any], groups: dict[str, dict[str, Any]]) -> AuthenticatedUser:
    user_groups = _groups_for_user(record["username"], groups)
    full_vm_access: set[str] = set()
    lab_access: dict[str, set[str]] = defaultdict(set)

    for group in user_groups:
        full_vm_access.update(group.get("vm_ids", []))
        for permission in group.get("lab_permissions", []):
            vm_id = permission.get("vm_id")
            if not vm_id or vm_id in full_vm_access:
                continue
            lab_access[vm_id].update(permission.get("labs", []))

    return AuthenticatedUser(
        username=record["username"],
        full_name=record["full_name"],
        email=record["email"],
        role=record["role"],
        active=bool(record.get("active", True)),
        must_change_password=bool(record.get("must_change_password", False)),
        groups=sorted(group["name"] for group in user_groups),
        full_vm_access=full_vm_access,
        lab_access={vm_id: set(labs) for vm_id, labs in lab_access.items()},
    )


def _is_password_change_exempt_path(path: str) -> bool:
    normalized = str(path or "").strip()
    return normalized in {
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/change-password",
    }


def _public_user_payload(record: dict[str, Any], groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    user_groups = sorted(group["name"] for group in _groups_for_user(record["username"], groups))
    return {
        "username": record["username"],
        "full_name": record["full_name"],
        "email": record["email"],
        "role": record["role"],
        "active": bool(record.get("active", True)),
        "created_at": record.get("created_at", ""),
        "must_change_password": bool(record.get("must_change_password", False)),
        "groups": user_groups,
    }


def _public_group_payload(record: dict[str, Any], users: dict[str, dict[str, Any]]) -> dict[str, Any]:
    members = list(record.get("members", []))
    return {
        "name": record["name"],
        "description": record.get("description", ""),
        "members": members,
        "member_details": [
            {
                "username": member,
                "full_name": users.get(member, {}).get("full_name", member),
            }
            for member in members
        ],
        "vm_ids": list(record.get("vm_ids", [])),
        "lab_permissions": list(record.get("lab_permissions", [])),
        "created_at": record.get("created_at", ""),
    }


def list_users() -> list[dict[str, Any]]:
    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        groups = storage["groups"]
        return [
            _public_user_payload(user, groups)
            for user in sorted(users.values(), key=lambda item: (item["role"], item["username"]))
        ]


def list_groups() -> list[dict[str, Any]]:
    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        groups = storage["groups"]
        return [
            _public_group_payload(group, users)
            for group in sorted(groups.values(), key=lambda item: item["name"])
        ]


def get_user(username: str) -> AuthenticatedUser | None:
    normalized = _normalize_username(username)
    if not normalized:
        return None

    with _USERS_LOCK:
        storage = _load_storage()
        record = storage["users"].get(normalized)
        if not record or not record.get("active", True):
            return None
        return _build_authenticated_user(record, storage["groups"])


def authenticate_user(username: str, password: str) -> AuthenticatedUser | None:
    normalized = _normalize_username(username)
    with _USERS_LOCK:
        storage = _load_storage()
        record = storage["users"].get(normalized)
        if not record or not record.get("active", True):
            return None
        if not PasswordHasher.verify_password(password, record.get("password_hash", "")):
            return None
        return _build_authenticated_user(record, storage["groups"])


def create_user(
    username: str,
    full_name: str,
    email: str,
    password: str,
    role: str,
    group_name: str | None = None,
) -> dict[str, Any]:
    normalized_username = _normalize_username(username)
    normalized_email = str(email or "").strip().lower()
    normalized_role = str(role or "user").strip().lower()
    clean_name = str(full_name or "").strip()

    if not normalized_username:
        raise ValueError("Le nom d'utilisateur est requis")
    if not clean_name:
        raise ValueError("Le nom complet est requis")
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Un email valide est requis")
    if normalized_role not in {"user", "admin", "group-admin"}:
        raise ValueError("Le role doit etre user, group-admin ou admin")
    if len(password or "") < 10:
        raise ValueError("Le mot de passe doit contenir au moins 10 caracteres")

    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        if normalized_username in users:
            raise RuntimeError("Cet utilisateur existe deja")

        record = {
            "username": normalized_username,
            "full_name": clean_name,
            "email": normalized_email,
            "role": normalized_role,
            "active": True,
            "created_at": _utc_now(),
            "password_hash": PasswordHasher.hash_password(password),
            "must_change_password": False,
        }
        users[normalized_username] = record

        if group_name is not None:
            _assign_user_to_group(normalized_username, group_name, storage["groups"])

        _save_storage(storage)
        return _public_user_payload(record, storage["groups"])


def update_user(
    username: str,
    *,
    full_name: str | None = None,
    email: str | None = None,
    role: str | None = None,
    active: bool | None = None,
    group_name: str | None = None,
    set_group_name: bool = False,
    actor_username: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_username(username)
    normalized_actor = _normalize_username(actor_username)

    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        record = users.get(normalized)
        if not record:
            raise RuntimeError("Utilisateur introuvable")

        if normalized_actor and normalized == normalized_actor:
            if active is False:
                raise RuntimeError("Un administrateur ne peut pas se desactiver lui-meme")
            if role and str(role).strip().lower() != "admin":
                raise RuntimeError("Un administrateur ne peut pas retirer son propre role admin")

        if full_name is not None:
            clean_name = str(full_name).strip()
            if not clean_name:
                raise ValueError("Le nom complet est requis")
            record["full_name"] = clean_name

        if email is not None:
            clean_email = str(email).strip().lower()
            if not clean_email or "@" not in clean_email:
                raise ValueError("Un email valide est requis")
            record["email"] = clean_email

        if role is not None:
            normalized_role = str(role).strip().lower()
            if normalized_role not in {"user", "admin", "group-admin"}:
                raise ValueError("Le role doit etre user, group-admin ou admin")
            if record.get("role") == "admin" and normalized_role != "admin" and _active_admin_count(users) <= 1:
                raise RuntimeError("Impossible de retirer le role du dernier admin actif")
            record["role"] = normalized_role

        if active is not None:
            if record.get("role") == "admin" and active is False and _active_admin_count(users) <= 1:
                raise RuntimeError("Impossible de desactiver le dernier admin actif")
            record["active"] = bool(active)

        if set_group_name:
            _set_user_group_membership(normalized, group_name, storage["groups"])

        users[normalized] = record
        _save_storage(storage)
        return _public_user_payload(record, storage["groups"])


def reset_user_password(username: str, new_password: str) -> dict[str, Any]:
    normalized = _normalize_username(username)
    if len(new_password or "") < 10:
        raise ValueError("Le mot de passe doit contenir au moins 10 caracteres")

    with _USERS_LOCK:
        storage = _load_storage()
        record = storage["users"].get(normalized)
        if not record:
            raise RuntimeError("Utilisateur introuvable")
        record["password_hash"] = PasswordHasher.hash_password(new_password)
        record["must_change_password"] = True
        storage["users"][normalized] = record
        _save_storage(storage)
        return _public_user_payload(record, storage["groups"])


def delete_user(username: str, actor_username: str | None = None) -> None:
    normalized = _normalize_username(username)
    normalized_actor = _normalize_username(actor_username)

    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        groups = storage["groups"]
        record = users.get(normalized)
        if not record:
            raise RuntimeError("Utilisateur introuvable")
        if normalized_actor and normalized == normalized_actor:
            raise RuntimeError("Un administrateur ne peut pas supprimer son propre compte")
        if record.get("role") == "admin" and bool(record.get("active", True)) and _active_admin_count(users) <= 1:
            raise RuntimeError("Impossible de supprimer le dernier admin actif")

        users.pop(normalized, None)
        for group in groups.values():
            group["members"] = [member for member in group.get("members", []) if member != normalized]
        _save_storage(storage)


def change_password(username: str, new_password: str) -> None:
    normalized = _normalize_username(username)
    if len(new_password or "") < 10:
        raise ValueError("Le mot de passe doit contenir au moins 10 caracteres")

    with _USERS_LOCK:
        storage = _load_storage()
        record = storage["users"].get(normalized)
        if not record:
            raise RuntimeError("Utilisateur introuvable")
        record["password_hash"] = PasswordHasher.hash_password(new_password)
        record["must_change_password"] = False
        storage["users"][normalized] = record
        _save_storage(storage)


def create_group(
    name: str,
    *,
    description: str = "",
    members: list[str] | None = None,
    vm_ids: list[str] | None = None,
    lab_permissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_name = _normalize_group_name(name)
    if not normalized_name or not _GROUP_NAME_RE.match(normalized_name):
        raise ValueError("Le nom du groupe doit contenir uniquement lettres, chiffres, tirets ou underscores")

    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        groups = storage["groups"]
        if normalized_name in groups:
            raise RuntimeError("Ce groupe existe deja")

        normalized_members = sorted({_normalize_username(item) for item in members or [] if _normalize_username(item)})
        unknown_members = [member for member in normalized_members if member not in users]
        if unknown_members:
            raise ValueError(f"Utilisateurs inconnus: {', '.join(unknown_members)}")
        _validate_single_group_membership(normalized_members, groups)

        group = _normalize_group(
            {
                "name": normalized_name,
                "description": description,
                "members": normalized_members,
                "vm_ids": vm_ids or [],
                "lab_permissions": lab_permissions or [],
                "created_at": _utc_now(),
            }
        )
        if not group:
            raise ValueError("Configuration de groupe invalide")

        groups[normalized_name] = group
        _save_storage(storage)
        return _public_group_payload(group, users)


def update_group(
    name: str,
    *,
    new_name: str | None = None,
    description: str | None = None,
    members: list[str] | None = None,
    vm_ids: list[str] | None = None,
    lab_permissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_name = _normalize_group_name(name)
    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        groups = storage["groups"]
        current = groups.get(normalized_name)
        if not current:
            raise RuntimeError("Groupe introuvable")

        target_name = normalized_name
        if new_name is not None:
            target_name = _normalize_group_name(new_name)
            if not target_name or not _GROUP_NAME_RE.match(target_name):
                raise ValueError("Le nom du groupe doit contenir uniquement lettres, chiffres, tirets ou underscores")
            if target_name != normalized_name and target_name in groups:
                raise RuntimeError("Un groupe avec ce nom existe deja")

        target_members = current.get("members", []) if members is None else sorted({_normalize_username(item) for item in members if _normalize_username(item)})
        unknown_members = [member for member in target_members if member not in users]
        if unknown_members:
            raise ValueError(f"Utilisateurs inconnus: {', '.join(unknown_members)}")
        _validate_single_group_membership(target_members, groups, ignore_group=normalized_name)

        candidate = _normalize_group(
            {
                "name": target_name,
                "description": current.get("description", "") if description is None else description,
                "members": target_members,
                "vm_ids": current.get("vm_ids", []) if vm_ids is None else vm_ids,
                "lab_permissions": current.get("lab_permissions", []) if lab_permissions is None else lab_permissions,
                "created_at": current.get("created_at") or _utc_now(),
            }
        )
        if not candidate:
            raise ValueError("Configuration de groupe invalide")

        if target_name != normalized_name:
            groups.pop(normalized_name, None)
        groups[target_name] = candidate
        _save_storage(storage)
        return _public_group_payload(candidate, users)


def delete_group(name: str) -> None:
    normalized_name = _normalize_group_name(name)
    with _USERS_LOCK:
        storage = _load_storage()
        removed = storage["groups"].pop(normalized_name, None)
        if not removed:
            raise RuntimeError("Groupe introuvable")
        _save_storage(storage)


def filter_items_for_user(items: list[dict[str, Any]], user: AuthenticatedUser) -> list[dict[str, Any]]:
    if user.role == "admin":
        return items

    filtered_items: list[dict[str, Any]] = []
    for item in items:
        vm_id = str(item.get("id") or "")
        if not user.can_access_vm(vm_id):
            continue

        clone = dict(item)
        labs = list(item.get("labs") or [])
        if vm_id in user.full_vm_access:
            clone["labs"] = labs
        else:
            clone["labs"] = [
                lab
                for lab in labs
                if user.can_access_lab(vm_id, str(lab.get("name") or ""))
            ]

        if vm_id not in user.full_vm_access and not clone["labs"]:
            continue
        filtered_items.append(clone)

    return filtered_items


def user_visible_vm_ids(user: AuthenticatedUser) -> set[str]:
    if user.role == "admin":
        agents = get_settings().get("agents", [])
        return {str(agent.get("id") or "") for agent in agents if str(agent.get("id") or "")}
    return set(user.full_vm_access) | set(user.lab_access.keys())


def require_admin_or_group_admin(request: Request) -> AuthenticatedUser:
    user = get_current_user(request)
    if user.role not in {"admin", "group-admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces reserve aux administrateurs")
    return user


def can_group_admin_manage_user(actor: AuthenticatedUser, target_username: str) -> bool:
    """Return True if a group-admin can manage target user (same group, non-admin account)."""
    if actor.role == "admin":
        return True
    if actor.role != "group-admin":
        return False

    normalized_target = _normalize_username(target_username)
    if not normalized_target:
        return False

    with _USERS_LOCK:
        storage = _load_storage()
        users = storage["users"]
        groups = storage["groups"]

        actor_record = users.get(_normalize_username(actor.username))
        target_record = users.get(normalized_target)
        if not actor_record or not target_record:
            return False

        if target_record.get("role") in {"admin", "group-admin"}:
            return False

        actor_group_names = {group["name"] for group in _groups_for_user(actor_record["username"], groups)}
        if not actor_group_names:
            return False

        target_group_names = {group["name"] for group in _groups_for_user(target_record["username"], groups)}
        return bool(actor_group_names & target_group_names)


def get_current_user(request: Request) -> AuthenticatedUser:
    username = _normalize_username(request.session.get("username"))
    last_activity = request.session.get("last_activity")
    
    # Check session timeout (3 hours of inactivity)
    if last_activity is not None:
        try:
            last_activity_time = float(last_activity)
            elapsed = time.monotonic() - last_activity_time
            if elapsed > _SESSION_TIMEOUT_SECONDS:
                request.session.clear()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Votre session a expiré. Veuillez vous reconnecter."
                )
        except (ValueError, TypeError):
            request.session.clear()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise")
    
    user = get_user(username)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise")

    if user.must_change_password and not _is_password_change_exempt_path(request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "password_change_required",
                "message": "Vous devez changer votre mot de passe avant de continuer.",
            },
        )
    
    # Update last activity timestamp
    request.session["last_activity"] = time.monotonic()
    
    return user


def require_admin(request: Request) -> AuthenticatedUser:
    user = get_current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces reserve aux administrateurs")
    return user
