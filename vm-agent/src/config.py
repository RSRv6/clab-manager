from __future__ import annotations

import os
from typing import Any

import yaml

_SETTINGS_CACHE: dict[str, Any] | None = None
_SETTINGS_PATH: str | None = None
_SETTINGS_MTIME: float | None = None


def get_settings() -> dict[str, Any]:
    """Load agent config with mtime-based hot-reload (no restart required)."""
    global _SETTINGS_CACHE, _SETTINGS_PATH, _SETTINGS_MTIME

    config_path = os.getenv("AGENT_CONFIG", "config.yaml")
    try:
        current_mtime = os.path.getmtime(config_path)
    except OSError:
        current_mtime = None

    if (
        _SETTINGS_CACHE is not None
        and _SETTINGS_PATH == config_path
        and _SETTINGS_MTIME == current_mtime
    ):
        return _SETTINGS_CACHE

    with open(config_path, "r", encoding="utf-8") as file:
        parsed = yaml.safe_load(file) or {}

    _SETTINGS_CACHE = parsed
    _SETTINGS_PATH = config_path
    _SETTINGS_MTIME = current_mtime
    return parsed
