"""JWT token cache for clab-api-server (per agent)."""
from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}  # agent_id → (token, expiry_monotonic)
_TTL = 3300.0  # 55 min (JWT tokens expire after 60 min by default, refresh early)


def _base(agent: dict) -> str:
    custom = str(agent.get("clab_api_base_url") or "").strip()
    return custom.rstrip("/") if custom else f"http://{agent.get('ip', '')}:8080"


async def get_token(agent: dict, client: httpx.AsyncClient) -> str:
    """Return a valid JWT token for this agent, fetching a new one if expired."""
    agent_id = str(agent.get("id") or agent.get("name") or "")
    with _lock:
        entry = _cache.get(agent_id)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
    token = await _login(agent, client)
    with _lock:
        _cache[agent_id] = (token, time.monotonic() + _TTL)
    return token


async def _login(agent: dict, client: httpx.AsyncClient) -> str:
    base = _base(agent)
    user = str(agent.get("clab_api_user") or "vlm-central")
    pwd = str(agent.get("clab_api_password") or "")
    resp = await client.post(
        f"{base}/login",
        json={"username": user, "password": pwd},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("JWT") or data.get("access_token") or ""
    if not token:
        raise ValueError(f"clab-api login: no token in response from {base}: {data}")
    logger.info("clab_api_auth: acquired token for agent '%s'", agent.get("name"))
    return token


def invalidate(agent_id: str) -> None:
    """Remove cached token (call after receiving 401 to force re-login)."""
    with _lock:
        _cache.pop(agent_id, None)
