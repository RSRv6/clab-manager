#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class AgentEntry:
    agent_id: str
    name: str
    ip: str
    token: str
    port: int


def _normalize_port(raw: str | int | None, default_port: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default_port


def _sanitize_text(value: str | None) -> str:
    return (value or "").strip()


def _entry_from_line(line: str, default_port: int) -> AgentEntry | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return None

    if "," in stripped:
        parts = [part.strip() for part in stripped.split(",")]
        if len(parts) < 3:
            return None
        agent_id = parts[0]
        name = parts[1]
        ip = parts[2]
        token = parts[3] if len(parts) > 3 else "change-me"
        port = _normalize_port(parts[4] if len(parts) > 4 else None, default_port)
    else:
        ip = stripped
        compact_ip = ip.replace(".", "-")
        agent_id = f"vm-{compact_ip}"
        name = agent_id
        token = "change-me"
        port = default_port

    ip = _sanitize_text(ip)
    if not ip:
        return None

    agent_id = _sanitize_text(agent_id) or f"vm-{ip.replace('.', '-')}"
    name = _sanitize_text(name) or agent_id
    token = _sanitize_text(token) or "change-me"

    return AgentEntry(agent_id=agent_id, name=name, ip=ip, token=token, port=port)


def _extract_ip_port(agent: dict[str, Any]) -> tuple[str, int]:
    ip = _sanitize_text(str(agent.get("ip") or ""))
    port = _normalize_port(agent.get("port"), 8081)

    if ip:
        return ip, port

    base_url = _sanitize_text(str(agent.get("base_url") or ""))
    if not base_url:
        return "", port

    parsed = urlparse(base_url)
    return parsed.hostname or "", parsed.port or 8081


def parse_hosts_file(path: Path, default_port: int) -> list[AgentEntry]:
    entries: list[AgentEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = _entry_from_line(line, default_port)
        if entry:
            entries.append(entry)
    return entries


def merge_agents(central_config: dict[str, Any], new_entries: list[AgentEntry]) -> tuple[dict[str, Any], int]:
    config = dict(central_config or {})
    agents = config.get("agents")
    if not isinstance(agents, list):
        agents = []

    existing_ids = set()
    existing_endpoints = set()
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_id = _sanitize_text(str(agent.get("id") or ""))
        if agent_id:
            existing_ids.add(agent_id)
        ip, port = _extract_ip_port(agent)
        if ip:
            existing_endpoints.add((ip, port))

    added = 0
    for entry in new_entries:
        if entry.agent_id in existing_ids:
            continue
        if (entry.ip, entry.port) in existing_endpoints:
            continue

        agents.append(
            {
                "id": entry.agent_id,
                "name": entry.name,
                "ip": entry.ip,
                "port": entry.port,
                "token": entry.token,
            }
        )
        existing_ids.add(entry.agent_id)
        existing_endpoints.add((entry.ip, entry.port))
        added += 1

    config["agents"] = agents
    return config, added


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync deployment host entries into central/config.yaml agents list")
    parser.add_argument("--hosts-file", required=True)
    parser.add_argument("--central-config", required=True)
    parser.add_argument("--default-port", type=int, default=8081)
    args = parser.parse_args()

    hosts_path = Path(args.hosts_file)
    central_path = Path(args.central_config)

    if not hosts_path.exists():
        raise SystemExit(f"Hosts file not found: {hosts_path}")
    if not central_path.exists():
        raise SystemExit(f"Central config file not found: {central_path}")

    new_entries = parse_hosts_file(hosts_path, default_port=args.default_port)

    with central_path.open("r", encoding="utf-8") as file:
        current_config = yaml.safe_load(file) or {}

    merged_config, added_count = merge_agents(current_config, new_entries)

    with central_path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(merged_config, file, sort_keys=False, allow_unicode=False)

    print(f"Sync complete: added {added_count} new agent(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
