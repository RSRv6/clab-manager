from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Any

import psutil
import yaml

logger = logging.getLogger(__name__)

# Persistent CPU state for accurate delta calculation
_CPU_PREV: tuple[int, int] | None = None


def _read_proc_stat() -> tuple[int, int] | None:
    """Read /proc/stat and return (idle_time, total_time) tuple."""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as file:
            first = file.readline().strip()
    except OSError:
        return None

    parts = first.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None

    try:
        values = [int(value) for value in parts[1:]]
    except ValueError:
        return None

    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def _cpu_percent() -> float | None:
    """Calculate CPU percentage using delta from previous reading."""
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


def get_resources() -> dict[str, Any]:
    vm_memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu = _cpu_percent() or 0.0

    return {
        "cpu_percent": cpu,
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "cpu_count_physical": psutil.cpu_count(logical=False) or 1,
        "memory": {
            "total": vm_memory.total,
            "used": vm_memory.used,
            "percent": vm_memory.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent,
        },
    }


def _inspect_all_labs_raw() -> Any:
    try:
        process = subprocess.run(
            ["containerlab", "inspect", "-a", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.warning("_inspect_all_labs_raw timed out")
        return []

    if process.returncode != 0:
        return []

    try:
        parsed = json.loads(process.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("containerlab inspect output not valid JSON: %s", exc)
        return []

    return parsed


def _normalize_labs(raw: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    if isinstance(raw, dict):
        for fallback_lab_name, nodes in raw.items():
            if not isinstance(nodes, list):
                continue

            routers = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                routers.append(
                    {
                        "name": node.get("name"),
                        "kind": node.get("kind"),
                        "image": node.get("image"),
                        "mgmt_ipv4": node.get("ipv4_address"),
                        "state": node.get("state"),
                        "uptime": _extract_uptime(node.get("status")),
                    }
                )

            first = nodes[0] if nodes and isinstance(nodes[0], dict) else {}
            lab_name = first.get("lab_name") or fallback_lab_name
            topology = first.get("absLabPath") or first.get("labPath")

            normalized.append(
                {
                    "name": lab_name,
                    "topology_file": topology,
                    "routers": routers,
                    "graph": _build_lab_graph(topology),
                }
            )

        return normalized

    if isinstance(raw, list):
        for lab in raw:
            if not isinstance(lab, dict):
                continue

            nodes = lab.get("nodes", {})
            routers = []
            if isinstance(nodes, dict):
                for router_name, router_data in nodes.items():
                    if not isinstance(router_data, dict):
                        continue
                    routers.append(
                        {
                            "name": router_name,
                            "kind": router_data.get("kind"),
                            "image": router_data.get("image"),
                            "mgmt_ipv4": router_data.get("ipv4_address"),
                            "state": router_data.get("state"),
                            "uptime": _extract_uptime(router_data.get("status")),
                        }
                    )

            normalized.append(
                {
                    "name": lab.get("name"),
                    "topology_file": lab.get("topology"),
                    "routers": routers,
                    "graph": _build_lab_graph(lab.get("topology")),
                }
            )

    return normalized


def _clean_endpoint(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.split(":", 1)[0]


def _extract_uptime(status_value: Any) -> str | None:
    if not isinstance(status_value, str) or not status_value:
        return None

    match = re.search(r"\bUp\s+(.+)$", status_value.strip(), flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return status_value.strip()


def _endpoint_interface(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(":", 1)
    if len(parts) < 2:
        return None
    return parts[1]


def _build_lab_graph(topology_file: Any) -> dict[str, Any]:
    if not isinstance(topology_file, str) or not topology_file:
        return {"nodes": [], "links": []}

    candidate_paths = [topology_file]
    if not os.path.isabs(topology_file):
        candidate_paths.append(os.path.join(os.path.expanduser("~"), topology_file))

    selected_path = None
    for path in candidate_paths:
        if os.path.exists(path):
            selected_path = path
            break

    if not selected_path:
        return {"nodes": [], "links": []}

    node_positions = _load_node_positions(selected_path)

    try:
        with open(selected_path, "r", encoding="utf-8") as file:
            topo = yaml.safe_load(file) or {}
    except Exception:
        return {"nodes": [], "links": []}

    topo_nodes = topo.get("topology", {}).get("nodes", {})
    topo_links = topo.get("topology", {}).get("links", [])

    nodes: list[dict[str, Any]] = []
    if isinstance(topo_nodes, dict):
        for node_name, node_data in topo_nodes.items():
            if not isinstance(node_data, dict):
                node_data = {}
            position = node_positions.get(node_name, {})
            nodes.append(
                {
                    "name": node_name,
                    "kind": node_data.get("kind"),
                    "image": node_data.get("image"),
                    "x": position.get("x"),
                    "y": position.get("y"),
                }
            )

    links: list[dict[str, Any]] = []
    if isinstance(topo_links, list):
        for link in topo_links:
            if not isinstance(link, dict):
                continue
            endpoints = link.get("endpoints", [])
            if not isinstance(endpoints, list) or len(endpoints) < 2:
                continue
            source = _clean_endpoint(endpoints[0])
            target = _clean_endpoint(endpoints[1])
            source_if = _endpoint_interface(endpoints[0])
            target_if = _endpoint_interface(endpoints[1])
            if source and target:
                links.append(
                    {
                        "source": source,
                        "target": target,
                        "source_if": source_if,
                        "target_if": target_if,
                    }
                )

    # Some topologies reference external endpoints (e.g. "host") only in links.
    # Ensure these endpoints exist as graph nodes so frontend renderers do not fail.
    known_nodes = {str(node.get("name") or "") for node in nodes if str(node.get("name") or "")}
    extra_endpoints: set[str] = set()
    for link in links:
        source = str(link.get("source") or "").strip()
        target = str(link.get("target") or "").strip()
        if source and source not in known_nodes:
            extra_endpoints.add(source)
        if target and target not in known_nodes:
            extra_endpoints.add(target)

    for endpoint in sorted(extra_endpoints):
        nodes.append(
            {
                "name": endpoint,
                "kind": "external",
                "image": None,
                "x": None,
                "y": None,
            }
        )

    return {"nodes": nodes, "links": links}


def _load_node_positions(topology_path: str) -> dict[str, dict[str, float]]:
    annotations_path = f"{topology_path}.annotations.json"
    if not os.path.exists(annotations_path):
        return {}

    try:
        with open(annotations_path, "r", encoding="utf-8") as file:
            annotations = json.load(file) or {}
    except Exception:
        return {}

    node_annotations = annotations.get("nodeAnnotations", [])
    if not isinstance(node_annotations, list):
        return {}

    positions: dict[str, dict[str, float]] = {}
    for item in node_annotations:
        if not isinstance(item, dict):
            continue
        node_id = item.get("id")
        position = item.get("position", {})
        if not isinstance(node_id, str) or not isinstance(position, dict):
            continue
        x = position.get("x")
        y = position.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            positions[node_id] = {"x": float(x), "y": float(y)}

    return positions


def get_labs() -> list[dict[str, Any]]:
    raw = _inspect_all_labs_raw()
    labs = _normalize_labs(raw)
    result: list[dict[str, Any]] = []

    for lab in labs:
        routers = lab.get("routers", [])
        node_count = len(routers) if isinstance(routers, list) else 0
        result.append(
            {
                "name": lab.get("name"),
                "topology_file": lab.get("topology_file"),
                "node_count": node_count,
                "routers": routers,
                "graph": lab.get("graph", {"nodes": [], "links": []}),
                "status": "running",
            }
        )

    return result


def _topology_roots() -> list[Path]:
    configured = str(os.getenv("AGENT_TOPOLOGY_SEARCH_ROOTS") or "").strip()
    if configured:
        roots = [Path(part).expanduser() for part in configured.split(":") if str(part).strip()]
    else:
        home = Path.home()
        roots = [home / "labs", home / "containerlab", home]

    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if root.exists() and root.is_dir():
            unique_roots.append(root)
    return unique_roots


def _infer_lab_name_from_topology(path: str) -> str:
    file_name = Path(path).name
    lower_name = file_name.lower()
    if lower_name.endswith(".clab.yaml"):
        return file_name[: -len(".clab.yaml")]
    if lower_name.endswith(".clab.yml"):
        return file_name[: -len(".clab.yml")]
    if lower_name.endswith(".yaml"):
        return file_name[: -len(".yaml")]
    if lower_name.endswith(".yml"):
        return file_name[: -len(".yml")]
    return file_name


def _is_topology_file(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(".clab.yaml") or lowered.endswith(".clab.yml") or lowered == "clab.yml"


def get_topology_inventory() -> list[dict[str, Any]]:
    running_labs = get_labs()
    running_by_topology: dict[str, dict[str, Any]] = {}
    for lab in running_labs:
        topology_file = str(lab.get("topology_file") or "").strip()
        if not topology_file:
            continue
        normalized = os.path.realpath(os.path.expanduser(topology_file))
        running_by_topology[normalized] = {
            "lab_name": str(lab.get("name") or _infer_lab_name_from_topology(topology_file)),
            "topology_file": topology_file,
            "running": True,
        }

    discovered: dict[str, dict[str, Any]] = {}
    skipped_dirs = {
        ".git",
        ".cache",
        ".local",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
    }
    max_depth = 6
    max_items = 2000

    for root in _topology_roots():
        root_depth = len(root.parts)
        for current_root, dirs, files in os.walk(root):
            current_depth = len(Path(current_root).parts) - root_depth
            dirs[:] = [entry for entry in dirs if entry not in skipped_dirs and not entry.startswith(".")]
            if current_depth >= max_depth:
                dirs[:] = []

            for file_name in files:
                if not _is_topology_file(file_name):
                    continue
                file_path = os.path.join(current_root, file_name)
                normalized = os.path.realpath(os.path.expanduser(file_path))
                if normalized in discovered:
                    continue
                discovered[normalized] = {
                    "lab_name": _infer_lab_name_from_topology(file_path),
                    "topology_file": file_path,
                    "running": normalized in running_by_topology,
                }
                if len(discovered) >= max_items:
                    break
            if len(discovered) >= max_items:
                break
        if len(discovered) >= max_items:
            break

    for normalized, running in running_by_topology.items():
        if normalized in discovered:
            discovered[normalized]["running"] = True
            discovered[normalized]["lab_name"] = running.get("lab_name") or discovered[normalized].get("lab_name")
            continue
        discovered[normalized] = {
            "lab_name": running.get("lab_name") or _infer_lab_name_from_topology(running.get("topology_file") or normalized),
            "topology_file": running.get("topology_file") or normalized,
            "running": True,
        }

    items = []
    for _, item in sorted(discovered.items(), key=lambda pair: str(pair[1].get("topology_file") or "")):
        topology_file = str(item.get("topology_file") or "")
        items.append(
            {
                "lab_name": str(item.get("lab_name") or _infer_lab_name_from_topology(topology_file)),
                "topology_file": topology_file,
                "running": bool(item.get("running")),
                "file_name": Path(topology_file).name if topology_file else "",
            }
        )
    return items


def get_lab_details(lab_name: str) -> dict[str, Any] | None:
    raw = _inspect_all_labs_raw()
    labs = _normalize_labs(raw)

    for lab in labs:
        if lab.get("name") != lab_name:
            continue

        return {
            "name": lab.get("name"),
            "topology_file": lab.get("topology_file"),
            "routers": lab.get("routers", []),
            "graph": lab.get("graph", {"nodes": [], "links": []}),
        }

    return None


def get_docker_images() -> list[dict]:
    """Return list of Docker images available on this host."""
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        images = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            ref = parts[0] if parts else ""
            if not ref or ref == "<none>:<none>":
                continue
            images.append(
                {
                    "image": ref,
                    "id": parts[1] if len(parts) > 1 else "",
                    "size": parts[2] if len(parts) > 2 else "",
                }
            )
        return images
    except Exception:
        return []