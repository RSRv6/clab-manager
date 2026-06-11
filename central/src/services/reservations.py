from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any
import uuid

from src.config import get_settings

_RESERVATIONS_LOCK = Lock()
_RESERVATIONS_CACHE: dict[str, Any] | None = None
_RESERVATIONS_CACHE_PATH: str | None = None
_RESERVATIONS_CACHE_MTIME: float | None = None

MAX_RESERVATION_HOURS: float = 6.0
MAX_RESERVATION_TOTAL_HOURS: float = 7.0
RESERVATION_EXTENSION_WINDOW_HOURS: float = 1.0
MAX_SIMULTANEOUS_LAB_RESERVATIONS: int = 2


def _default_reservations_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "lab_reservations.json"


def _reservations_path() -> Path:
    configured = os.getenv("CENTRAL_RESERVATIONS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    auth_settings = get_settings().get("auth", {})
    configured_path = str(auth_settings.get("reservations_file") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    return _default_reservations_path()


def _load() -> dict[str, Any]:
    path = _reservations_path()
    path_str = str(path)
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        current_mtime = None

    global _RESERVATIONS_CACHE
    global _RESERVATIONS_CACHE_PATH
    global _RESERVATIONS_CACHE_MTIME

    if (
        _RESERVATIONS_CACHE is not None
        and _RESERVATIONS_CACHE_PATH == path_str
        and _RESERVATIONS_CACHE_MTIME == current_mtime
    ):
        return dict(_RESERVATIONS_CACHE)

    if current_mtime is None:
        _RESERVATIONS_CACHE = {}
        _RESERVATIONS_CACHE_PATH = path_str
        _RESERVATIONS_CACHE_MTIME = None
        return {}

    try:
        text = path.read_text(encoding="utf-8").strip()
        parsed = json.loads(text) if text else {}
        if not isinstance(parsed, dict):
            parsed = {}
        _RESERVATIONS_CACHE = parsed
        _RESERVATIONS_CACHE_PATH = path_str
        _RESERVATIONS_CACHE_MTIME = current_mtime
        return dict(parsed)
    except (json.JSONDecodeError, OSError):
        _RESERVATIONS_CACHE = {}
        _RESERVATIONS_CACHE_PATH = path_str
        _RESERVATIONS_CACHE_MTIME = current_mtime
        return {}


def _save(data: dict[str, Any]) -> None:
    path = _reservations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

    global _RESERVATIONS_CACHE
    global _RESERVATIONS_CACHE_PATH
    global _RESERVATIONS_CACHE_MTIME
    _RESERVATIONS_CACHE = dict(data)
    _RESERVATIONS_CACHE_PATH = str(path)
    try:
        _RESERVATIONS_CACHE_MTIME = path.stat().st_mtime
    except OSError:
        _RESERVATIONS_CACHE_MTIME = None


def _key(vm_id: str, lab_name: str) -> str:
    return f"{vm_id}::{lab_name}"


def _parse_dt(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _entry_start_end(entry: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = _parse_dt(entry.get("starts_at") or entry.get("reserved_at"))
    end = _parse_dt(entry.get("expires_at"))
    return start, end


def _is_active_now(entry: dict[str, Any], now: datetime) -> bool:
    start, end = _entry_start_end(entry)
    if start is None or end is None:
        return False
    return start <= now <= end


def _intervals_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    return start_a < end_b and start_b < end_a


def _entries_for_key(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = data.get(key)
    if raw is None:
        return []
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _ensure_entry_ids(entries: list[dict[str, Any]]) -> bool:
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        reservation_id = str(entry.get("reservation_id") or "").strip()
        if reservation_id:
            continue
        entry["reservation_id"] = str(uuid.uuid4())
        changed = True
    return changed


def _set_entries_for_key(data: dict[str, Any], key: str, entries: list[dict[str, Any]]) -> None:
    if not entries:
        data.pop(key, None)
        return
    data[key] = entries


def _prune_expired_entries(data: dict[str, Any], now: datetime | None = None) -> bool:
    ref = now or datetime.now(timezone.utc)
    changed = False
    keys = list(data.keys())
    for key in keys:
        before = len(_entries_for_key(data, key))
        kept: list[dict[str, Any]] = []
        for entry in _entries_for_key(data, key):
            _, end = _entry_start_end(entry)
            if end is None or end >= ref:
                kept.append(entry)
        _set_entries_for_key(data, key, kept)
        if len(kept) != before:
            changed = True
    return changed


def _count_user_active_reservations_in_data(data: dict[str, Any], username: str) -> int:
    username_lower = str(username or "").lower()
    now = datetime.now(timezone.utc)
    active_labs: set[str] = set()
    for key in list(data.keys()):
        for entry in _entries_for_key(data, key):
            if str(entry.get("owner_username") or "").lower() != username_lower:
                continue
            if _is_active_now(entry, now):
                active_labs.add(key)
                break
    return len(active_labs)


def get_reservation(vm_id: str, lab_name: str) -> dict[str, Any] | None:
    """Return the active reservation for a lab, or None if not reserved / expired."""
    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        changed = _prune_expired_entries(data, now)
        key = _key(vm_id, lab_name)
        entries = _entries_for_key(data, key)
        if _ensure_entry_ids(entries):
            changed = True
        active_entry = None
        active_start = None
        for entry in entries:
            start, end = _entry_start_end(entry)
            if start is None or end is None:
                continue
            if start <= now <= end:
                if active_entry is None or (active_start is not None and start > active_start):
                    active_entry = entry
                    active_start = start
        if changed:
            _save(data)
        return active_entry


def list_scheduled_reservations(vm_id: str, lab_name: str) -> list[dict[str, Any]]:
    """Return future reservations for a lab, sorted by start datetime."""
    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        changed = _prune_expired_entries(data, now)
        key = _key(vm_id, lab_name)
        entries = _entries_for_key(data, key)
        if _ensure_entry_ids(entries):
            changed = True

        scheduled: list[dict[str, Any]] = []
        for entry in entries:
            start, end = _entry_start_end(entry)
            if start is None or end is None:
                continue
            if start > now:
                scheduled.append(entry)

        scheduled.sort(key=lambda entry: _entry_start_end(entry)[0] or datetime.max.replace(tzinfo=timezone.utc))
        if changed:
            _save(data)
        return scheduled


def cancel_scheduled_reservation(vm_id: str, lab_name: str, reservation_id: str) -> dict[str, Any] | None:
    """Cancel a future reservation by id. Returns removed entry or None."""
    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        changed = _prune_expired_entries(data, now)
        key = _key(vm_id, lab_name)
        entries = _entries_for_key(data, key)
        if _ensure_entry_ids(entries):
            changed = True

        removed = None
        kept: list[dict[str, Any]] = []
        for entry in entries:
            rid = str(entry.get("reservation_id") or "")
            start, _ = _entry_start_end(entry)
            is_future = bool(start and start > now)
            if removed is None and rid == reservation_id and is_future:
                removed = entry
                changed = True
                continue
            kept.append(entry)

        if removed is not None:
            _set_entries_for_key(data, key, kept)
            _save(data)
        elif changed:
            _set_entries_for_key(data, key, entries)
            _save(data)

        return removed


def update_scheduled_reservation(
    vm_id: str,
    lab_name: str,
    reservation_id: str,
    *,
    starts_at: str | None = None,
    duration_hours: float | None = None,
) -> dict[str, Any] | None:
    """Update a future reservation by id. Returns updated entry or None if not found."""
    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        _prune_expired_entries(data, now)
        key = _key(vm_id, lab_name)
        entries = _entries_for_key(data, key)
        _ensure_entry_ids(entries)

        target = None
        target_index = -1
        for index, entry in enumerate(entries):
            rid = str(entry.get("reservation_id") or "")
            start, _ = _entry_start_end(entry)
            if rid == reservation_id and start and start > now:
                target = entry
                target_index = index
                break
        if target is None:
            return None

        current_start = _parse_dt(target.get("starts_at") or target.get("reserved_at")) or now
        requested_start = current_start if starts_at is None else _parse_dt(starts_at)
        if requested_start is None:
            raise ValueError("Date de début invalide")
        if requested_start < now:
            raise ValueError("La date de début doit être dans le futur")

        try:
            current_duration = float(target.get("duration_hours") or 0)
        except (TypeError, ValueError):
            current_duration = 0.0
        next_duration = current_duration if duration_hours is None else float(duration_hours)
        if next_duration <= 0 or next_duration > MAX_RESERVATION_HOURS:
            raise ValueError(f"La durée doit être entre 0 et {MAX_RESERVATION_HOURS} heures")

        requested_end = requested_start + timedelta(hours=next_duration)
        owner_username = str(target.get("owner_username") or "")

        # Same-lab overlap protection across users.
        for index, entry in enumerate(entries):
            if index == target_index:
                continue
            entry_start, entry_end = _entry_start_end(entry)
            if entry_start is None or entry_end is None:
                continue
            if not _intervals_overlap(requested_start, requested_end, entry_start, entry_end):
                continue
            holder_username = str(entry.get("owner_username") or "")
            if holder_username.lower() != owner_username.lower():
                holder = entry.get("reserved_by") or "quelqu'un d'autre"
                raise RuntimeError(f"Ce LAB est déjà réservé sur ce créneau par {holder}")

        # Simultaneous reservation rule: max 2 labs at the same time (distinct labs).
        timeline_events: list[tuple[datetime, int]] = []
        for data_key in list(data.keys()):
            for entry in _entries_for_key(data, data_key):
                rid = str(entry.get("reservation_id") or "")
                if data_key == key and rid == reservation_id:
                    continue
                if str(entry.get("owner_username") or "").lower() != owner_username.lower():
                    continue
                entry_start, entry_end = _entry_start_end(entry)
                if entry_start is None or entry_end is None:
                    continue
                overlap_start = max(requested_start, entry_start)
                overlap_end = min(requested_end, entry_end)
                if overlap_start >= overlap_end:
                    continue
                timeline_events.append((overlap_start, 1))
                timeline_events.append((overlap_end, -1))

        current_overlap = 0
        max_overlap = 0
        for _, delta in sorted(timeline_events, key=lambda item: (item[0], item[1])):
            current_overlap += delta
            if current_overlap > max_overlap:
                max_overlap = current_overlap
        if max_overlap + 1 > MAX_SIMULTANEOUS_LAB_RESERVATIONS:
            raise RuntimeError(
                f"Vous ne pouvez pas réserver plus de {MAX_SIMULTANEOUS_LAB_RESERVATIONS} labs simultanément sur ce créneau"
            )

        updated = {
            **target,
            "duration_hours": next_duration,
            "starts_at": requested_start.isoformat(),
            "expires_at": requested_end.isoformat(),
            "updated_at": now.isoformat(),
        }
        entries[target_index] = updated
        entries.sort(key=lambda entry: _entry_start_end(entry)[0] or datetime.min.replace(tzinfo=timezone.utc))
        _set_entries_for_key(data, key, entries)
        _save(data)
        return updated
def count_user_active_reservations(username: str) -> int:
    """Count the number of active reservations for a user."""
    with _RESERVATIONS_LOCK:
        data = _load()
        return _count_user_active_reservations_in_data(data, username)




def reserve_lab(
    vm_id: str,
    lab_name: str,
    reserved_by: str,
    email: str,
    duration_hours: float,
    owner_username: str,
    starts_at: str | None = None,
) -> dict[str, Any]:
    """Create or replace a reservation.  Raises RuntimeError if already held by someone else."""
    if duration_hours <= 0 or duration_hours > MAX_RESERVATION_HOURS:
        raise ValueError(f"La durée doit être entre 0 et {MAX_RESERVATION_HOURS} heures")

    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        _prune_expired_entries(data, now)

        if starts_at:
            start_time = _parse_dt(starts_at)
            if start_time is None:
                raise ValueError("Date de début invalide")
            if start_time < now:
                start_time = now
        else:
            start_time = now
        end_time = start_time + timedelta(hours=duration_hours)

        key = _key(vm_id, lab_name)
        existing_entries = _entries_for_key(data, key)

        # Same-lab overlap protection across users.
        for entry in existing_entries:
            entry_start, entry_end = _entry_start_end(entry)
            if entry_start is None or entry_end is None:
                continue
            if not _intervals_overlap(start_time, end_time, entry_start, entry_end):
                continue
            holder_username = str(entry.get("owner_username") or "")
            if holder_username.lower() != owner_username.lower():
                holder = entry.get("reserved_by") or "quelqu'un d'autre"
                raise RuntimeError(f"Ce LAB est déjà réservé sur ce créneau par {holder}")

        # Simultaneous reservation rule: max 2 labs at the same time (distinct labs).
        timeline_events: list[tuple[datetime, int]] = []
        for data_key in list(data.keys()):
            if data_key == key:
                continue
            for entry in _entries_for_key(data, data_key):
                if str(entry.get("owner_username") or "").lower() != owner_username.lower():
                    continue
                entry_start, entry_end = _entry_start_end(entry)
                if entry_start is None or entry_end is None:
                    continue
                overlap_start = max(start_time, entry_start)
                overlap_end = min(end_time, entry_end)
                if overlap_start >= overlap_end:
                    continue
                timeline_events.append((overlap_start, 1))
                timeline_events.append((overlap_end, -1))

        current_overlap = 0
        max_overlap = 0
        for _, delta in sorted(timeline_events, key=lambda item: (item[0], item[1])):
            current_overlap += delta
            if current_overlap > max_overlap:
                max_overlap = current_overlap

        if max_overlap + 1 > MAX_SIMULTANEOUS_LAB_RESERVATIONS:
            raise RuntimeError(
                f"Vous ne pouvez pas réserver plus de {MAX_SIMULTANEOUS_LAB_RESERVATIONS} labs simultanément sur ce créneau"
            )

        # Replace overlapping reservations from the same owner on the same lab.
        cleaned_entries: list[dict[str, Any]] = []
        for entry in existing_entries:
            entry_start, entry_end = _entry_start_end(entry)
            if entry_start is None or entry_end is None:
                continue
            same_owner = str(entry.get("owner_username") or "").lower() == owner_username.lower()
            if same_owner and _intervals_overlap(start_time, end_time, entry_start, entry_end):
                continue
            cleaned_entries.append(entry)

        reservation: dict[str, Any] = {
            "reservation_id": str(uuid.uuid4()),
            "vm_id": vm_id,
            "lab_name": lab_name,
            "reserved_by": reserved_by,
            "owner_username": owner_username,
            "email": email,
            "duration_hours": duration_hours,
            "reserved_at": now.isoformat(),
            "starts_at": start_time.isoformat(),
            "expires_at": end_time.isoformat(),
        }
        cleaned_entries.append(reservation)
        cleaned_entries.sort(key=lambda entry: _entry_start_end(entry)[0] or datetime.min.replace(tzinfo=timezone.utc))
        _set_entries_for_key(data, key, cleaned_entries)
        _save(data)
        return reservation


def extend_reservation(
    vm_id: str,
    lab_name: str,
    additional_hours: float,
) -> dict[str, Any]:
    """Extend an active reservation.

    Rules:
    - extension is allowed only in the last hour before expiration;
    - total reservation duration cannot exceed MAX_RESERVATION_TOTAL_HOURS.
    """
    if additional_hours <= 0:
        raise ValueError("La durée d'extension doit être supérieure à 0")

    key = _key(vm_id, lab_name)
    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        _prune_expired_entries(data, now)

        existing = None
        existing_index = -1
        entries = _entries_for_key(data, key)
        for index, entry in enumerate(entries):
            if _is_active_now(entry, now):
                existing = entry
                existing_index = index
                break
        if existing is None:
            raise RuntimeError("Aucune réservation active")

        try:
            reserved_at = datetime.fromisoformat(str(existing.get("reserved_at") or ""))
            expires_at = datetime.fromisoformat(str(existing.get("expires_at") or ""))
        except (ValueError, TypeError):
            raise RuntimeError("Réservation invalide")

        if now > expires_at:
            entries.pop(existing_index)
            _set_entries_for_key(data, key, entries)
            _save(data)
            raise RuntimeError("La réservation est expirée")

        extension_window_start = expires_at - timedelta(hours=RESERVATION_EXTENSION_WINDOW_HOURS)
        if now < extension_window_start:
            raise RuntimeError("L'extension est autorisée uniquement pendant la dernière heure")

        stored_duration = existing.get("duration_hours")
        if stored_duration is None:
            current_total_hours = max(0.0, (expires_at - reserved_at).total_seconds() / 3600)
        else:
            try:
                current_total_hours = float(stored_duration)
            except (TypeError, ValueError):
                current_total_hours = max(0.0, (expires_at - reserved_at).total_seconds() / 3600)

        remaining_extension_hours = MAX_RESERVATION_TOTAL_HOURS - current_total_hours
        if remaining_extension_hours <= 0:
            raise RuntimeError(f"Durée maximale atteinte ({MAX_RESERVATION_TOTAL_HOURS}h)")
        if additional_hours > remaining_extension_hours:
            raise ValueError(f"Extension maximale disponible: {remaining_extension_hours:g}h")

        new_total_hours = current_total_hours + additional_hours
        new_expires_at = expires_at + timedelta(hours=additional_hours)

        updated = {
            **existing,
            "duration_hours": round(new_total_hours, 4),
            "expires_at": new_expires_at.isoformat(),
            "extended_at": now.isoformat(),
        }
        entries[existing_index] = updated
        _set_entries_for_key(data, key, entries)
        _save(data)
        return updated


def release_lab(vm_id: str, lab_name: str) -> dict[str, Any] | None:
    """Remove the reservation for a lab.  Returns the removed entry, or None if there was none."""
    with _RESERVATIONS_LOCK:
        data = _load()
        key = _key(vm_id, lab_name)
        now = datetime.now(timezone.utc)
        _prune_expired_entries(data, now)
        entries = _entries_for_key(data, key)
        removed = None
        kept: list[dict[str, Any]] = []
        for entry in entries:
            if removed is None and _is_active_now(entry, now):
                removed = entry
                continue
            kept.append(entry)
        if removed is not None:
            _set_entries_for_key(data, key, kept)
            _save(data)
        return removed


def reservation_belongs_to(reservation: dict[str, Any], username: str) -> bool:
    """True when the reservation's owner_username equals username (case-insensitive)."""
    return str(reservation.get("owner_username") or "").lower() == str(username or "").lower()


def attach_reservations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich a list of VM items (each with a 'labs' list) with live reservation data."""
    with _RESERVATIONS_LOCK:
        data = _load()
        now = datetime.now(timezone.utc)
        changed = _prune_expired_entries(data, now)
        if changed:
            _save(data)

    result = []
    for item in items:
        vm_id = item.get("id") or item.get("vm_id") or ""
        labs = []
        for lab in item.get("labs") or []:
            lab_name = lab.get("name") or ""
            reservation = None
            scheduled_reservations: list[dict[str, Any]] = []
            key = _key(vm_id, lab_name)
            for entry in _entries_for_key(data, key):
                start, _ = _entry_start_end(entry)
                if _is_active_now(entry, now):
                    reservation = entry
                    continue
                if start and start > now:
                    scheduled_reservations.append(entry)
            scheduled_reservations.sort(key=lambda entry: _entry_start_end(entry)[0] or datetime.max.replace(tzinfo=timezone.utc))
            labs.append({**lab, "reservation": reservation, "scheduled_reservations": scheduled_reservations})
        result.append({**item, "labs": labs})
    return result
