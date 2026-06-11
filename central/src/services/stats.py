"""Compute usage statistics from persisted audit events.

Primary source is SQLite audit_events for consistency with runtime writes.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any

from src.services.audit import _audit_path
from src.services.sqlite_store import db_path, ensure_ready as ensure_sqlite_ready, with_connection


_STATS_CACHE_LOCK = Lock()
_STATS_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_STATS_CACHE_TTL_SECONDS = 15.0


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _ensure_db_ready() -> None:
    ensure_sqlite_ready(audit_jsonl_path=_audit_path())


def _db_file_stamp() -> tuple[str, int, int]:
    path = db_path()
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return str(path), 0, 0


def _load_all_events() -> list[dict[str, Any]]:
    _ensure_db_ready()

    events: list[dict[str, Any]] = []
    with with_connection() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, action, status, vm_id, lab_name, user_json, client_ip, path, method, details_json
            FROM audit_events
            ORDER BY id ASC
            """
        ).fetchall()

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


def _bucket(dt: datetime, granularity: str) -> str:
    """Return ISO-formatted bucket key for a datetime."""
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "week":
        # ISO week Monday
        monday = dt - timedelta(days=dt.weekday())
        return monday.strftime("%Y-%m-%d")
    # month
    return dt.strftime("%Y-%m")


# ── main function ─────────────────────────────────────────────────────────────

def compute_stats(
    *,
    allowed_usernames: set[str] | None = None,
    allowed_vm_ids: set[str] | None = None,
) -> dict[str, Any]:
    user_scope = {str(username).strip().lower() for username in (allowed_usernames or set()) if str(username).strip()} if allowed_usernames is not None else None
    vm_scope = {str(vm_id).strip() for vm_id in (allowed_vm_ids or set()) if str(vm_id).strip()} if allowed_vm_ids is not None else None

    file_stamp = _db_file_stamp()

    cache_key = (
        file_stamp,
        tuple(sorted(user_scope)) if user_scope is not None else None,
        tuple(sorted(vm_scope)) if vm_scope is not None else None,
    )
    now_monotonic = datetime.now(timezone.utc).timestamp()
    with _STATS_CACHE_LOCK:
        cached = _STATS_CACHE.get(cache_key)
        if cached and now_monotonic - cached[0] <= _STATS_CACHE_TTL_SECONDS:
            return cached[1]

    events = _load_all_events()
    now = datetime.now(timezone.utc)

    # ── Aggregate logins ─────────────────────────────────────────────────────
    # Only auth.login with status ok
    logins_by_day: dict[str, int] = defaultdict(int)
    logins_by_week: dict[str, int] = defaultdict(int)
    logins_by_month: dict[str, int] = defaultdict(int)
    logins_per_user: dict[str, int] = defaultdict(int)   # username → count
    user_display: dict[str, str] = {}                     # username → full_name

    # Unique days active per user
    user_active_days: dict[str, set[str]] = defaultdict(set)

    for ev in events:
        if ev.get("action") != "auth.login" or ev.get("status") != "ok":
            continue
        ts = _parse_ts(ev.get("timestamp", ""))
        if ts is None:
            continue
        # username either from user dict (logged-in) or details.username (for denied — skipped)
        user_dict = ev.get("user") or {}
        username = str(user_dict.get("username") or "").strip().lower()
        if not username:
            username = str((ev.get("details") or {}).get("username") or "").strip().lower()
        if not username:
            continue

        if user_scope is not None and username not in user_scope:
            continue

        full_name = str(user_dict.get("full_name") or username)
        user_display[username] = full_name

        logins_by_day[_bucket(ts, "day")] += 1
        logins_by_week[_bucket(ts, "week")] += 1
        logins_by_month[_bucket(ts, "month")] += 1
        logins_per_user[username] += 1
        user_active_days[username].add(_bucket(ts, "day"))

    # ── Aggregate reservations ───────────────────────────────────────────────
    reservations_by_month: dict[str, int] = defaultdict(int)
    reservations_by_week: dict[str, int] = defaultdict(int)
    reservations_by_day: dict[str, int] = defaultdict(int)
    lab_reservation_count: dict[str, int] = defaultdict(int)  # "vm_id/lab_name" → count
    user_reservation_count: dict[str, int] = defaultdict(int)

    for ev in events:
        if ev.get("action") != "lab.reserve" or ev.get("status") != "ok":
            continue
        ts = _parse_ts(ev.get("timestamp", ""))
        if ts is None:
            continue
        vm_id = str(ev.get("vm_id") or "").strip()
        lab_name = str(ev.get("lab_name") or "").strip()
        lab_key = f"{vm_id}/{lab_name}" if vm_id and lab_name else lab_name or vm_id or "?"

        user_dict = ev.get("user") or {}
        username = str(user_dict.get("username") or "").strip().lower()
        full_name = str(user_dict.get("full_name") or username)

        if user_scope is not None and username not in user_scope:
            continue
        if vm_scope is not None and vm_id not in vm_scope:
            continue

        if username:
            user_display[username] = full_name

        reservations_by_day[_bucket(ts, "day")] += 1
        reservations_by_week[_bucket(ts, "week")] += 1
        reservations_by_month[_bucket(ts, "month")] += 1
        lab_reservation_count[lab_key] += 1
        if username:
            user_reservation_count[username] += 1

    # ── Build sorted-bucket series (last 30 days / 12 weeks / 12 months) ─────
    def _sorted_series(d: dict[str, int]) -> list[dict[str, Any]]:
        return [{"bucket": k, "count": v} for k, v in sorted(d.items())]

    def _last_n_daily(d: dict[str, int], n: int = 30) -> list[dict[str, Any]]:
        today = now.date()
        result = []
        for i in range(n - 1, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            result.append({"bucket": day, "count": d.get(day, 0)})
        return result

    def _last_n_weekly(d: dict[str, int], n: int = 12) -> list[dict[str, Any]]:
        # Last n Mondays
        today = now.date()
        monday = today - timedelta(days=today.weekday())
        result = []
        for i in range(n - 1, -1, -1):
            week_start = (monday - timedelta(weeks=i)).isoformat()
            result.append({"bucket": week_start, "count": d.get(week_start, 0)})
        return result

    def _last_n_monthly(d: dict[str, int], n: int = 12) -> list[dict[str, Any]]:
        result = []
        for i in range(n - 1, -1, -1):
            # subtract i months
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            key = f"{y:04d}-{m:02d}"
            result.append({"bucket": key, "count": d.get(key, 0)})
        return result

    # ── Per-user login stats ──────────────────────────────────────────────────
    top_users_by_logins = sorted(
        [
            {
                "username": u,
                "full_name": user_display.get(u, u),
                "login_count": c,
                "active_days": len(user_active_days.get(u, set())),
            }
            for u, c in logins_per_user.items()
        ],
        key=lambda x: x["login_count"],
        reverse=True,
    )

    # ── Per-user reservation stats ────────────────────────────────────────────
    top_users_by_reservations = sorted(
        [
            {
                "username": u,
                "full_name": user_display.get(u, u),
                "reservation_count": c,
            }
            for u, c in user_reservation_count.items()
        ],
        key=lambda x: x["reservation_count"],
        reverse=True,
    )

    # ── Top labs ──────────────────────────────────────────────────────────────
    top_labs = sorted(
        [{"lab_key": k, "count": v} for k, v in lab_reservation_count.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:20]

    # ── Summary numbers ───────────────────────────────────────────────────────
    total_logins = sum(logins_per_user.values())
    total_reservations = sum(lab_reservation_count.values())

    result = {
        "generated_at": now.isoformat(),
        "summary": {
            "total_logins": total_logins,
            "total_reservations": total_reservations,
            "distinct_users_logged_in": len(logins_per_user),
            "distinct_labs_reserved": len(lab_reservation_count),
        },
        "logins": {
            "by_day": _last_n_daily(logins_by_day, 30),
            "by_week": _last_n_weekly(logins_by_week, 12),
            "by_month": _last_n_monthly(logins_by_month, 12),
            "top_users": top_users_by_logins,
        },
        "reservations": {
            "by_day": _last_n_daily(reservations_by_day, 30),
            "by_week": _last_n_weekly(reservations_by_week, 12),
            "by_month": _last_n_monthly(reservations_by_month, 12),
            "top_labs": top_labs,
            "top_users": top_users_by_reservations,
        },
    }

    with _STATS_CACHE_LOCK:
        _STATS_CACHE[cache_key] = (now_monotonic, result)
        # Drop very old cache entries opportunistically to keep memory bounded.
        stale_before = now_monotonic - (_STATS_CACHE_TTL_SECONDS * 4)
        stale_keys = [key for key, (ts, _) in _STATS_CACHE.items() if ts < stale_before]
        for key in stale_keys:
            _STATS_CACHE.pop(key, None)

    return result
