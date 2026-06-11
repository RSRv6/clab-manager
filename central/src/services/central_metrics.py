from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from src.services.sqlite_store import ensure_ready as ensure_sqlite_ready, recover_database, with_connection

logger = logging.getLogger(__name__)

_METRICS_LOCK = threading.Lock()
_SAMPLER_THREAD: threading.Thread | None = None
_SAMPLER_STOP = threading.Event()
_CPU_PREV: tuple[int, int] | None = None
_APPEND_COUNTER = 0


def _metrics_jsonl_path() -> Path:
    configured = os.getenv("CENTRAL_RESOURCE_METRICS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "central_resources.jsonl"


def _ensure_storage_dir() -> Path:
    path = _metrics_jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_db_ready() -> None:
    ensure_sqlite_ready(central_metrics_jsonl_path=_ensure_storage_dir())


def _audit_jsonl_path() -> Path:
    configured = os.getenv("CENTRAL_AUDIT_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "audit.log.jsonl"


def _recover_if_malformed(error: Exception) -> bool:
    message = str(error).lower()
    if "malformed" not in message and "disk image is malformed" not in message:
        return False
    try:
        recovered = recover_database(
            reason=message,
            audit_jsonl_path=_audit_jsonl_path(),
            central_metrics_jsonl_path=_ensure_storage_dir(),
        )
        logger.warning("central_metrics recovered malformed sqlite database at %s", recovered)
        return True
    except Exception:
        logger.exception("central_metrics failed to recover malformed sqlite database")
        return False


def _retention_hours() -> int:
    raw = os.getenv("CENTRAL_METRICS_RETENTION_HOURS", "720").strip()
    try:
        return max(24, int(raw))
    except ValueError:
        return 720


def _max_window_hours() -> int:
    raw = os.getenv("CENTRAL_METRICS_MAX_WINDOW_HOURS", "720").strip()
    try:
        return max(24, int(raw))
    except ValueError:
        return 720


def _prune_every_samples() -> int:
    raw = os.getenv("CENTRAL_METRICS_PRUNE_EVERY_SAMPLES", "60").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


def _read_proc_stat() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as file:
            first = file.readline().strip()
    except OSError:
        return None

    parts = first.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None

    values = [int(value) for value in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def _cpu_percent() -> float | None:
    global _CPU_PREV

    current = _read_proc_stat()
    if current is None:
        return None

    previous = _CPU_PREV
    _CPU_PREV = current
    if previous is None:
        return None

    prev_idle, prev_total = previous
    curr_idle, curr_total = current
    total_delta = curr_total - prev_total
    idle_delta = curr_idle - prev_idle
    if total_delta <= 0:
        return None

    usage = 100.0 * (1.0 - (idle_delta / total_delta))
    return max(0.0, min(100.0, usage))


def _memory_snapshot() -> dict[str, float | int | None]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as file:
            for raw in file:
                key, _, tail = raw.partition(":")
                tokens = tail.strip().split()
                if not tokens:
                    continue
                try:
                    values[key] = int(tokens[0])
                except ValueError:
                    continue
    except OSError:
        return {
            "percent": None,
            "used": None,
            "total": None,
        }

    total_kib = values.get("MemTotal", 0)
    available_kib = values.get("MemAvailable", 0)
    if total_kib <= 0:
        return {
            "percent": None,
            "used": None,
            "total": None,
        }

    used_kib = max(0, total_kib - available_kib)
    percent = 100.0 * (used_kib / total_kib)
    return {
        "percent": max(0.0, min(100.0, percent)),
        "used": used_kib * 1024,
        "total": total_kib * 1024,
    }


def _disk_snapshot() -> dict[str, float | int | None]:
    try:
        stat = os.statvfs("/")
    except OSError:
        return {
            "percent": None,
            "used": None,
            "total": None,
        }

    total = stat.f_blocks * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    used = max(0, total - available)
    percent = (100.0 * used / total) if total > 0 else None
    return {
        "percent": max(0.0, min(100.0, percent)) if percent is not None else None,
        "used": used,
        "total": total,
    }


def sample_now() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    cpu = _cpu_percent()
    memory = _memory_snapshot()
    disk = _disk_snapshot()
    load1, load5, load15 = os.getloadavg()

    return {
        "timestamp": now,
        "cpu_percent": cpu,
        "memory": memory,
        "disk": disk,
        "load_avg": {
            "one": load1,
            "five": load5,
            "fifteen": load15,
        },
    }


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _prune_history(max_hours: int | None = None) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours or _retention_hours())
    cutoff_iso = cutoff.isoformat()
    with with_connection() as conn:
        conn.execute("DELETE FROM central_metrics WHERE timestamp < ?", (cutoff_iso,))
        conn.execute("DELETE FROM vm_metrics WHERE timestamp < ?", (cutoff_iso,))
        conn.commit()


def append_vm_samples(items: list[dict[str, Any]]) -> None:
    _ensure_db_ready()
    now_iso = datetime.now(timezone.utc).isoformat()

    def _do_append() -> None:
        with _METRICS_LOCK:
            with with_connection() as conn:
                for item in items:
                    if not bool(item.get("online", False)):
                        continue
                    vm_id = str(item.get("id") or "").strip()
                    if not vm_id:
                        continue
                    vm_name = str(item.get("name") or vm_id)
                    resources = item.get("resources") if isinstance(item.get("resources"), dict) else {}
                    memory = resources.get("memory") if isinstance(resources.get("memory"), dict) else {}
                    disk = resources.get("disk") if isinstance(resources.get("disk"), dict) else {}
                    cpu = resources.get("cpu_percent")
                    conn.execute(
                        """
                        INSERT INTO vm_metrics(vm_id, vm_name, timestamp, cpu_percent, memory_json, disk_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            vm_id,
                            vm_name,
                            now_iso,
                            cpu,
                            json.dumps(memory, ensure_ascii=True),
                            json.dumps(disk, ensure_ascii=True),
                        ),
                    )
                conn.commit()

    try:
        _do_append()
    except sqlite3.DatabaseError as exc:
        if not _recover_if_malformed(exc):
            raise
        _do_append()


def get_vm_history(vm_id: str, hours: int = 24) -> list[dict[str, Any]]:
    _ensure_db_ready()
    vm_id_clean = str(vm_id or "").strip()
    if not vm_id_clean:
        return []

    safe_hours = max(1, min(int(hours or 24), _max_window_hours()))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=safe_hours)
    cutoff_iso = cutoff.isoformat()

    with _METRICS_LOCK:
        with with_connection() as conn:
            rows = conn.execute(
                """
                SELECT vm_id, vm_name, timestamp, cpu_percent, memory_json, disk_json
                FROM vm_metrics
                WHERE vm_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (vm_id_clean, cutoff_iso),
            ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            memory = json.loads(str(row["memory_json"] or "{}"))
        except json.JSONDecodeError:
            memory = {}
        try:
            disk = json.loads(str(row["disk_json"] or "{}"))
        except json.JSONDecodeError:
            disk = {}
        items.append(
            {
                "vm_id": str(row["vm_id"] or ""),
                "vm_name": str(row["vm_name"] or ""),
                "timestamp": str(row["timestamp"] or ""),
                "cpu_percent": row["cpu_percent"],
                "memory": memory if isinstance(memory, dict) else {},
                "disk": disk if isinstance(disk, dict) else {},
            }
        )
    return items


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _truncate_bucket(timestamp: datetime, bucket: str) -> datetime:
    ts_utc = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    ts_utc = ts_utc.astimezone(timezone.utc)
    if bucket == "day":
        return ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "minute":
        return ts_utc.replace(second=0, microsecond=0)
    return ts_utc.replace(minute=0, second=0, microsecond=0)


def _decode_vm_sample(row: Any) -> dict[str, Any]:
    try:
        memory = json.loads(str(row["memory_json"] or "{}"))
    except json.JSONDecodeError:
        memory = {}
    try:
        disk = json.loads(str(row["disk_json"] or "{}"))
    except json.JSONDecodeError:
        disk = {}
    return {
        "vm_id": str(row["vm_id"] or ""),
        "vm_name": str(row["vm_name"] or ""),
        "timestamp": str(row["timestamp"] or ""),
        "cpu_percent": row["cpu_percent"],
        "memory": memory if isinstance(memory, dict) else {},
        "disk": disk if isinstance(disk, dict) else {},
    }


def _fetch_latest_vm_sample(vm_id: str) -> dict[str, Any] | None:
    _ensure_db_ready()
    vm_id_clean = str(vm_id or "").strip()
    if not vm_id_clean:
        return None

    with _METRICS_LOCK:
        with with_connection() as conn:
            row = conn.execute(
                """
                SELECT vm_id, vm_name, timestamp, cpu_percent, memory_json, disk_json
                FROM vm_metrics
                WHERE vm_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (vm_id_clean,),
            ).fetchone()
    if row is None:
        return None
    return _decode_vm_sample(row)


def get_vm_history_aggregated(vm_id: str, hours: int = 24, bucket: str = "hour") -> list[dict[str, Any]]:
    _ensure_db_ready()
    vm_id_clean = str(vm_id or "").strip()
    if not vm_id_clean:
        return []

    safe_hours = max(1, min(int(hours or 24), _max_window_hours()))
    bucket_raw = str(bucket or "").strip().lower()
    if bucket_raw in {"minute", "hour", "day"}:
        bucket_clean = bucket_raw
    else:
        bucket_clean = "hour"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=safe_hours)
    cutoff_iso = cutoff.isoformat()

    with _METRICS_LOCK:
        with with_connection() as conn:
            rows = conn.execute(
                """
                SELECT vm_id, vm_name, timestamp, cpu_percent, memory_json, disk_json
                FROM vm_metrics
                WHERE vm_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (vm_id_clean, cutoff_iso),
            ).fetchall()

    aggregates: dict[str, dict[str, Any]] = {}
    for row in rows:
        timestamp_raw = str(row["timestamp"] or "")
        ts = _parse_timestamp(timestamp_raw)
        if ts is None:
            continue

        bucket_start = _truncate_bucket(ts, bucket_clean).isoformat()
        bucket_item = aggregates.get(bucket_start)
        if bucket_item is None:
            bucket_item = {
                "vm_id": str(row["vm_id"] or vm_id_clean),
                "vm_name": str(row["vm_name"] or vm_id_clean),
                "timestamp": bucket_start,
                "cpu_total": 0.0,
                "cpu_count": 0,
                "memory_percent_total": 0.0,
                "memory_percent_count": 0,
                "memory_used_total": 0.0,
                "memory_used_count": 0,
                "memory_total_total": 0.0,
                "memory_total_count": 0,
                "disk_percent_total": 0.0,
                "disk_percent_count": 0,
                "disk_used_total": 0.0,
                "disk_used_count": 0,
                "disk_total_total": 0.0,
                "disk_total_count": 0,
            }
            aggregates[bucket_start] = bucket_item

        cpu_value = _safe_float(row["cpu_percent"])
        if cpu_value is not None:
            bucket_item["cpu_total"] += cpu_value
            bucket_item["cpu_count"] += 1

        try:
            memory = json.loads(str(row["memory_json"] or "{}"))
        except json.JSONDecodeError:
            memory = {}
        if not isinstance(memory, dict):
            memory = {}

        memory_percent = _safe_float(memory.get("percent"))
        if memory_percent is not None:
            bucket_item["memory_percent_total"] += memory_percent
            bucket_item["memory_percent_count"] += 1

        memory_used = _safe_float(memory.get("used"))
        if memory_used is not None:
            bucket_item["memory_used_total"] += memory_used
            bucket_item["memory_used_count"] += 1

        memory_total = _safe_float(memory.get("total"))
        if memory_total is not None:
            bucket_item["memory_total_total"] += memory_total
            bucket_item["memory_total_count"] += 1

        try:
            disk = json.loads(str(row["disk_json"] or "{}"))
        except json.JSONDecodeError:
            disk = {}
        if not isinstance(disk, dict):
            disk = {}

        disk_percent = _safe_float(disk.get("percent"))
        if disk_percent is not None:
            bucket_item["disk_percent_total"] += disk_percent
            bucket_item["disk_percent_count"] += 1

        disk_used = _safe_float(disk.get("used"))
        if disk_used is not None:
            bucket_item["disk_used_total"] += disk_used
            bucket_item["disk_used_count"] += 1

        disk_total = _safe_float(disk.get("total"))
        if disk_total is not None:
            bucket_item["disk_total_total"] += disk_total
            bucket_item["disk_total_count"] += 1

    def _avg(total: float, count: int) -> float | None:
        if count <= 0:
            return None
        return total / count

    items: list[dict[str, Any]] = []
    for bucket_start in sorted(aggregates.keys()):
        value = aggregates[bucket_start]
        items.append(
            {
                "vm_id": value["vm_id"],
                "vm_name": value["vm_name"],
                "timestamp": value["timestamp"],
                "cpu_percent": _avg(value["cpu_total"], value["cpu_count"]),
                "memory": {
                    "percent": _avg(value["memory_percent_total"], value["memory_percent_count"]),
                    "used": _avg(value["memory_used_total"], value["memory_used_count"]),
                    "total": _avg(value["memory_total_total"], value["memory_total_count"]),
                },
                "disk": {
                    "percent": _avg(value["disk_percent_total"], value["disk_percent_count"]),
                    "used": _avg(value["disk_used_total"], value["disk_used_count"]),
                    "total": _avg(value["disk_total_total"], value["disk_total_count"]),
                },
            }
        )
    return items


def get_vm_snapshot_with_history(vm_id: str, hours: int = 24) -> dict[str, Any]:
    history = get_vm_history(vm_id, hours)
    current = history[-1] if history else None
    return {
        "ok": True,
        "vm_id": str(vm_id or ""),
        "current": current,
        "history": history,
        "window_hours": max(1, min(int(hours or 24), _max_window_hours())),
    }


def get_vm_snapshot_with_aggregated_history(vm_id: str, hours: int = 24, bucket: str = "auto") -> dict[str, Any]:
    safe_hours = max(1, min(int(hours or 24), _max_window_hours()))
    bucket_raw = str(bucket or "auto").strip().lower()
    if bucket_raw == "auto":
        if safe_hours <= 24:
            bucket_clean = "minute"
        elif safe_hours <= 168:
            bucket_clean = "hour"
        else:
            bucket_clean = "day"
    elif bucket_raw in {"minute", "hour", "day"}:
        bucket_clean = bucket_raw
    else:
        bucket_clean = "hour"

    history = get_vm_history_aggregated(vm_id=vm_id, hours=safe_hours, bucket=bucket_clean)
    current = _fetch_latest_vm_sample(vm_id)
    return {
        "ok": True,
        "vm_id": str(vm_id or ""),
        "current": current,
        "history": history,
        "window_hours": safe_hours,
        "aggregation": bucket_clean,
    }


def append_sample() -> dict[str, Any]:
    global _APPEND_COUNTER

    with _METRICS_LOCK:
        _ensure_db_ready()
        sample = sample_now()
        with with_connection() as conn:
            conn.execute(
                """
                INSERT INTO central_metrics(timestamp, cpu_percent, memory_json, disk_json, load_avg_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(sample.get("timestamp") or ""),
                    sample.get("cpu_percent"),
                    json.dumps(sample.get("memory") or {}, ensure_ascii=True),
                    json.dumps(sample.get("disk") or {}, ensure_ascii=True),
                    json.dumps(sample.get("load_avg") or {}, ensure_ascii=True),
                ),
            )
            conn.commit()
        _APPEND_COUNTER += 1
        if _APPEND_COUNTER >= _prune_every_samples():
            _prune_history()
            _APPEND_COUNTER = 0
        return sample


def get_history(hours: int = 24) -> list[dict[str, Any]]:
    _ensure_db_ready()
    safe_hours = max(1, min(int(hours or 24), _max_window_hours()))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=safe_hours)
    cutoff_iso = cutoff.isoformat()
    with _METRICS_LOCK:
        with with_connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, cpu_percent, memory_json, disk_json, load_avg_json
                FROM central_metrics
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (cutoff_iso,),
            ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            memory = json.loads(str(row["memory_json"] or "{}"))
        except json.JSONDecodeError:
            memory = {}
        try:
            disk = json.loads(str(row["disk_json"] or "{}"))
        except json.JSONDecodeError:
            disk = {}
        try:
            load_avg = json.loads(str(row["load_avg_json"] or "{}"))
        except json.JSONDecodeError:
            load_avg = {}
        items.append(
            {
                "timestamp": str(row["timestamp"] or ""),
                "cpu_percent": row["cpu_percent"],
                "memory": memory if isinstance(memory, dict) else {},
                "disk": disk if isinstance(disk, dict) else {},
                "load_avg": load_avg if isinstance(load_avg, dict) else {},
            }
        )
    return items


def get_snapshot_with_history(hours: int = 24) -> dict[str, Any]:
    history = get_history(hours)
    current = append_sample()

    if not history or str(history[-1].get("timestamp") or "") != str(current.get("timestamp") or ""):
        history.append(current)

    return {
        "ok": True,
        "current": current,
        "history": history,
        "window_hours": max(1, min(int(hours or 24), _max_window_hours())),
    }


def _sampler_loop(interval_seconds: int) -> None:
    while not _SAMPLER_STOP.is_set():
        try:
            append_sample()
        except Exception:
            # Sampling should never crash the app process.
            logger.exception("central_metrics sampler iteration failed")
        _SAMPLER_STOP.wait(interval_seconds)


def start_sampler(interval_seconds: int = 60) -> None:
    global _SAMPLER_THREAD

    with _METRICS_LOCK:
        if _SAMPLER_THREAD is not None and _SAMPLER_THREAD.is_alive():
            return
        _SAMPLER_STOP.clear()
        _SAMPLER_THREAD = threading.Thread(
            target=_sampler_loop,
            args=(max(15, int(interval_seconds)),),
            daemon=True,
            name="central-metrics-sampler",
        )
        _SAMPLER_THREAD.start()


def stop_sampler() -> None:
    global _SAMPLER_THREAD

    _SAMPLER_STOP.set()
    thread = _SAMPLER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.5)
    _SAMPLER_THREAD = None
