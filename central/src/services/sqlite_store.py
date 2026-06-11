from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

_DB_LOCK = Lock()


def _default_db_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "vlm.db"


def db_path() -> Path:
    configured = os.getenv("CENTRAL_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _default_db_path()


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _maybe_rotate_file(path: Path, suffix: str) -> None:
    if not path.exists():
        return
    rotated = path.with_name(f"{path.name}.{suffix}")
    try:
        path.replace(rotated)
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            vm_id TEXT NOT NULL,
            lab_name TEXT NOT NULL,
            username_lower TEXT NOT NULL,
            user_json TEXT NOT NULL,
            client_ip TEXT NOT NULL,
            path TEXT NOT NULL,
            method TEXT NOT NULL,
            details_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_events(username_lower)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_vm_id ON audit_events(vm_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS central_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cpu_percent REAL,
            memory_json TEXT NOT NULL,
            disk_json TEXT NOT NULL,
            load_avg_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON central_metrics(timestamp)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vm_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vm_id TEXT NOT NULL,
            vm_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cpu_percent REAL,
            memory_json TEXT NOT NULL,
            disk_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_metrics_vm_ts ON vm_metrics(vm_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_metrics_timestamp ON vm_metrics(timestamp)")


def _is_migration_done(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM migrations WHERE name = ?", (name,)).fetchone()
    return row is not None


def _mark_migration_done(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO migrations(name, applied_at) VALUES(?, ?)",
        (name, datetime.now(timezone.utc).isoformat()),
    )


def _migrate_audit_jsonl(conn: sqlite3.Connection, path: Path) -> None:
    migration_name = "audit_jsonl_to_sqlite_v1"
    if _is_migration_done(conn, migration_name):
        return
    if not path.exists():
        _mark_migration_done(conn, migration_name)
        return

    with path.open("r", encoding="utf-8") as file:
        for raw in file:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            user_obj = event.get("user") if isinstance(event.get("user"), dict) else None
            username_lower = str((user_obj or {}).get("username") or "").strip().lower()
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
                    json.dumps(user_obj, ensure_ascii=True),
                    str(event.get("client_ip") or ""),
                    str(event.get("path") or ""),
                    str(event.get("method") or ""),
                    json.dumps(event.get("details") or {}, ensure_ascii=True),
                ),
            )

    _mark_migration_done(conn, migration_name)


def _migrate_central_metrics_jsonl(conn: sqlite3.Connection, path: Path) -> None:
    migration_name = "central_metrics_jsonl_to_sqlite_v1"
    if _is_migration_done(conn, migration_name):
        return
    if not path.exists():
        _mark_migration_done(conn, migration_name)
        return

    with path.open("r", encoding="utf-8") as file:
        for raw in file:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue

            conn.execute(
                """
                INSERT INTO central_metrics(
                    timestamp, cpu_percent, memory_json, disk_json, load_avg_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(item.get("timestamp") or ""),
                    item.get("cpu_percent"),
                    json.dumps(item.get("memory") or {}, ensure_ascii=True),
                    json.dumps(item.get("disk") or {}, ensure_ascii=True),
                    json.dumps(item.get("load_avg") or {}, ensure_ascii=True),
                ),
            )

    _mark_migration_done(conn, migration_name)


def ensure_ready(
    audit_jsonl_path: Path | None = None,
    central_metrics_jsonl_path: Path | None = None,
) -> None:
    with _DB_LOCK:
        _ensure_ready_locked(
            audit_jsonl_path=audit_jsonl_path,
            central_metrics_jsonl_path=central_metrics_jsonl_path,
        )


def _ensure_ready_locked(
    audit_jsonl_path: Path | None = None,
    central_metrics_jsonl_path: Path | None = None,
) -> None:
    with _connect() as conn:
        _create_schema(conn)
        if audit_jsonl_path is not None:
            _migrate_audit_jsonl(conn, audit_jsonl_path)
        if central_metrics_jsonl_path is not None:
            _migrate_central_metrics_jsonl(conn, central_metrics_jsonl_path)
        conn.commit()


def recover_database(
    *,
    reason: str = "",
    audit_jsonl_path: Path | None = None,
    central_metrics_jsonl_path: Path | None = None,
) -> Path:
    path = db_path()
    suffix = f"corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    with _DB_LOCK:
        _maybe_rotate_file(path, suffix)
        _maybe_rotate_file(path.with_name(f"{path.name}-wal"), suffix)
        _maybe_rotate_file(path.with_name(f"{path.name}-shm"), suffix)
        _ensure_ready_locked(
            audit_jsonl_path=audit_jsonl_path,
            central_metrics_jsonl_path=central_metrics_jsonl_path,
        )
    return path


def with_connection() -> sqlite3.Connection:
    return _connect()
