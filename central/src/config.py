import os
from urllib.parse import urlparse
import yaml


_SETTINGS_CACHE: dict | None = None
_SETTINGS_PATH: str | None = None
_SETTINGS_MTIME: float | None = None


def _infer_ip_and_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    ip = parsed.hostname or ""
    port = parsed.port or 8081
    return ip, port


def normalize_agent(agent: dict) -> dict:
    normalized = dict(agent or {})
    base_url = str(normalized.get("base_url") or "").strip()
    parsed_base = urlparse(base_url) if base_url else None

    if not normalized.get("ip") and base_url:
        ip, port = _infer_ip_and_port(base_url)
        if ip:
            normalized["ip"] = ip
        if not normalized.get("port"):
            normalized["port"] = port

    ip_value = str(normalized.get("ip") or "").strip()
    port_value = normalized.get("port", 8081)
    try:
        normalized["port"] = int(port_value)
    except (TypeError, ValueError):
        normalized["port"] = 8081

    if base_url:
        scheme = (parsed_base.scheme or "http").lower() if parsed_base else "http"
        host = ip_value or (parsed_base.hostname if parsed_base else "") or ""
        if host:
            normalized["base_url"] = f"{scheme}://{host}:{normalized['port']}"
        else:
            normalized.pop("base_url", None)
    elif ip_value:
        normalized["base_url"] = f"http://{ip_value}:{normalized['port']}"

    if not normalized.get("name") and normalized.get("id"):
        normalized["name"] = normalized["id"]

    return normalized


def normalize_settings(raw_settings: dict) -> dict:
    settings = dict(raw_settings or {})
    agents = settings.get("agents")
    if isinstance(agents, list):
        settings["agents"] = [normalize_agent(agent) for agent in agents if isinstance(agent, dict)]
    else:
        settings["agents"] = []
    return settings

def get_settings():
    global _SETTINGS_CACHE
    global _SETTINGS_PATH
    global _SETTINGS_MTIME

    path = os.getenv("CENTRAL_CONFIG", "config.yaml")
    try:
        current_mtime = os.path.getmtime(path)
    except OSError:
        current_mtime = None

    if _SETTINGS_CACHE is not None and _SETTINGS_PATH == path and _SETTINGS_MTIME == current_mtime:
        return _SETTINGS_CACHE

    with open(path, "r", encoding="utf-8") as f:
        parsed = normalize_settings(yaml.safe_load(f) or {})

    _SETTINGS_CACHE = parsed
    _SETTINGS_PATH = path
    _SETTINGS_MTIME = current_mtime
    return parsed
