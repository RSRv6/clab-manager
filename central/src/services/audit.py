from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import Request

from src.config import get_settings
from src.services.auth import AuthenticatedUser
from src.services.sqlite_store import ensure_ready as ensure_sqlite_ready, with_connection

_AUDIT_LOCK = Lock()

_TRUSTED_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip")


def _get_client_ip(request: Request | None) -> str:
    """Return the real client IP, honouring X-Forwarded-For when behind a proxy."""
    if not request:
        return ""
    for header in _TRUSTED_PROXY_HEADERS:
        value = request.headers.get(header, "").strip()
        if value:
            # X-Forwarded-For may contain a comma-separated list; take the first.
            return value.split(",")[0].strip()
    return request.client.host if request.client else ""


def _default_audit_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "audit.log.jsonl"


def _audit_path() -> Path:
    configured = os.getenv("CENTRAL_AUDIT_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()

    auth_settings = get_settings().get("auth", {})
    configured_path = str(auth_settings.get("audit_file") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()

    return _default_audit_path()


def _ensure_storage_dir() -> Path:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_db_ready() -> None:
    ensure_sqlite_ready(audit_jsonl_path=_ensure_storage_dir())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_event(
    *,
    request: Request | None,
    user: AuthenticatedUser | None,
    action: str,
    status: str,
    vm_id: str | None = None,
    lab_name: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "timestamp": _now(),
        "action": action,
        "status": status,
        "vm_id": vm_id or "",
        "lab_name": lab_name or "",
        "user": user.to_dict() if user else None,
        "client_ip": _get_client_ip(request),
        "path": request.url.path if request else "",
        "method": request.method if request else "",
        "details": details or {},
    }

    _ensure_db_ready()
    username_lower = str(((event.get("user") or {}).get("username") or "")).strip().lower()
    with _AUDIT_LOCK:
        with with_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_events(
                    timestamp, action, status, vm_id, lab_name,
                    username_lower, user_json, client_ip, path, method, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.get("timestamp") or ""),
                    str(event.get("action") or ""),
                    str(event.get("status") or ""),
                    str(event.get("vm_id") or ""),
                    str(event.get("lab_name") or ""),
                    username_lower,
                    json.dumps(event.get("user"), ensure_ascii=True),
                    str(event.get("client_ip") or ""),
                    str(event.get("path") or ""),
                    str(event.get("method") or ""),
                    json.dumps(event.get("details") or {}, ensure_ascii=True),
                ),
            )
            conn.commit()
    return event


def list_events(
    limit: int = 200,
    username: str | None = None,
    vm_id: str | None = None,
    action_prefix: str | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 200), 1000))
    _ensure_db_ready()

    filter_username = str(username or "").strip().lower() or None
    filter_vm_id = str(vm_id or "").strip() or None
    filter_action_prefix = str(action_prefix or "").strip() or None

    events: list[dict[str, Any]] = []
    clauses: list[str] = []
    params: list[Any] = []
    if filter_username is not None:
        clauses.append("username_lower = ?")
        params.append(filter_username)
    if filter_vm_id is not None:
        clauses.append("vm_id = ?")
        params.append(filter_vm_id)
    if filter_action_prefix is not None:
        clauses.append("action LIKE ?")
        params.append(f"{filter_action_prefix}%")

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    sql = (
        "SELECT timestamp, action, status, vm_id, lab_name, user_json, client_ip, path, method, details_json "
        f"FROM audit_events {where_sql} ORDER BY id DESC LIMIT ?"
    )
    params.append(safe_limit)

    with _AUDIT_LOCK:
        with with_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

    for row in rows:
        try:
            user_obj = json.loads(str(row["user_json"] or "null"))
        except json.JSONDecodeError:
            user_obj = None
        try:
            details_obj = json.loads(str(row["details_json"] or "{}"))
        except json.JSONDecodeError:
            details_obj = {}
        events.append(
            {
                "timestamp": str(row["timestamp"] or ""),
                "action": str(row["action"] or ""),
                "status": str(row["status"] or ""),
                "vm_id": str(row["vm_id"] or ""),
                "lab_name": str(row["lab_name"] or ""),
                "user": user_obj,
                "client_ip": str(row["client_ip"] or ""),
                "path": str(row["path"] or ""),
                "method": str(row["method"] or ""),
                "details": details_obj if isinstance(details_obj, dict) else {},
            }
        )
    return events
