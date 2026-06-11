from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from src.config import get_settings

_SANDBOX_LOCK = Lock()


def _default_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "sandbox_labs.json"


def _storage_path() -> Path:
    configured = os.getenv("CENTRAL_SANDBOX_LABS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    auth_settings = get_settings().get("auth", {})
    configured_path = str(auth_settings.get("sandbox_labs_file") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    return _default_path()


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


def is_enabled(vm_id: str, lab_name: str) -> bool:
    with _SANDBOX_LOCK:
        data = _load()
        return bool((data.get("labs") or {}).get(_key(vm_id, lab_name), False))


def is_modified(vm_id: str, lab_name: str) -> bool:
    with _SANDBOX_LOCK:
        data = _load()
        modified = data.get("modified") or {}
        return bool(modified.get(_key(vm_id, lab_name), False))


def set_enabled(vm_id: str, lab_name: str, enabled: bool) -> None:
    with _SANDBOX_LOCK:
        data = _load()
        labs = data.setdefault("labs", {})
        modified = data.setdefault("modified", {})
        key = _key(vm_id, lab_name)
        if enabled:
            labs[key] = True
        else:
            labs.pop(key, None)
            modified.pop(key, None)
        _save(data)


def set_modified(vm_id: str, lab_name: str, modified: bool) -> None:
    with _SANDBOX_LOCK:
        data = _load()
        key = _key(vm_id, lab_name)
        modified_map = data.setdefault("modified", {})
        if modified:
            modified_map[key] = True
        else:
            modified_map.pop(key, None)
        _save(data)


def list_enabled_keys() -> list[str]:
    with _SANDBOX_LOCK:
        data = _load()
        labs = data.get("labs") or {}
        return sorted([key for key, value in labs.items() if bool(value)])


def attach_flags(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with _SANDBOX_LOCK:
        data = _load()
        labs = data.get("labs") or {}
        enabled = {key for key, value in labs.items() if bool(value)}
        modified = data.get("modified") or {}
        changed = {key for key, value in modified.items() if bool(value)}

    out: list[dict[str, Any]] = []
    for item in items:
        vm_id = str(item.get("id") or item.get("vm_id") or "")
        labs_out = []
        for lab in item.get("labs") or []:
            lab_name = str(lab.get("name") or "")
            labs_out.append({
                **lab,
                "sandbox_enabled": _key(vm_id, lab_name) in enabled,
                "sandbox_yaml_modified": _key(vm_id, lab_name) in changed,
            })
        out.append({**item, "labs": labs_out})
    return out
