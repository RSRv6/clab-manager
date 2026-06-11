"""docker_images_cache.py — Background poller for Docker images on every agent VM.

Polls each agent's /docker-images endpoint every DOCKER_IMAGES_POLL_INTERVAL seconds
(default 300 s / 5 min) in a dedicated daemon thread.  Results are stored per vm_id
and served from cache; no live requests to agents are made when a user opens the
topology builder.
"""
from __future__ import annotations

import asyncio
import copy
import httpx
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ─── Module state ─────────────────────────────────────────────────────────────

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None

# vm_id → { images: list, fetched_at: float, error: str|None }
_CACHE: dict[str, dict[str, Any]] = {}


# ─── Config helpers ────────────────────────────────────────────────────────────

def _poll_interval() -> int:
    raw = os.getenv("CENTRAL_DOCKER_IMAGES_POLL_INTERVAL", "300").strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


# ─── Cache accessors ───────────────────────────────────────────────────────────

def get_images_for_vm(vm_id: str) -> list[dict]:
    """Return cached Docker images for *vm_id* (empty list if not yet fetched)."""
    with _LOCK:
        entry = _CACHE.get(vm_id)
    if entry is None:
        return []
    return copy.deepcopy(entry.get("images", []))


def get_cache_info() -> dict[str, Any]:
    """Return a debug summary of the cache (all VMs, age, counts)."""
    with _LOCK:
        now = time.monotonic()
        return {
            vm_id: {
                "count": len(entry.get("images", [])),
                "age_seconds": round(now - entry["fetched_at"], 1),
                "error": entry.get("error"),
            }
            for vm_id, entry in _CACHE.items()
        }


# ─── Background polling ────────────────────────────────────────────────────────

async def _poll_all_agents(agents: list[dict]) -> None:
    """Fetch docker images from every agent concurrently and store results."""
    from src.services.collector import fetch_agent_docker_images  # local import to avoid circular
    from src.services.collector import _agent_poll_concurrency, _default_limits

    semaphore = asyncio.Semaphore(_agent_poll_concurrency())

    async def _fetch_one(agent: dict, client) -> None:
        vm_id = str(agent.get("id") or "").strip()
        if not vm_id:
            return
        try:
            async with semaphore:
                result = await fetch_agent_docker_images(agent, client=client)
            images = result.get("images", [])
            with _LOCK:
                _CACHE[vm_id] = {
                    "images": images,
                    "fetched_at": time.monotonic(),
                    "error": None if result.get("ok") else result.get("error"),
                }
            logger.debug(
                "docker_images_cache refreshed vm=%s images=%d", vm_id, len(images)
            )
        except Exception as exc:
            logger.warning("docker_images_cache fetch failed vm=%s error=%s", vm_id, exc)
            with _LOCK:
                existing = _CACHE.get(vm_id, {})
                _CACHE[vm_id] = {
                    "images": existing.get("images", []),
                    "fetched_at": existing.get("fetched_at", time.monotonic()),
                    "error": str(exc),
                }

    async with httpx.AsyncClient(timeout=20.0, limits=_default_limits()) as client:
        await asyncio.gather(*(_fetch_one(agent, client) for agent in agents))


def _loop(interval: int) -> None:
    """Background thread: run poll loop every *interval* seconds."""
    from src.config import get_settings  # local import — config only available after app init

    while not _STOP.is_set():
        try:
            agents = get_settings().get("agents") or []
            asyncio.run(_poll_all_agents(agents))
            logger.info("docker_images_cache poll ok vms=%d", len(agents))
        except Exception:
            logger.exception("docker_images_cache unhandled error in poll loop")
        if _STOP.wait(interval):
            break


# ─── Lifecycle ─────────────────────────────────────────────────────────────────

def start_poller(interval_seconds: int | None = None) -> None:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    interval = max(30, int(interval_seconds or _poll_interval()))
    _THREAD = threading.Thread(
        target=_loop,
        args=(interval,),
        daemon=True,
        name="docker-images-cache-poller",
    )
    _THREAD.start()
    logger.info("docker_images_cache poller started interval=%ds", interval)


def stop_poller() -> None:
    _STOP.set()
