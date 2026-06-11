from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

_AGENT_RESOURCE_CACHE_LOCK = Lock()
_AGENT_RESOURCE_CACHE: dict[str, dict] = {}


def update_agent_resource_cache(item: dict) -> None:
    vm_id = str(item.get("id") or "").strip()
    if not vm_id:
        return
    payload = {
        "online": bool(item.get("online")),
        "resources": item.get("resources") or {},
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    with _AGENT_RESOURCE_CACHE_LOCK:
        _AGENT_RESOURCE_CACHE[vm_id] = payload


def get_cached_agent_resources(vm_id: str) -> dict | None:
    with _AGENT_RESOURCE_CACHE_LOCK:
        cached = _AGENT_RESOURCE_CACHE.get(vm_id)
        return dict(cached) if isinstance(cached, dict) else None