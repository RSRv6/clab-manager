from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Any

from src.config import get_settings
from src.services.audit import record_event
from src.services import central_metrics
from src.services.collector import collect_all

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_SNAPSHOT: list[dict[str, Any]] = []
_LAST_REFRESH_AT: str | None = None
_LAST_REFRESH_MONOTONIC: float | None = None
_LAST_ERROR: str | None = None
_REFRESH_IN_PROGRESS = False
_VM_ONLINE_STATE: dict[str, bool] = {}
_VM_FAILURE_STREAK: dict[str, int] = {}
_VM_SUCCESS_STREAK: dict[str, int] = {}


def _offline_confirmation_failures() -> int:
    raw = os.getenv("CENTRAL_VM_OFFLINE_CONFIRMATION_FAILURES", "4").strip()
    try:
        return max(2, int(raw))
    except ValueError:
        return 4


def _online_confirmation_successes() -> int:
    raw = os.getenv("CENTRAL_VM_ONLINE_CONFIRMATION_SUCCESSES", "2").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _interval_seconds() -> int:
    raw = os.getenv("CENTRAL_STATE_SNAPSHOT_INTERVAL", "3").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _max_age_seconds() -> int:
    raw = os.getenv("CENTRAL_STATE_SNAPSHOT_MAX_AGE", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(6, _interval_seconds() * 2)


def _record_vm_connectivity_transitions(items: list[dict[str, Any]]) -> None:
    transitions: list[tuple[dict[str, Any], bool, bool]] = []
    offline_confirm = _offline_confirmation_failures()
    online_confirm = _online_confirmation_successes()

    with _LOCK:
        for item in items:
            vm_id = str(item.get("id") or "").strip()
            if not vm_id:
                continue
            current_online = bool(item.get("online", False))
            previous_online = _VM_ONLINE_STATE.get(vm_id)

            if current_online:
                _VM_SUCCESS_STREAK[vm_id] = _VM_SUCCESS_STREAK.get(vm_id, 0) + 1
                _VM_FAILURE_STREAK[vm_id] = 0
            else:
                _VM_SUCCESS_STREAK[vm_id] = 0
                _VM_FAILURE_STREAK[vm_id] = _VM_FAILURE_STREAK.get(vm_id, 0) + 1

            if previous_online is None:
                _VM_ONLINE_STATE[vm_id] = current_online
                continue

            if previous_online:
                if (not current_online) and _VM_FAILURE_STREAK[vm_id] >= offline_confirm:
                    _VM_ONLINE_STATE[vm_id] = False
                    transitions.append((item, previous_online, False))
                continue

            if current_online and _VM_SUCCESS_STREAK[vm_id] >= online_confirm:
                _VM_ONLINE_STATE[vm_id] = True
                transitions.append((item, previous_online, True))

    for item, previous_online, current_online in transitions:
        vm_id = str(item.get("id") or "")
        vm_name = str(item.get("name") or vm_id)
        status = "ok" if current_online else "error"
        details = {
            "vm_name": vm_name,
            "previous_online": previous_online,
            "current_online": current_online,
            "error": item.get("error") or "",
        }
        record_event(
            request=None,
            user=None,
            action="vm.connection",
            status=status,
            vm_id=vm_id,
            details=details,
        )


async def refresh_now_async(*, force: bool = True) -> list[dict[str, Any]]:
    global _SNAPSHOT
    global _LAST_REFRESH_AT
    global _LAST_REFRESH_MONOTONIC
    global _LAST_ERROR
    global _REFRESH_IN_PROGRESS

    waited_for_other_refresh = False
    while True:
        with _LOCK:
            if _REFRESH_IN_PROGRESS:
                waited_for_other_refresh = True
                snapshot = copy.deepcopy(_SNAPSHOT)
                if snapshot:
                    return snapshot
            else:
                if waited_for_other_refresh:
                    return copy.deepcopy(_SNAPSHOT)

                if (not force) and _SNAPSHOT:
                    return copy.deepcopy(_SNAPSHOT)

                _REFRESH_IN_PROGRESS = True
                break
        await asyncio.sleep(0.05)

    try:
        agents = get_settings().get("agents", [])
        items = await collect_all(agents)
        try:
            central_metrics.append_vm_samples(items)
        except Exception:
            logger.exception("state_snapshot failed to persist VM metrics samples")
        _record_vm_connectivity_transitions(items)
        snapshot = copy.deepcopy(items)
        refreshed_at = _utc_now()
        with _LOCK:
            _SNAPSHOT = snapshot
            _LAST_REFRESH_AT = refreshed_at
            _LAST_REFRESH_MONOTONIC = time.monotonic()
            _LAST_ERROR = None
        logger.info("state_snapshot refresh ok agents=%s", len(agents))
        return copy.deepcopy(snapshot)
    except Exception as exc:
        with _LOCK:
            _LAST_ERROR = str(exc)
        logger.warning("state_snapshot refresh failed: %s", exc)
        raise
    finally:
        with _LOCK:
            _REFRESH_IN_PROGRESS = False


def refresh_now() -> list[dict[str, Any]]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(refresh_now_async(force=True))
    raise RuntimeError("refresh_now cannot be called from async context; use refresh_now_async")


def get_snapshot_copy() -> list[dict[str, Any]]:
    with _LOCK:
        return copy.deepcopy(_SNAPSHOT)


def get_snapshot_status() -> dict[str, Any]:
    with _LOCK:
        age_seconds = None
        if _LAST_REFRESH_MONOTONIC is not None:
            age_seconds = max(0.0, time.monotonic() - _LAST_REFRESH_MONOTONIC)
        return {
            "items_count": len(_SNAPSHOT),
            "last_refresh_at": _LAST_REFRESH_AT,
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "last_error": _LAST_ERROR,
            "refresh_in_progress": _REFRESH_IN_PROGRESS,
        }


async def get_or_refresh_snapshot_async() -> list[dict[str, Any]]:
    with _LOCK:
        has_snapshot = bool(_SNAPSHOT)
        age_seconds = None if _LAST_REFRESH_MONOTONIC is None else max(0.0, time.monotonic() - _LAST_REFRESH_MONOTONIC)
        refresh_in_progress = _REFRESH_IN_PROGRESS

    if not has_snapshot:
        return await refresh_now_async(force=True)

    if refresh_in_progress:
        return get_snapshot_copy()

    if age_seconds is not None and age_seconds > _max_age_seconds():
        try:
            return await refresh_now_async(force=True)
        except Exception:
            logger.warning("state_snapshot returning stale snapshot after refresh failure")
            return get_snapshot_copy()

    return get_snapshot_copy()


def _loop(interval_seconds: int) -> None:
    while not _STOP.is_set():
        try:
            refresh_now()
        except Exception:
            logger.exception("Unhandled error in background snapshot loop")
        if _STOP.wait(interval_seconds):
            break


def start_reconciler(interval_seconds: int | None = None) -> None:
    global _THREAD

    if _THREAD is not None and _THREAD.is_alive():
        return

    _STOP.clear()
    interval = max(1, int(interval_seconds or _interval_seconds()))
    _THREAD = threading.Thread(
        target=_loop,
        args=(interval,),
        daemon=True,
        name="state-snapshot-reconciler",
    )
    _THREAD.start()


def stop_reconciler() -> None:
    global _THREAD

    _STOP.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.5)
    _THREAD = None
