from __future__ import annotations

import math
from typing import Any

import yaml


# Conservative baseline profiles (per node kind)
_KIND_PROFILES: dict[str, dict[str, float]] = {
    "cisco_xrd": {"memory_mb": 2200.0, "cpu_units": 130.0},
    "cisco_xrv9k": {"memory_mb": 2800.0, "cpu_units": 150.0},
    "cisco_xrv": {"memory_mb": 2600.0, "cpu_units": 145.0},
    "cisco_iol": {"memory_mb": 384.0, "cpu_units": 35.0},
    "cisco_iosv": {"memory_mb": 1024.0, "cpu_units": 70.0},
    "cisco_csr1000v": {"memory_mb": 3072.0, "cpu_units": 165.0},
    "juniper_vmx": {"memory_mb": 4096.0, "cpu_units": 180.0},
    "juniper_vsrx": {"memory_mb": 2048.0, "cpu_units": 120.0},
    "juniper_vqfx": {"memory_mb": 2048.0, "cpu_units": 130.0},
    "juniper_crpd": {"memory_mb": 1024.0, "cpu_units": 75.0},
    "juniper_vjunos-switch": {"memory_mb": 2200.0, "cpu_units": 140.0},
    "juniper_vjunos-router": {"memory_mb": 2200.0, "cpu_units": 140.0},
    "nokia_sros": {"memory_mb": 4600.0, "cpu_units": 200.0},
    "nokia_srlinux": {"memory_mb": 1400.0, "cpu_units": 90.0},
    "arista_ceos": {"memory_mb": 1500.0, "cpu_units": 95.0},
    "linux": {"memory_mb": 256.0, "cpu_units": 15.0},
    "host": {"memory_mb": 256.0, "cpu_units": 15.0},
    "bridge": {"memory_mb": 64.0, "cpu_units": 3.0},
    "ovs-bridge": {"memory_mb": 96.0, "cpu_units": 5.0},
}
_DEFAULT_PROFILE = {"memory_mb": 1024.0, "cpu_units": 60.0}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_yaml_counts(yaml_text: str) -> tuple[dict[str, int], list[str], str | None]:
    try:
        parsed = yaml.safe_load(str(yaml_text or "")) or {}
    except Exception as exc:
        return {}, [], f"YAML invalide: {exc}"

    if not isinstance(parsed, dict):
        return {}, [], "La racine YAML doit être un objet"

    topology = parsed.get("topology")
    if not isinstance(topology, dict):
        return {}, [], "Champ 'topology' manquant ou invalide"

    nodes = topology.get("nodes")
    if not isinstance(nodes, dict):
        return {}, [], "Champ 'topology.nodes' manquant ou invalide"

    counts: dict[str, int] = {}
    unknown_kinds: list[str] = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        kind = str(node.get("kind") or "").strip().lower()
        if not kind:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        if kind not in _KIND_PROFILES and kind not in unknown_kinds:
            unknown_kinds.append(kind)

    return counts, unknown_kinds, None


def estimate_sandbox_capacity(
    yaml_text: str,
    resources: dict[str, Any],
    guard_band: float = 0.20,
    comparison_mode: str = "physical",
) -> dict[str, Any]:
    counts, unknown_kinds, parse_error = _parse_yaml_counts(yaml_text)
    if parse_error:
        return {
            "ok": False,
            "error": "Impossible d'évaluer la capacité: YAML invalide",
            "details": parse_error,
        }

    memory_total = _to_int((resources.get("memory") or {}).get("total"), 0)
    memory_used = _to_int((resources.get("memory") or {}).get("used"), 0)
    cpu_percent = _to_float(resources.get("cpu_percent"), 0.0)
    cpu_count = _to_int(resources.get("cpu_count"), 1)
    if cpu_count <= 0:
        cpu_count = 1

    margin = min(max(guard_band, 0.0), 0.95)
    allowed_usage_ratio = 1.0 - margin

    max_mem_after_guard = max(0.0, memory_total * allowed_usage_ratio)

    total_cpu_units = float(cpu_count * 100)
    used_cpu_units = (min(max(cpu_percent, 0.0), 100.0) / 100.0) * total_cpu_units
    max_cpu_after_guard = total_cpu_units * allowed_usage_ratio

    mode = str(comparison_mode or "physical").strip().lower()
    if mode == "available":
        mem_budget = max(0.0, max_mem_after_guard - memory_used)
        cpu_budget_units = max(0.0, max_cpu_after_guard - used_cpu_units)
    else:
        mode = "physical"
        # Sandbox edit/redeploy compares against the VM physical envelope (after guard band),
        # because the currently running sandbox topology is torn down before redeploy.
        mem_budget = max_mem_after_guard
        cpu_budget_units = max_cpu_after_guard

    required_mem = 0.0
    required_cpu = 0.0
    per_kind: list[dict[str, Any]] = []
    for kind, count in sorted(counts.items()):
        profile = _KIND_PROFILES.get(kind, _DEFAULT_PROFILE)
        kind_mem = float(count) * float(profile["memory_mb"]) * 1024 * 1024
        kind_cpu = float(count) * float(profile["cpu_units"])
        required_mem += kind_mem
        required_cpu += kind_cpu
        per_kind.append(
            {
                "kind": kind,
                "count": int(count),
                "memory_mb_per_node": float(profile["memory_mb"]),
                "cpu_units_per_node": float(profile["cpu_units"]),
                "memory_mb_total": round(kind_mem / (1024 * 1024), 1),
                "cpu_units_total": round(kind_cpu, 1),
                "profile_defaulted": kind not in _KIND_PROFILES,
            }
        )

    mem_ok = required_mem <= mem_budget
    cpu_ok = required_cpu <= cpu_budget_units
    ok = bool(mem_ok and cpu_ok)

    mem_ratio = (mem_budget / required_mem) if required_mem > 0 else 1.0
    cpu_ratio = (cpu_budget_units / required_cpu) if required_cpu > 0 else 1.0
    fit_ratio = min(1.0, mem_ratio, cpu_ratio)

    suggested_by_kind: list[dict[str, Any]] = []
    if not ok:
        for entry in per_kind:
            current = int(entry["count"])
            suggested = max(0, math.floor(current * fit_ratio))
            suggested_by_kind.append(
                {
                    "kind": entry["kind"],
                    "current": current,
                    "suggested_max": suggested,
                }
            )

    return {
        "ok": ok,
        "guard_band_percent": round(margin * 100, 1),
        "resources": {
            "comparison_mode": mode,
            "cpu_count": cpu_count,
            "cpu_percent_used": round(cpu_percent, 1),
            "cpu_units_budget": round(cpu_budget_units, 1),
            "memory_total_mb": round(memory_total / (1024 * 1024), 1),
            "memory_used_mb": round(memory_used / (1024 * 1024), 1),
            "memory_budget_mb": round(mem_budget / (1024 * 1024), 1),
        },
        "required": {
            "cpu_units": round(required_cpu, 1),
            "memory_mb": round(required_mem / (1024 * 1024), 1),
            "by_kind": per_kind,
        },
        "fits": {
            "cpu": cpu_ok,
            "memory": mem_ok,
        },
        "unknown_kinds": unknown_kinds,
        "recommendation": {
            "fit_ratio": round(fit_ratio, 3),
            "suggested_by_kind": suggested_by_kind,
            "message": (
                "Capacité suffisante"
                if ok
                else "Capacité insuffisante: réduire le nombre de noeuds lourds ou choisir un agent avec plus de ressources"
            ),
        },
    }
