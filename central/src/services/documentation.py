from __future__ import annotations

import json
import os
import re
from threading import Lock
from typing import Any

_DOCS_LOCK = Lock()


def _docs_file_path() -> str:
    return os.getenv("CENTRAL_LAB_DOCS_FILE", "data/lab_documentation.json")


def _pdf_dir_path() -> str:
    return os.getenv("CENTRAL_LAB_DOCS_PDF_DIR", "data/lab_documentation_pdf")


def _normalize_key(vm_id: str, lab_name: str) -> str:
    return f"{str(vm_id or '').strip()}::{str(lab_name or '').strip()}"


def _sanitize_filename(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "document.pdf"
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw)
    cleaned = cleaned.strip("-._")
    if not cleaned:
        cleaned = "document.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def _load_raw() -> dict[str, Any]:
    path = _docs_file_path()
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except FileNotFoundError:
        payload = {}
    except json.JSONDecodeError:
        payload = {}

    if not isinstance(payload, dict):
        payload = {}
    docs = payload.get("docs")
    if not isinstance(docs, dict):
        docs = {}
    general_section = payload.get("general_section")
    if not isinstance(general_section, dict):
        general_section = {}
    return {"docs": docs, "general_section": general_section}


def _persist_raw(payload: dict[str, Any]) -> None:
    path = _docs_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        file.write(text)
    os.replace(tmp, path)


def _normalize_entry(entry: dict[str, Any] | None, vm_id: str, lab_name: str) -> dict[str, Any]:
    data = dict(entry or {})
    return {
        "vm_id": str(data.get("vm_id") or vm_id),
        "lab_name": str(data.get("lab_name") or lab_name),
        "text": str(data.get("text") or ""),
        "diagram": str(data.get("diagram") or ""),
        "pdf_file": str(data.get("pdf_file") or ""),
        "pdf_name": str(data.get("pdf_name") or ""),
        "updated_at": str(data.get("updated_at") or ""),
        "updated_by": str(data.get("updated_by") or ""),
    }


def get_general_section() -> dict[str, Any]:
    with _DOCS_LOCK:
        payload = _load_raw()
        section = payload.get("general_section") or {}
        return {
            "title": str(section.get("title") or ""),
            "text": str(section.get("text") or ""),
            "updated_at": str(section.get("updated_at") or ""),
            "updated_by": str(section.get("updated_by") or ""),
        }


def upsert_general_section(title: str, text: str, updated_by: str, updated_at: str) -> dict[str, Any]:
    with _DOCS_LOCK:
        payload = _load_raw()
        payload["general_section"] = {
            "title": str(title or ""),
            "text": str(text or ""),
            "updated_by": str(updated_by or ""),
            "updated_at": str(updated_at or ""),
        }
        _persist_raw(payload)
        return dict(payload["general_section"])


def get_doc(vm_id: str, lab_name: str) -> dict[str, Any]:
    key = _normalize_key(vm_id, lab_name)
    with _DOCS_LOCK:
        payload = _load_raw()
        return _normalize_entry(payload["docs"].get(key), vm_id, lab_name)


def list_docs_map() -> dict[str, dict[str, Any]]:
    with _DOCS_LOCK:
        payload = _load_raw()
        result: dict[str, dict[str, Any]] = {}
        for key, value in payload["docs"].items():
            if not isinstance(value, dict):
                continue
            vm_id = str(value.get("vm_id") or "")
            lab_name = str(value.get("lab_name") or "")
            result[str(key)] = _normalize_entry(value, vm_id, lab_name)
        return result


def upsert_text(vm_id: str, lab_name: str, text: str, diagram: str, updated_by: str, updated_at: str) -> dict[str, Any]:
    key = _normalize_key(vm_id, lab_name)
    with _DOCS_LOCK:
        payload = _load_raw()
        current = _normalize_entry(payload["docs"].get(key), vm_id, lab_name)
        current["text"] = str(text or "")
        current["diagram"] = str(diagram or "")
        current["updated_by"] = str(updated_by or "")
        current["updated_at"] = str(updated_at or "")
        payload["docs"][key] = current
        _persist_raw(payload)
        return dict(current)


def save_pdf(vm_id: str, lab_name: str, original_name: str, content: bytes, updated_by: str, updated_at: str) -> dict[str, Any]:
    key = _normalize_key(vm_id, lab_name)
    file_name = _sanitize_filename(f"{vm_id}-{lab_name}-{updated_at}.pdf")
    pdf_dir = _pdf_dir_path()
    os.makedirs(pdf_dir, exist_ok=True)
    file_path = os.path.join(pdf_dir, file_name)

    with _DOCS_LOCK:
        with open(file_path, "wb") as file:
            file.write(content)

        payload = _load_raw()
        current = _normalize_entry(payload["docs"].get(key), vm_id, lab_name)

        previous_file = str(current.get("pdf_file") or "")
        if previous_file and previous_file != file_name:
            previous_path = os.path.join(pdf_dir, previous_file)
            if os.path.isfile(previous_path):
                try:
                    os.remove(previous_path)
                except OSError:
                    pass

        current["pdf_file"] = file_name
        current["pdf_name"] = _sanitize_filename(original_name)
        current["updated_by"] = str(updated_by or "")
        current["updated_at"] = str(updated_at or "")
        payload["docs"][key] = current
        _persist_raw(payload)
        return dict(current)


def remove_pdf(vm_id: str, lab_name: str, updated_by: str, updated_at: str) -> dict[str, Any]:
    key = _normalize_key(vm_id, lab_name)
    pdf_dir = _pdf_dir_path()

    with _DOCS_LOCK:
        payload = _load_raw()
        current = _normalize_entry(payload["docs"].get(key), vm_id, lab_name)
        previous_file = str(current.get("pdf_file") or "")
        if previous_file:
            previous_path = os.path.join(pdf_dir, previous_file)
            if os.path.isfile(previous_path):
                try:
                    os.remove(previous_path)
                except OSError:
                    pass

        current["pdf_file"] = ""
        current["pdf_name"] = ""
        current["updated_by"] = str(updated_by or "")
        current["updated_at"] = str(updated_at or "")
        payload["docs"][key] = current
        _persist_raw(payload)
        return dict(current)


def resolve_pdf_path(entry: dict[str, Any]) -> str:
    file_name = str((entry or {}).get("pdf_file") or "")
    if not file_name:
        return ""
    path = os.path.join(_pdf_dir_path(), file_name)
    if not os.path.isfile(path):
        return ""
    return path
