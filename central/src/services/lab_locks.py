from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from src.config import get_settings

_LAB_LOCKS_LOCK = Lock()


def _default_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "lab_locks.json"


def _storage_path() -> Path:
    configured = os.getenv("CENTRAL_LAB_LOCKS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    auth_settings = get_settings().get("auth", {})
    configured_path = str(auth_settings.get("lab_locks_file") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    return _default_path()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict[str, Any]:
    path = _storage_path()
    if not path.exists():
        return {"labs": {}}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            return {"labs": {}}
        labs = data.get("labs")
        if not isinstance(labs, dict):
            data["labs"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"labs": {}}


def _save(data: dict[str, Any]) -> None:
    path = _storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=True, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _key(vm_id: str, lab_name: str) -> str:
    return f"{vm_id}::{lab_name}"


def get_lock(vm_id: str, lab_name: str) -> dict[str, Any]:
    with _LAB_LOCKS_LOCK:
        data = _load()
        labs = data.get("labs") or {}
        entry = labs.get(_key(vm_id, lab_name))
        if not isinstance(entry, dict):
            return {"locked": False}
        return {
            "locked": bool(entry.get("locked", False)),
            "locked_by": str(entry.get("locked_by") or ""),
            "locked_at": str(entry.get("locked_at") or ""),
        }


def is_locked(vm_id: str, lab_name: str) -> bool:
    return bool(get_lock(vm_id, lab_name).get("locked", False))


def set_locked(vm_id: str, lab_name: str, locked: bool, actor_username: str | None = None) -> dict[str, Any]:
    with _LAB_LOCKS_LOCK:
        data = _load()
        labs = data.setdefault("labs", {})
        key = _key(vm_id, lab_name)
        if locked:
            labs[key] = {
                "locked": True,
                "locked_by": str(actor_username or ""),
                "locked_at": _utc_now(),
            }
        else:
            labs.pop(key, None)
        _save(data)

    return get_lock(vm_id, lab_name)


def attach_flags(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with _LAB_LOCKS_LOCK:
        data = _load()
        labs = data.get("labs") or {}

    out: list[dict[str, Any]] = []
    for item in items:
        vm_id = str(item.get("id") or item.get("vm_id") or "")
        labs_out = []
        for lab in item.get("labs") or []:
            lab_name = str(lab.get("name") or "")
            entry = labs.get(_key(vm_id, lab_name))
            lock_info = entry if isinstance(entry, dict) else {}
            labs_out.append(
                {
                    **lab,
                    "lab_locked": bool(lock_info.get("locked", False)),
                    "lab_lock": {
                        "locked": bool(lock_info.get("locked", False)),
                        "locked_by": str(lock_info.get("locked_by") or ""),
                        "locked_at": str(lock_info.get("locked_at") or ""),
                    },
                }
            )
        out.append({**item, "labs": labs_out})
    return out
