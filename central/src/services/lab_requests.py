from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from src.config import get_settings

_LAB_REQUESTS_LOCK = Lock()

_ALLOWED_STATUSES = {
    "pris_en_compte",
    "en_cours_etude",
    "reponse",
}


def _default_lab_requests_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "lab_requests.json"


def _lab_requests_path() -> Path:
    configured = os.getenv("CENTRAL_LAB_REQUESTS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()

    auth_settings = get_settings().get("auth", {})
    configured_path = str(auth_settings.get("lab_requests_file") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()

    return _default_lab_requests_path()


def _load_items() -> list[dict[str, Any]]:
    path = _lab_requests_path()
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return []

    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else []
    else:
        items = []

    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(dict(item))
    return normalized


def _save_items(items: list[dict[str, Any]]) -> None:
    path = _lab_requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": items}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_status(value: str | None) -> str:
    status = str(value or "").strip().lower()
    if status not in _ALLOWED_STATUSES:
        raise ValueError("Statut invalide")
    return status


def _sort_desc(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )


def create_request(
    *,
    owner_username: str,
    owner_full_name: str,
    owner_email: str,
    suggestion_title: str,
    need_details: str,
    resources_requirements: str,
    requested_router_images: str,
) -> dict[str, Any]:
    now = _utc_now_iso()
    owner = str(owner_username or "").strip().lower()
    if not owner:
        raise ValueError("Utilisateur invalide")

    details = str(need_details or "").strip()
    if len(details) < 8:
        raise ValueError("La description du besoin est trop courte")

    title = str(suggestion_title or "").strip() or "Suggestion LAB"

    item = {
        "id": uuid4().hex[:12],
        "owner_username": owner,
        "owner_full_name": str(owner_full_name or "").strip(),
        "owner_email": str(owner_email or "").strip(),
        "suggestion_title": title,
        "need_details": details,
        "resources_requirements": str(resources_requirements or "").strip(),
        "requested_router_images": str(requested_router_images or "").strip(),
        "status": "pris_en_compte",
        "assigned_admin_username": None,
        "assigned_admin_full_name": None,
        "assigned_at": None,
        "admin_response": "",
        "created_at": now,
        "updated_at": now,
        "updated_by": owner,
    }

    with _LAB_REQUESTS_LOCK:
        items = _load_items()
        items.append(item)
        _save_items(_sort_desc(items))

    return dict(item)


def list_requests(*, actor_username: str, actor_is_admin: bool) -> list[dict[str, Any]]:
    actor = str(actor_username or "").strip().lower()
    with _LAB_REQUESTS_LOCK:
        items = _load_items()

    visible: list[dict[str, Any]] = []
    for item in items:
        owner = str(item.get("owner_username") or "").strip().lower()
        if actor_is_admin or owner == actor:
            visible.append(dict(item))

    return _sort_desc(visible)


def get_request(*, request_id: str) -> dict[str, Any] | None:
    key = str(request_id or "").strip()
    if not key:
        return None

    with _LAB_REQUESTS_LOCK:
        items = _load_items()

    for item in items:
        if str(item.get("id") or "").strip() == key:
            return dict(item)
    return None


def claim_request(
    *,
    request_id: str,
    admin_username: str,
    admin_full_name: str,
) -> dict[str, Any]:
    req_id = str(request_id or "").strip()
    admin_user = str(admin_username or "").strip().lower()
    if not req_id:
        raise ValueError("Demande invalide")
    if not admin_user:
        raise ValueError("Administrateur invalide")

    with _LAB_REQUESTS_LOCK:
        items = _load_items()
        for index, item in enumerate(items):
            if str(item.get("id") or "").strip() != req_id:
                continue

            now = _utc_now_iso()
            updated = dict(item)
            updated["assigned_admin_username"] = admin_user
            updated["assigned_admin_full_name"] = str(admin_full_name or "").strip() or admin_user
            updated["assigned_at"] = now
            updated["updated_at"] = now
            updated["updated_by"] = admin_user
            if str(updated.get("status") or "").strip().lower() not in _ALLOWED_STATUSES:
                updated["status"] = "pris_en_compte"

            items[index] = updated
            _save_items(_sort_desc(items))
            return dict(updated)

    raise RuntimeError("Demande introuvable")


def update_request_by_admin(
    *,
    request_id: str,
    admin_username: str,
    admin_full_name: str,
    status: str | None,
    admin_response: str | None,
) -> dict[str, Any]:
    req_id = str(request_id or "").strip()
    admin_user = str(admin_username or "").strip().lower()
    if not req_id:
        raise ValueError("Demande invalide")
    if not admin_user:
        raise ValueError("Administrateur invalide")

    normalized_status = None
    if status is not None:
        normalized_status = _normalize_status(status)

    with _LAB_REQUESTS_LOCK:
        items = _load_items()
        for index, item in enumerate(items):
            if str(item.get("id") or "").strip() != req_id:
                continue

            updated = dict(item)
            if normalized_status is not None:
                updated["status"] = normalized_status

            if admin_response is not None:
                updated["admin_response"] = str(admin_response).strip()

            now = _utc_now_iso()
            updated["updated_at"] = now
            updated["updated_by"] = admin_user

            if not updated.get("assigned_admin_username"):
                updated["assigned_admin_username"] = admin_user
                updated["assigned_admin_full_name"] = str(admin_full_name or "").strip() or admin_user
                updated["assigned_at"] = now

            items[index] = updated
            _save_items(_sort_desc(items))
            return dict(updated)

    raise RuntimeError("Demande introuvable")


def delete_request(*, request_id: str) -> dict[str, Any]:
    req_id = str(request_id or "").strip()
    if not req_id:
        raise ValueError("Demande invalide")

    with _LAB_REQUESTS_LOCK:
        items = _load_items()
        for index, item in enumerate(items):
            if str(item.get("id") or "").strip() != req_id:
                continue

            removed = dict(item)
            items.pop(index)
            _save_items(_sort_desc(items))
            return removed

    raise RuntimeError("Demande introuvable")


def allowed_statuses() -> list[str]:
    return sorted(_ALLOWED_STATUSES)
