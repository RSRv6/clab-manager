from __future__ import annotations

import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable

import paramiko
import yaml

from src.audit.collector import get_lab_details, get_topology_inventory
from src.management.controller import start_lab, stop_lab, stop_lab_by_topology

logger = logging.getLogger(__name__)


# Maps ContainerLab node kind → internal family name used for SSH dispatch.
# Adding a new router here is enough to enable reconfigure/export/set-default
# for that kind — provided the matching _apply_* / _capture_* logic handles
# the family string below.
SUPPORTED_FAMILIES = {
    # Cisco
    "cisco_xrd": "iosxr",
    "cisco_xrv9k": "iosxr",
    "cisco_xrv": "iosxr",
    "cisco_iol": "ios",
    "cisco_iosv": "ios",
    "cisco_csr1000v": "ios",
    # Juniper
    "juniper_vmx": "junos",
    "juniper_vsrx": "junos",
    "juniper_vqfx": "junos",
    "juniper_crpd": "crpd",
    "juniper_vjunos-switch": "vjunos",
    "juniper_vjunos-router": "vjunos",
    # Nokia
    "nokia_sros": "sros",
    "nokia_srlinux": "srl",
    # Arista
    "arista_ceos": "ceos",
}

_IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/:-]*$")

# Nokia SR Linux interfaces: ethernet-X/Y with X and Y both >= 1
_SRL_ETHERNET_RE = re.compile(r"^ethernet-(\d+)/(\d+)$")
_SRL_KINDS = frozenset({"nokia_srlinux", "srlinux", "sr-linux", "srl"})
# Nokia SR OS port-channel style: 1/1/1
_SROS_PORT_RE = re.compile(r"^\d+(/\d+)+$")
_SROS_KINDS = frozenset({"nokia_sros", "sros", "vr-sros"})
_IOS_EXEC_PROMPT_PATTERN = r"(?m)^\r*[^\r\n]*[>#]\s*$"
_IOS_CONFIG_PROMPT_PATTERN = r"(?m)^\r*[^\r\n]*\([^)\r\n]*\)#\s*$"
_IOSXR_CONFIG_PROMPT_PATTERN = r"(?m)^\r*[^\r\n]*\([^)\r\n]*\)#\s*$"
_IOSXR_EXEC_PROMPT_PATTERN = r"(?m)^\r*[^\r\n]*[#>]\s*$"
_IOSXR_ANY_PROMPT_PATTERN = r"(?m)^\r*(?:[^\r\n]*\([^)\r\n]*\)#|[^\r\n]*[#>])\s*$"
_IOSXR_COMMIT_ERROR_RE = re.compile(
    r"(?im)(failed to commit|commit failed|pseudo-atomic operation|"
    r"one or more configuration items|syntax error|invalid input)"
)
_IOSXR_CAPTURE_TIMESTAMP_RE = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+UTC$")
_IOSXR_CAPTURE_PROMPT_RE = re.compile(r"^(?:RP/\d+/RP\d+/CPU\d+:[^\s#>]+(?:\([^)]+\))?[#>].*|\d+/RP\d+/CPU\d+\s+.*IOS XR RUN.*)$")
_IOSXR_APPLY_ERROR_RE = re.compile(r"(?im)(% Invalid input|% Failed|syntax error|error:)")
_IOSXR_REPLACE_CONFIRM_PATTERN = r"Do you wish to proceed\? \[no\]:"
_IOS_APPLY_ERROR_RE = re.compile(
    r"(?im)(^%\s*(?:Invalid input|Incomplete command|Ambiguous command|Error)|Invalid input detected at '\^' marker\.)"
)
_IOS_CAPTURE_PROMPT_RE = re.compile(r"^[A-Za-z0-9_.:/-]+(?:\([^)\r\n]+\))?[#>]\s*$")
_JUNOS_COMMIT_ERROR_RE = re.compile(r"(?im)(\berror:\b|commit failed|configuration check-out failed|syntax error)")
_JUNOS_DUP_LOCAL_ADDR_RE = re.compile(
    r"(?im)(Cannot have the same local address on different units of an interface)"
)
_JUNOS_DB_LOCK_RE = re.compile(r"(?im)configuration\s+database\s+locked\s+by")
_JUNOS_LICENSE_BLOCK_RE = re.compile(
    r"(?im)(license|licence|entitlement|subscription|triton mode|only loopback interface is supported under vrf routing instances)"
)
_JUNOS_LOCK_PID_RE = re.compile(r"(?im)\(pid\s+(\d+)\)")
_JUNOS_LOCK_TERM_RE = re.compile(r"(?im)terminal\s+(\S+)\s+\(pid")
_JUNOS_NODE_FROM_PROMPT_RE = re.compile(r"(?im)@([A-Za-z0-9._-]+)[#>%]")

# ---------------------------------------------------------------------------
# Async reconfigure job store
# ---------------------------------------------------------------------------
_RECONFIG_JOBS: dict[str, dict] = {}
_RECONFIG_JOBS_LOCK = threading.Lock()
_RECONFIG_JOBS_MAX = 30
_SET_DEFAULT_JOBS: dict[str, dict] = {}
_SET_DEFAULT_JOBS_LOCK = threading.Lock()
_SET_DEFAULT_JOBS_MAX = 30
_CONFIG_DIFF_JOBS: dict[str, dict] = {}
_CONFIG_DIFF_JOBS_LOCK = threading.Lock()
_CONFIG_DIFF_JOBS_MAX = 50
_RECONFIG_CANCEL_EVENTS: dict[str, threading.Event] = {}
_RECONFIG_CANCEL_EVENTS_LOCK = threading.Lock()
_SET_DEFAULT_CANCEL_EVENTS: dict[str, threading.Event] = {}
_SET_DEFAULT_CANCEL_EVENTS_LOCK = threading.Lock()
_CONFIG_DIFF_CANCEL_EVENTS: dict[str, threading.Event] = {}
_CONFIG_DIFF_CANCEL_EVENTS_LOCK = threading.Lock()
_SANDBOX_LAB_LOCKS: dict[str, threading.Lock] = {}
_SANDBOX_LAB_LOCKS_GUARD = threading.Lock()
_SANDBOX_BACKUP_KEEP_PER_TAG = max(1, int(os.getenv("SANDBOX_BACKUP_KEEP_PER_TAG", "20")))


def _resolve_recovery_script_path(kind: str) -> str | None:
    env_var = "VLM_JUNOS_UNLOCK_SCRIPT" if kind == "unlock" else "VLM_JUNOS_LICENSE_SCRIPT"
    configured = str(os.getenv(env_var, "")).strip()
    if configured:
        expanded = os.path.expanduser(configured)
        if os.path.isfile(expanded):
            return expanded
        logger.warning("junos_recovery missing_script kind=%s env=%s path=%s", kind, env_var, expanded)

    scripts_dir = os.path.expanduser("~/scripts")
    if not os.path.isdir(scripts_dir):
        return None

    pattern = re.compile(r"(?i)(unlock|release|lock|pid|terminat|blocking)") if kind == "unlock" else re.compile(r"(?i)(licen[cs]e|entitlement|subscription)")
    try:
        candidates = sorted(os.listdir(scripts_dir))
    except OSError:
        return None

    for name in candidates:
        if not pattern.search(name):
            continue
        full_path = os.path.join(scripts_dir, name)
        if os.path.isfile(full_path):
            return full_path
    return None


def _extract_junos_recovery_context(error_text: str) -> tuple[str | None, str]:
    node_match = _JUNOS_NODE_FROM_PROMPT_RE.search(error_text or "")
    node_name = node_match.group(1) if node_match else None
    pids = ",".join(_JUNOS_LOCK_PID_RE.findall(error_text or ""))
    return node_name, pids


def _normalize_junos_lab_arg(lab_name: str | None) -> str:
    value = str(lab_name or "").strip()
    if value.lower().endswith("-lab"):
        return value[:-4]
    return value


def _direct_junos_unlock(
    *,
    lab_arg: str,
    node_name: str,
    username: str,
    password: str,
    pid: str | None,
    terminal_id: str | None,
) -> bool:
    host = f"clab-{lab_arg}-lab-{node_name}"
    ssh = None
    chan = None
    try:
        ssh = _ssh_connect_with_retry(host, username, password, family="junos")
        chan = ssh.invoke_shell()
        _wait_for_prompt(chan, r"[>%] ?$", 20)

        if pid:
            _send_line(chan, f"request system process terminate {pid}")
            _wait_for_prompt_quiet(chan, r"[>%] ?$", 15)

        if terminal_id:
            _send_line(chan, f"request system logout terminal {terminal_id}")
            _wait_for_prompt_quiet(chan, r"[>%] ?$", 15)

        logger.warning("junos_recovery direct_unlock attempted host=%s pid=%s terminal=%s", host, pid or "", terminal_id or "")
        return True
    except Exception as exc:
        logger.warning("junos_recovery direct_unlock failed host=%s error=%s", host, exc)
        return False
    finally:
        try:
            if chan is not None:
                chan.close()
        except Exception:
            pass
        try:
            if ssh is not None:
                ssh.close()
        except Exception:
            pass


def _run_junos_recovery(
    kind: str,
    error_text: str,
    lab_name: str | None = None,
    node_name_hint: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> bool:
    script_path = _resolve_recovery_script_path(kind)
    if not script_path:
        logger.warning("junos_recovery no_script kind=%s", kind)
        return False

    node_name, pid_list = _extract_junos_recovery_context(error_text)
    if not node_name and node_name_hint:
        node_name = str(node_name_hint)

    pid_match = _JUNOS_LOCK_PID_RE.search(error_text or "")
    pid_value = pid_match.group(1) if pid_match else None
    term_match = _JUNOS_LOCK_TERM_RE.search(error_text or "")
    terminal_value = term_match.group(1) if term_match else None

    lab_arg = _normalize_junos_lab_arg(lab_name)
    if not lab_arg:
        logger.error("junos_recovery missing_lab_context kind=%s", kind)
        return False

    env = os.environ.copy()
    env["VLM_RECOVERY_KIND"] = kind
    env["VLM_RECOVERY_LAB"] = lab_arg
    env["VLM_RECOVERY_NODE"] = node_name or ""
    env["VLM_RECOVERY_PIDS"] = pid_list
    env["VLM_RECOVERY_USERNAME"] = str(username or "")
    env["VLM_RECOVERY_PASSWORD"] = str(password or "")

    base_command = [script_path] if os.access(script_path, os.X_OK) else ["bash", script_path]
    command = [*base_command, "--lab", lab_arg]
    if node_name:
        command.extend(["--nodes", node_name])
    if username:
        command.extend(["--username", str(username)])
    if password:
        command.extend(["--password", str(password)])

    logger.warning("junos_recovery start kind=%s script=%s lab=%s node=%s pids=%s", kind, script_path, lab_arg, node_name or "", pid_list)

    def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

    try:
        completed = _run(command)
        if completed.returncode != 0 and node_name and "unrecognized arguments" in (completed.stderr or "") and "--nodes" in (completed.stderr or ""):
            command = [*base_command, "--lab", lab_arg]
            completed = _run(command)

        # Always run an inline unlock attempt for lock errors so we handle
        # terminal ids like pts/2 even when external script reports success.
        if kind == "unlock" and node_name and username and password and (pid_value or terminal_value):
            _direct_junos_unlock(
                lab_arg=lab_arg,
                node_name=node_name,
                username=username,
                password=password,
                pid=pid_value,
                terminal_id=terminal_value,
            )

        if completed.returncode != 0:
            logger.error(
                "junos_recovery failed kind=%s rc=%s stderr=%s",
                kind,
                completed.returncode,
                (completed.stderr or "")[-400:],
            )
            return False

        logger.warning("junos_recovery success kind=%s stdout=%s", kind, (completed.stdout or "")[-200:])
        return True
    except Exception as exc:
        logger.error("junos_recovery exception kind=%s error=%s", kind, exc)
        return False


def _try_junos_recovery(
    error_text: str,
    lab_name: str | None = None,
    node_name_hint: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> bool:
    if _JUNOS_DB_LOCK_RE.search(error_text or ""):
        return _run_junos_recovery(
            "unlock",
            error_text,
            lab_name=lab_name,
            node_name_hint=node_name_hint,
            username=username,
            password=password,
        )
    if _JUNOS_LICENSE_BLOCK_RE.search(error_text or ""):
        return _run_junos_recovery(
            "license",
            error_text,
            lab_name=lab_name,
            node_name_hint=node_name_hint,
            username=username,
            password=password,
        )
    return False


def _sandbox_lock(lab_name: str) -> threading.Lock:
    with _SANDBOX_LAB_LOCKS_GUARD:
        lock = _SANDBOX_LAB_LOCKS.get(lab_name)
        if lock is None:
            lock = threading.Lock()
            _SANDBOX_LAB_LOCKS[lab_name] = lock
        return lock


def _sandbox_baseline_path(topology_path: Path) -> Path:
    return topology_path.with_suffix(topology_path.suffix + ".sandbox-admin-baseline")


def _sandbox_backup_path(topology_path: Path, tag: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return topology_path.with_suffix(topology_path.suffix + f".{tag}-{timestamp}")


def _prune_sandbox_backups(topology_path: Path, tag: str, keep: int | None = None) -> dict[str, Any]:
    limit = max(1, int(keep if keep is not None else _SANDBOX_BACKUP_KEEP_PER_TAG))
    pattern = f"{topology_path.name}.{tag}-*"
    try:
        candidates = [path for path in topology_path.parent.glob(pattern) if path.is_file()]
    except Exception as exc:
        return {"ok": False, "error": str(exc), "removed": [], "kept": 0, "total": 0}

    if len(candidates) <= limit:
        return {"ok": True, "removed": [], "kept": len(candidates), "total": len(candidates), "limit": limit}

    try:
        ordered = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        ordered = sorted(candidates, reverse=True)

    removed: list[str] = []
    failures: list[str] = []
    for stale in ordered[limit:]:
        try:
            stale.unlink(missing_ok=True)
            removed.append(str(stale))
        except Exception as exc:
            failures.append(f"{stale}: {exc}")

    return {
        "ok": len(failures) == 0,
        "removed": removed,
        "kept": min(limit, len(ordered)),
        "total": len(ordered),
        "limit": limit,
        "errors": failures,
    }


def _extract_mgmt_signature(parsed: Any) -> tuple[str, str]:
    if not isinstance(parsed, dict):
        return "", ""
    mgmt = parsed.get("mgmt")
    if not isinstance(mgmt, dict):
        return "", ""
    network_name = str(mgmt.get("network") or "").strip()
    ipv4_subnet = str(mgmt.get("ipv4-subnet") or mgmt.get("ipv4_subnet") or "").strip()
    return network_name, ipv4_subnet


def _load_sandbox_reference_yaml(lab_name: str) -> tuple[str, str]:
    topology_path = _resolve_topology_path(lab_name)
    baseline_path = _sandbox_baseline_path(topology_path)
    if baseline_path.exists() and baseline_path.is_file():
        return baseline_path.read_text(encoding="utf-8"), "admin-baseline"
    return topology_path.read_text(encoding="utf-8"), "current-topology"


def _ensure_sandbox_baseline(topology_path: Path) -> Path:
    baseline_path = _sandbox_baseline_path(topology_path)
    if baseline_path.exists() and baseline_path.is_file():
        return baseline_path
    baseline_path.write_text(topology_path.read_text(encoding="utf-8"), encoding="utf-8")
    return baseline_path


def _annotations_path_for_topology(topology_path: Path) -> Path:
    return Path(f"{topology_path}.annotations.json")


def _normalize_node_positions(
    node_positions: dict[str, dict[str, float]] | None,
) -> dict[str, dict[str, float]]:
    normalized: dict[str, dict[str, float]] = {}
    if not isinstance(node_positions, dict):
        return normalized

    for raw_name, raw_pos in node_positions.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_pos, dict):
            continue

        x_raw = raw_pos.get("x")
        y_raw = raw_pos.get("y")
        try:
            x = float(x_raw)
            y = float(y_raw)
        except (TypeError, ValueError):
            continue

        if not (math.isfinite(x) and math.isfinite(y)):
            continue

        normalized[name] = {"x": x, "y": y}

    return normalized


def _snapshot_annotations(topology_path: Path) -> tuple[Path, bool, str | None]:
    annotations_path = _annotations_path_for_topology(topology_path)
    existed = annotations_path.exists() and annotations_path.is_file()
    original_text = None
    if existed:
        try:
            original_text = annotations_path.read_text(encoding="utf-8")
        except Exception:
            original_text = None
    return annotations_path, existed, original_text


def _restore_annotations_snapshot(
    annotations_path: Path,
    existed: bool,
    original_text: str | None,
) -> dict[str, Any]:
    try:
        if existed:
            annotations_path.write_text(
                original_text if original_text is not None else "{}",
                encoding="utf-8",
            )
            return {"ok": True, "restored": True, "removed": False, "annotations_file": str(annotations_path)}

        annotations_path.unlink(missing_ok=True)
        return {"ok": True, "restored": True, "removed": True, "annotations_file": str(annotations_path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "annotations_file": str(annotations_path)}


def _write_node_positions_annotations(
    topology_path: Path,
    node_positions: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    normalized = _normalize_node_positions(node_positions)
    annotations_path = _annotations_path_for_topology(topology_path)
    if not normalized:
        return {
            "ok": True,
            "written": False,
            "positions_count": 0,
            "reason": "no_valid_positions",
            "annotations_file": str(annotations_path),
        }

    payload: dict[str, Any] = {}
    if annotations_path.exists() and annotations_path.is_file():
        try:
            existing = json.loads(annotations_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except Exception:
            payload = {}

    payload["nodeAnnotations"] = [
        {
            "id": node_name,
            "position": {"x": coords["x"], "y": coords["y"]},
        }
        for node_name, coords in sorted(normalized.items())
    ]

    tmp_path = Path(f"{annotations_path}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(annotations_path)

    return {
        "ok": True,
        "written": True,
        "positions_count": len(normalized),
        "annotations_file": str(annotations_path),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_reconfigure_job(lab_name: str, router_names: list[str] | None = None, mode: str | None = None) -> dict[str, Any]:
    """Start a background reconfigure job and return its job_id immediately.
    
    Args:
        lab_name: Name of the lab to reconfigure
        router_names: Optional list of router short names to reconfigure. If None/empty, reconfigures all.
        mode: Reconfigure mode. "base" merges baseline config, "cleanup" removes extra config before apply.
    """
    normalized_mode = str(mode or "base").strip().lower() or "base"
    job_id = str(uuid.uuid4())
    job: dict[str, Any] = {
        "job_id": job_id,
        "lab": lab_name,
        "status": "running",
        "results": [],
        "total": 0,
        "completed_count": 0,
        "success_count": 0,
        "started_at": _now_iso(),
        "finished_at": None,
        "error": None,
        "requested_routers": router_names or [],
        "mode": normalized_mode,
        "cancel_requested": False,
        "cancel_requested_at": None,
    }
    with _RECONFIG_JOBS_LOCK:
        _RECONFIG_JOBS[job_id] = job
        if len(_RECONFIG_JOBS) > _RECONFIG_JOBS_MAX:
            oldest_key = next(iter(_RECONFIG_JOBS))
            del _RECONFIG_JOBS[oldest_key]
            with _RECONFIG_CANCEL_EVENTS_LOCK:
                _RECONFIG_CANCEL_EVENTS.pop(oldest_key, None)
    with _RECONFIG_CANCEL_EVENTS_LOCK:
        _RECONFIG_CANCEL_EVENTS[job_id] = threading.Event()
    threading.Thread(target=_run_reconfigure_job, args=(job_id, lab_name, router_names, normalized_mode), daemon=True).start()
    logger.info(
        "start_reconfigure_job lab=%s job=%s mode=%s router_names=%s",
        lab_name,
        job_id,
        normalized_mode,
        router_names or "all",
    )
    return {"ok": True, "job_id": job_id}


def get_reconfigure_job(job_id: str) -> dict[str, Any] | None:
    return _RECONFIG_JOBS.get(job_id)


def cancel_reconfigure_job(job_id: str) -> dict[str, Any]:
    with _RECONFIG_JOBS_LOCK:
        job = _RECONFIG_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Job not found"}

        status_value = str(job.get("status") or "").strip().lower()
        if status_value in {"completed", "error", "failed", "cancelled", "canceled"}:
            return {
                "ok": True,
                "already_terminal": True,
                "job_id": job_id,
                "status": job.get("status"),
            }

        if status_value != "cancelling":
            job["status"] = "cancelling"
        job["cancel_requested"] = True
        job["cancel_requested_at"] = _now_iso()

    with _RECONFIG_CANCEL_EVENTS_LOCK:
        cancel_event = _RECONFIG_CANCEL_EVENTS.get(job_id)
        if cancel_event is None:
            cancel_event = threading.Event()
            _RECONFIG_CANCEL_EVENTS[job_id] = cancel_event
        cancel_event.set()

    return {"ok": True, "job_id": job_id, "status": "cancelling"}


def start_set_default_job(lab_name: str) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    job: dict[str, Any] = {
        "job_id": job_id,
        "lab": lab_name,
        "status": "running",
        "results": [],
        "total": 0,
        "completed_count": 0,
        "success_count": 0,
        "saved_count": 0,
        "backup_file": None,
        "db_path": None,
        "skipped_count": 0,
        "skipped_nodes": [],
        "started_at": _now_iso(),
        "finished_at": None,
        "error": None,
        "ok": False,
        "cancel_requested": False,
        "cancel_requested_at": None,
    }
    with _SET_DEFAULT_JOBS_LOCK:
        _SET_DEFAULT_JOBS[job_id] = job
        if len(_SET_DEFAULT_JOBS) > _SET_DEFAULT_JOBS_MAX:
            oldest_key = next(iter(_SET_DEFAULT_JOBS))
            del _SET_DEFAULT_JOBS[oldest_key]
            with _SET_DEFAULT_CANCEL_EVENTS_LOCK:
                _SET_DEFAULT_CANCEL_EVENTS.pop(oldest_key, None)
    with _SET_DEFAULT_CANCEL_EVENTS_LOCK:
        _SET_DEFAULT_CANCEL_EVENTS[job_id] = threading.Event()
    threading.Thread(target=_run_set_default_job, args=(job_id, lab_name), daemon=True).start()
    logger.info("start_set_default_job lab=%s job=%s", lab_name, job_id)
    return {"ok": True, "job_id": job_id}


def get_set_default_job(job_id: str) -> dict[str, Any] | None:
    return _SET_DEFAULT_JOBS.get(job_id)


def cancel_set_default_job(job_id: str) -> dict[str, Any]:
    with _SET_DEFAULT_JOBS_LOCK:
        job = _SET_DEFAULT_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Job not found"}

        status_value = str(job.get("status") or "").strip().lower()
        if status_value in {"completed", "error", "failed", "cancelled", "canceled"}:
            return {
                "ok": True,
                "already_terminal": True,
                "job_id": job_id,
                "status": job.get("status"),
            }

        if status_value != "cancelling":
            job["status"] = "cancelling"
        job["cancel_requested"] = True
        job["cancel_requested_at"] = _now_iso()

    with _SET_DEFAULT_CANCEL_EVENTS_LOCK:
        cancel_event = _SET_DEFAULT_CANCEL_EVENTS.get(job_id)
        if cancel_event is None:
            cancel_event = threading.Event()
            _SET_DEFAULT_CANCEL_EVENTS[job_id] = cancel_event
        cancel_event.set()

    return {"ok": True, "job_id": job_id, "status": "cancelling"}


def start_config_diff_job(lab_name: str, router_names: list[str] | None = None) -> dict[str, Any]:
    """Start a background config-diff job and return its job_id immediately."""
    job_id = str(uuid.uuid4())
    requested = list(router_names or [])
    job: dict[str, Any] = {
        "job_id": job_id,
        "lab": lab_name,
        "status": "running",
        "started_at": _now_iso(),
        "finished_at": None,
        "error": None,
        "requested_routers": requested,
        "requested_count": len(requested),
        "total": 0,
        "completed_count": 0,
        "success_count": 0,
        "result": None,
        "cancel_requested": False,
        "cancel_requested_at": None,
    }
    with _CONFIG_DIFF_JOBS_LOCK:
        _CONFIG_DIFF_JOBS[job_id] = job
        if len(_CONFIG_DIFF_JOBS) > _CONFIG_DIFF_JOBS_MAX:
            oldest_key = next(iter(_CONFIG_DIFF_JOBS))
            del _CONFIG_DIFF_JOBS[oldest_key]
            with _CONFIG_DIFF_CANCEL_EVENTS_LOCK:
                _CONFIG_DIFF_CANCEL_EVENTS.pop(oldest_key, None)
    with _CONFIG_DIFF_CANCEL_EVENTS_LOCK:
        _CONFIG_DIFF_CANCEL_EVENTS[job_id] = threading.Event()
    threading.Thread(target=_run_config_diff_job, args=(job_id, lab_name, router_names), daemon=True).start()
    logger.info("start_config_diff_job lab=%s job=%s router_names=%s", lab_name, job_id, router_names or "all")
    return {"ok": True, "job_id": job_id}


def get_config_diff_job(job_id: str) -> dict[str, Any] | None:
    return _CONFIG_DIFF_JOBS.get(job_id)


def cancel_config_diff_job(job_id: str) -> dict[str, Any]:
    with _CONFIG_DIFF_JOBS_LOCK:
        job = _CONFIG_DIFF_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Job not found"}

        status_value = str(job.get("status") or "").strip().lower()
        if status_value in {"completed", "error", "failed", "cancelled", "canceled"}:
            return {
                "ok": True,
                "already_terminal": True,
                "job_id": job_id,
                "status": job.get("status"),
            }

        if status_value != "cancelling":
            job["status"] = "cancelling"
        job["cancel_requested"] = True
        job["cancel_requested_at"] = _now_iso()

    with _CONFIG_DIFF_CANCEL_EVENTS_LOCK:
        cancel_event = _CONFIG_DIFF_CANCEL_EVENTS.get(job_id)
        if cancel_event is None:
            cancel_event = threading.Event()
            _CONFIG_DIFF_CANCEL_EVENTS[job_id] = cancel_event
        cancel_event.set()

    return {"ok": True, "job_id": job_id, "status": "cancelling"}


def _run_config_diff_job(job_id: str, lab_name: str, router_names: list[str] | None = None) -> None:
    job = _CONFIG_DIFF_JOBS.get(job_id)
    if not job:
        return

    with _CONFIG_DIFF_CANCEL_EVENTS_LOCK:
        cancel_event = _CONFIG_DIFF_CANCEL_EVENTS.get(job_id)

    def _on_progress(progress: dict[str, Any]) -> None:
        with _CONFIG_DIFF_JOBS_LOCK:
            current_job = _CONFIG_DIFF_JOBS.get(job_id)
            if not current_job:
                return
            current_job["total"] = int(progress.get("total") or 0)
            current_job["completed_count"] = int(progress.get("completed_count") or 0)
            current_job["success_count"] = int(progress.get("success_count") or 0)
            current_job["phase"] = str(progress.get("phase") or "running")
            current_node = progress.get("current_node")
            current_job["current_node"] = str(current_node) if current_node else None

    try:
        result = get_lab_config_diff(
            lab_name,
            router_names=router_names,
            should_cancel=(lambda: bool(cancel_event and cancel_event.is_set())),
            on_progress=_on_progress,
        )
        results = result.get("results") if isinstance(result, dict) else []
        results = results if isinstance(results, list) else []
        success_count = sum(1 for item in results if isinstance(item, dict) and item.get("ok"))

        with _CONFIG_DIFF_JOBS_LOCK:
            job["result"] = result
            job["total"] = len(results)
            job["completed_count"] = len(results)
            job["success_count"] = success_count
            if result.get("cancelled"):
                job["status"] = "cancelled"
                job["error"] = result.get("error") or "Cancelled by user"
            elif result.get("ok"):
                job["status"] = "completed"
            else:
                job["status"] = "error"
                job["error"] = result.get("error") or "Failed to get config diff"
            job["finished_at"] = _now_iso()
    except Exception as exc:
        logger.exception("config_diff_job failed job=%s lab=%s", job_id, lab_name, exc_info=exc)
        with _CONFIG_DIFF_JOBS_LOCK:
            job["status"] = "error"
            job["error"] = str(exc)
            job["finished_at"] = _now_iso()


def _run_reconfigure_job(job_id: str, lab_name: str, router_names: list[str] | None = None, mode: str = "base") -> None:
    """Background thread: run reconfigure and update job state per router.
    
    Args:
        job_id: UUID of the job
        lab_name: Name of the lab to reconfigure
        router_names: Optional list of router short names to reconfigure. If None/empty, all routers.
        mode: Reconfigure mode. "base" merges baseline config, "cleanup" removes extra config before apply.
    """
    job = _RECONFIG_JOBS.get(job_id)
    if not job:
        return

    with _RECONFIG_CANCEL_EVENTS_LOCK:
        cancel_event = _RECONFIG_CANCEL_EVENTS.get(job_id)

    details = get_lab_details(lab_name)
    if not details:
        with _RECONFIG_JOBS_LOCK:
            job["status"] = "error"
            job["error"] = "Lab not found"
            job["finished_at"] = _now_iso()
        logger.warning("reconfigure_job lab_not_found job=%s lab=%s", job_id, lab_name)
        return

    routers = details.get("routers", [])
    topology_file = str(details.get("topology_file") or "")
    lab_db_dir = _ensure_lab_db_dir(_resolve_lab_db_dir(lab_name, topology_file=topology_file))
    
    requested_routers = {str(r).strip().lower() for r in (router_names or [])} if router_names else set()
    logger.info("_run_reconfigure_job: mode=%s router_names=%s requested_routers=%s", mode, router_names, requested_routers)

    actionable_routers: list[dict[str, Any]] = []
    skipped_nodes: list[str] = []
    for router in routers:
        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        
        if requested_routers and short_name.lower() not in requested_routers:
            logger.debug("_run_reconfigure_job: skipping router short_name=%s (not in requested=%s)", short_name, requested_routers)
            skipped_nodes.append(short_name)
            continue
        
        if _is_linux_srv_node(router, short_name):
            skipped_nodes.append(short_name)
            continue
        logger.debug("_run_reconfigure_job: including router short_name=%s", short_name)
        actionable_routers.append(router)

    with _RECONFIG_JOBS_LOCK:
        job["total"] = len(actionable_routers)
        job["skipped_count"] = len(skipped_nodes)
        job["skipped_nodes"] = skipped_nodes

    success = 0
    cancelled = False
    for router in actionable_routers:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break

        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        family = SUPPORTED_FAMILIES.get(str(router.get("kind") or "").lower())
        mgmt_ip = str(router.get("mgmt_ipv4") or "").split("/")[0]

        if not family:
            logger.warning("reconfigure skip node=%s reason=unsupported_model", short_name)
            entry = {"node": short_name, "ok": False, "error": "Unsupported model"}
            with _RECONFIG_JOBS_LOCK:
                job["results"].append(entry)
                job["completed_count"] += 1
            continue

        cfg_path = os.path.join(lab_db_dir, f"{short_name}.cfg")
        if not os.path.exists(cfg_path):
            logger.warning("reconfigure missing_config node=%s path=%s", short_name, cfg_path)
            entry = {"node": short_name, "ok": False, "error": f"Missing static config: {cfg_path}"}
            with _RECONFIG_JOBS_LOCK:
                job["results"].append(entry)
                job["completed_count"] += 1
            continue

        try:
            with open(cfg_path, "r", encoding="utf-8") as file:
                raw_cfg = file.read()
        except Exception as exc:
            entry = {"node": short_name, "ok": False, "error": f"Read error: {exc}"}
            with _RECONFIG_JOBS_LOCK:
                job["results"].append(entry)
                job["completed_count"] += 1
            continue

        if not mgmt_ip:
            logger.warning("reconfigure missing_mgmt_ip node=%s", short_name)
            entry = {"node": short_name, "ok": False, "error": "Missing management IP"}
            with _RECONFIG_JOBS_LOCK:
                job["results"].append(entry)
                job["completed_count"] += 1
            continue

        username, password = _credentials_for_node(short_name, family)
        ssh = None
        try:
            ssh = _ssh_connect_with_retry(mgmt_ip, username, password, family)
            if family == "ios":
                logger.warning("reconfigure cisco_path node=%s family=ios mode=%s", short_name, mode)
                _apply_cisco_reconfigure(ssh, family, raw_cfg, mode, short_name)
            elif family == "iosxr":
                _apply_cisco_reconfigure(ssh, family, raw_cfg, mode, short_name)
            elif family == "ceos":
                _apply_cisco_reconfigure(ssh, family, raw_cfg, mode, short_name)
            elif family == "sros":
                _apply_sros(ssh, raw_cfg)
            elif family == "srl":
                _apply_srl(ssh, raw_cfg)
            else:
                # Junos / cRPD / vJunos
                config_blob = _prepare_junos(raw_cfg)
                try:
                    running_cfg_raw = _capture_running_config(ssh, family)
                    running_cfg = _normalize_captured_config(family, running_cfg_raw)
                    diff = _calculate_config_diff(config_blob, running_cfg, family)
                    if diff.get("match"):
                        logger.info("reconfigure noop node=%s reason=desired_config_already_running", short_name)
                    elif mode == "cleanup":
                        _apply_junos_cleanup(
                            ssh,
                            config_blob,
                            diff.get("delete_commands") or [],
                            lab_name=lab_name,
                            node_name=short_name,
                            username=username,
                            password=password,
                        )
                    else:
                        _apply_junos(
                            ssh,
                            config_blob,
                            lab_name=lab_name,
                            node_name=short_name,
                            username=username,
                            password=password,
                        )
                except Exception:
                    # If precheck fails unexpectedly, keep current behavior.
                    _apply_junos(
                        ssh,
                        config_blob,
                        lab_name=lab_name,
                        node_name=short_name,
                        username=username,
                        password=password,
                    )
            success += 1
            logger.info("reconfigure success node=%s", short_name)
            entry = {"node": short_name, "ok": True}
        except Exception as exc:
            logger.error("reconfigure failed node=%s error=%s", short_name, exc, exc_info=True)
            entry = {"node": short_name, "ok": False, "error": str(exc)}
        finally:
            if ssh is not None:
                ssh.close()

        with _RECONFIG_JOBS_LOCK:
            job["results"].append(entry)
            job["completed_count"] += 1
            job["success_count"] = success

    with _RECONFIG_JOBS_LOCK:
        job["ok"] = (success == len(actionable_routers)) if actionable_routers else False
        job["success_count"] = success
        if cancelled:
            job["status"] = "cancelled"
            job["ok"] = False
            job["error"] = "Cancelled by user"
        else:
            job["status"] = "completed"
        job["finished_at"] = _now_iso()

    logger.info(
        "reconfigure_job done job=%s lab=%s ok=%s success=%s total=%s",
        job_id, lab_name, job["ok"], success, len(routers),
    )


def _run_set_default_job(job_id: str, lab_name: str) -> None:
    job = _SET_DEFAULT_JOBS.get(job_id)
    if not job:
        return

    with _SET_DEFAULT_CANCEL_EVENTS_LOCK:
        cancel_event = _SET_DEFAULT_CANCEL_EVENTS.get(job_id)

    details = get_lab_details(lab_name)
    if not details:
        with _SET_DEFAULT_JOBS_LOCK:
            job["status"] = "error"
            job["error"] = "Lab not found"
            job["finished_at"] = _now_iso()
        logger.warning("set_default_job lab_not_found job=%s lab=%s", job_id, lab_name)
        return

    routers = details.get("routers", [])
    topology_file = str(details.get("topology_file") or "")
    lab_db_dir = Path(_ensure_lab_db_dir(_resolve_lab_db_dir(lab_name, topology_file=topology_file)))

    actionable_routers: list[dict[str, Any]] = []
    skipped_nodes: list[str] = []
    for router in routers:
        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        if _is_linux_srv_node(router, short_name):
            skipped_nodes.append(short_name)
            continue
        actionable_routers.append(router)

    with _SET_DEFAULT_JOBS_LOCK:
        job["total"] = len(actionable_routers)
        job["db_path"] = str(lab_db_dir)
        job["skipped_count"] = len(skipped_nodes)
        job["skipped_nodes"] = skipped_nodes

    captured_configs: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    success_count = 0
    cancelled = False

    for router in actionable_routers:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break

        container_name = str(router.get("name") or "")
        short_name = _short_node_name(container_name, lab_name)
        family = SUPPORTED_FAMILIES.get(str(router.get("kind") or "").lower())
        mgmt_ip = str(router.get("mgmt_ipv4") or "").split("/")[0]

        if not container_name or not family or not mgmt_ip:
            entry = {"node": short_name or "unknown", "ok": False, "error": "missing info/model"}
            failures.append({"node": short_name or "unknown", "error": "missing info/model"})
            with _SET_DEFAULT_JOBS_LOCK:
                job["results"].append(entry)
                job["completed_count"] += 1
            continue

        username, password = _credentials_for_node(short_name, family)
        ssh = None
        try:
            ssh = _ssh_connect_with_retry(mgmt_ip, username, password, family)
            running_cfg = _capture_running_config(ssh, family)
            captured_configs[short_name] = _normalize_captured_config(family, running_cfg)
            success_count += 1
            entry = {"node": short_name, "ok": True}
        except Exception as exc:
            logger.error("set_default_job failed node=%s error=%s", short_name, exc)
            failures.append({"node": short_name, "error": str(exc)})
            entry = {"node": short_name, "ok": False, "error": str(exc)}
        finally:
            if ssh is not None:
                ssh.close()

        with _SET_DEFAULT_JOBS_LOCK:
            job["results"].append(entry)
            job["completed_count"] += 1
            job["success_count"] = success_count

    try:
        if cancelled:
            with _SET_DEFAULT_JOBS_LOCK:
                job["status"] = "cancelled"
                job["ok"] = False
                job["error"] = "Cancelled by user"
                job["saved_count"] = len(captured_configs)
                job["finished_at"] = _now_iso()
            return

        if failures:
            with _SET_DEFAULT_JOBS_LOCK:
                job["status"] = "completed"
                job["ok"] = False
                job["error"] = "Unable to capture all running configurations"
                job["saved_count"] = len(captured_configs)
                job["finished_at"] = _now_iso()
            return

        backup_file = None
        if lab_db_dir.exists() and any(lab_db_dir.iterdir()):
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            backup_file = lab_db_dir.parent / f"{lab_name}-default-backup-{timestamp}.tgz"
            with tarfile.open(backup_file, "w:gz") as archive:
                archive.add(lab_db_dir, arcname=lab_db_dir.name)

        for cfg_file in lab_db_dir.glob("*.cfg"):
            try:
                cfg_file.unlink()
            except OSError:
                logger.warning("set_default_job unable_to_remove file=%s", cfg_file)

        for short_name, content in captured_configs.items():
            cfg_path = lab_db_dir / f"{short_name}.cfg"
            cfg_path.write_text(content, encoding="utf-8")

        with _SET_DEFAULT_JOBS_LOCK:
            job["status"] = "completed"
            job["ok"] = True
            job["saved_count"] = len(captured_configs)
            job["backup_file"] = str(backup_file) if backup_file else None
            job["finished_at"] = _now_iso()
    except Exception as exc:
        logger.error("set_default_job finalize failed lab=%s error=%s", lab_name, exc)
        with _SET_DEFAULT_JOBS_LOCK:
            job["status"] = "error"
            job["ok"] = False
            job["error"] = str(exc)
            job["saved_count"] = len(captured_configs)
            job["finished_at"] = _now_iso()

    logger.info(
        "set_default_job done job=%s lab=%s ok=%s success=%s total=%s",
        job_id,
        lab_name,
        job.get("ok"),
        success_count,
        len(actionable_routers),
    )


def _wait_for_prompt(chan: paramiko.Channel, pattern: str, timeout: int = 20) -> str:
    end_time = time.time() + timeout
    buf = ""
    rx = re.compile(pattern)
    while time.time() < end_time:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="ignore")
            buf += chunk
            if rx.search(buf):
                return buf
        else:
            time.sleep(0.1)
    tail = buf[-240:].replace("\n", "\\n").replace("\r", "\\r")
    raise TimeoutError(f"Timeout waiting prompt pattern={pattern} tail={tail}")


def _wait_for_prompt_quiet(
    chan: paramiko.Channel,
    pattern: str,
    timeout: int = 20,
    quiet_seconds: float = 0.7,
) -> str:
    """Wait until prompt is seen and the channel stays quiet briefly.

    This avoids returning on an intermediate prompt while the device still
    processes pasted configuration lines.
    """
    end_time = time.time() + timeout
    buf = ""
    rx = re.compile(pattern)
    saw_prompt = False
    last_data_at = time.time()

    while time.time() < end_time:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="ignore")
            buf += chunk
            last_data_at = time.time()
            if rx.search(buf):
                saw_prompt = True
            continue

        if saw_prompt and (time.time() - last_data_at) >= quiet_seconds:
            return buf

        time.sleep(0.1)

    tail = buf[-240:].replace("\n", "\\n").replace("\r", "\\r")
    raise TimeoutError(f"Timeout waiting stable prompt pattern={pattern} tail={tail}")


def _send_line(chan: paramiko.Channel, cmd: str) -> None:
    chan.send(cmd + "\n")


def _send_multiline_terminal_payload(
    chan: paramiko.Channel,
    payload: str,
    *,
    pause_every: int = 25,
    pause_seconds: float = 0.03,
) -> None:
    """Send multiline CLI payload progressively to avoid line loss on virtual routers."""
    lines = payload.splitlines()
    for index, line in enumerate(lines, start=1):
        chan.send(line + "\n")
        if pause_every > 0 and index % pause_every == 0:
            time.sleep(pause_seconds)
    if payload.endswith("\n") and not lines:
        chan.send("\n")
    elif payload.endswith("\n"):
        # Preserve intentional trailing newline from the source payload.
        chan.send("\n")
    chan.send("\x04")


def _ssh_connect(host: str, username: str, password: str, timeout: int = 20) -> paramiko.SSHClient:
    logger.info("ssh_connect host=%s user=%s", host, username)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=host,
        username=username,
        password=password,
        port=22,
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        # Banner can be delayed on virtual routers during control-plane spikes.
        banner_timeout=max(timeout, 45),
    )
    return ssh


def _ssh_connect_with_retry(host: str, username: str, password: str, family: str, timeout: int = 20) -> paramiko.SSHClient:
    attempts = 7 if family == "junos" else 3

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            ssh = _ssh_connect(host, username, password, timeout=timeout)
            if attempt > 1:
                logger.info(
                    "ssh_connect recovered host=%s user=%s family=%s attempt=%s/%s",
                    host,
                    username,
                    family,
                    attempt,
                    attempts,
                )
            return ssh
        except (socket.error, OSError, paramiko.SSHException) as exc:
            last_exc = exc
            if attempt >= attempts:
                break

            # Typical transient cases: banner read reset, handshake aborts, brief SSH daemon unavailability.
            wait_seconds = min(2 * attempt, 8)
            logger.warning(
                "ssh_connect retry host=%s user=%s family=%s attempt=%s/%s wait=%ss err=%s",
                host,
                username,
                family,
                attempt,
                attempts,
                wait_seconds,
                exc,
            )
            time.sleep(wait_seconds)

    if last_exc is None:
        raise RuntimeError(f"SSH connection failed host={host}: unknown error")
    raise RuntimeError(f"SSH connection failed host={host} after retries: {last_exc}") from last_exc


def _short_node_name(container_name: str, lab_name: str) -> str:
    prefix = f"clab-{lab_name}-"
    if container_name.startswith(prefix):
        return container_name[len(prefix):]
    return container_name


# Kinds that are always skipped for SSH config operations (no CLI to push to).
_SKIP_KINDS = frozenset({"linux", "host", "bridge", "ovs-bridge", "ovs_bridge"})


def _is_linux_srv_node(router: dict[str, Any], short_name: str) -> bool:
    """Return True for Linux/bridge nodes excluded from SSH config operations."""
    kind = str(router.get("kind") or "").strip().lower()
    normalized_short_name = str(short_name or "").strip().upper()

    if normalized_short_name.startswith("SRV"):
        return True
    if kind in _SKIP_KINDS:
        return True
    if "linux" in kind or "server" in kind:
        return True
    return False


def _credentials_for_node(short_name: str, family: str) -> tuple[str, str]:
    upper = short_name.upper()
    if upper.startswith("CE"):
        return "admin", "admin"
    if family == "srl":
        return "admin", "NokiaSrl1!"
    if family == "sros":
        return "admin", "admin"
    if family == "ceos":
        return "admin", "admin"
    if upper.startswith("J-") or family == "junos":
        return "admin", "admin@123"
    if upper.startswith("C-") or family == "iosxr":
        return "clab", "clab@123"
    return "admin", "admin"


def _sanitize_ios_like(config: str) -> list[str]:
    out = []
    in_cert_chain_block = False
    for line in config.splitlines():
        value = line.rstrip("\r")
        stripped = value.strip()

        if in_cert_chain_block:
            if stripped == "quit":
                in_cert_chain_block = False
            continue

        if not value or value.strip() == "!":
            continue
        if value.startswith("Building configuration") or value.startswith("Current configuration"):
            continue
        if stripped == "show running-config":
            continue
        if stripped == "end":
            continue
        if _IOS_CAPTURE_PROMPT_RE.match(stripped):
            continue
        # IOS PKI certificate chain payloads include long hex blobs and an
        # interactive submode that is brittle to replay in lab environments.
        if stripped.startswith("crypto pki certificate chain "):
            in_cert_chain_block = True
            continue
        out.append(value)
    return out


def _sanitize_iosxr(config: str) -> list[str]:
    out = []
    for line in config.splitlines():
        value = line.rstrip("\r")
        stripped = value.strip()
        if stripped.startswith("interface preconfigure "):
            value = value.replace("interface preconfigure ", "interface ", 1)
            stripped = value.strip()
        if not stripped:
            continue
        if stripped in {"^", "end", "!"}:
            continue
        if stripped.startswith("!!"):
            continue
        if stripped == "show running-config":
            continue
        if stripped.startswith("Building configuration") or stripped.startswith("Current configuration"):
            continue
        if stripped.startswith("Last configuration change"):
            continue
        if stripped.startswith("% ") or "Invalid input detected" in stripped:
            continue
        if _IOSXR_CAPTURE_TIMESTAMP_RE.match(stripped):
            continue
        if _IOSXR_CAPTURE_PROMPT_RE.match(stripped):
            continue
        if set(stripped) == {"-"} and len(stripped) >= 5:
            continue
        out.append(value)
    return out


def _normalize_captured_config(family: str, config_text: str) -> str:
    if family in {"ios", "ceos"}:
        lines = _sanitize_ios_like(config_text)
        if not lines:
            return ""
        return "\n".join(lines) + "\n"
    if family == "iosxr":
        lines = _sanitize_iosxr(config_text)
        if not lines:
            return ""
        return "\n".join(lines) + "\n"
    return config_text


def _prepare_iosxr_reconfigure(config_text: str) -> list[str]:
    sanitized = _sanitize_iosxr(config_text)
    prepared: list[str] = []
    in_mgmt_block = False

    for line in sanitized:
        stripped = line.strip()

        if in_mgmt_block:
            if line.startswith(" ") or line.startswith("\t"):
                continue
            in_mgmt_block = False

        if stripped == "interface MgmtEth0/RP0/CPU0/0":
            in_mgmt_block = True
            continue

        if (
            "MgmtEth0/RP0/CPU0/0" in stripped
            and (stripped.startswith("0.0.0.0/0 ") or stripped.startswith("::/0 "))
        ):
            continue

        prepared.append(line)

    return prepared


def _prepare_junos(config: str) -> str:
    kept_lines: list[str] = []
    for raw in config.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        kept_lines.append(stripped)
    return "\n".join(kept_lines)


def _apply_sros(ssh: paramiko.SSHClient, config_blob: str) -> None:
    """Push configuration to Nokia SR OS via SSH CLI (MD-CLI load from terminal)."""
    chan = ssh.invoke_shell()
    _wait_for_prompt(chan, r"[\$#>@]\s*$", 25)
    # Enter MD-CLI if not already there
    _send_line(chan, "environment more false")
    _wait_for_prompt(chan, r"[\$#>@]\s*$", 10)
    _send_line(chan, "edit-config exclusive")
    _wait_for_prompt(chan, r"\(ex\)\[\s*\]\s*#\s*$", 20)
    _send_line(chan, "load override terminal")
    _wait_for_prompt(chan, r"Load complete\.", 30)
    chan.send(config_blob)
    if not config_blob.endswith("\n"):
        chan.send("\n")
    chan.send("\x04")  # Ctrl-D to end multi-line input
    _wait_for_prompt(chan, r"\(ex\)\[\s*\]\s*#\s*$", 120)
    _send_line(chan, "commit")
    _wait_for_prompt(chan, r"[\$#>@]\s*$", 180)
    _send_line(chan, "quit-config")
    _wait_for_prompt(chan, r"[\$#>@]\s*$", 15)
    chan.close()


def _apply_srl(ssh: paramiko.SSHClient, config_blob: str) -> None:
    """Push configuration to Nokia SR Linux via SSH CLI (candidate commit)."""
    chan = ssh.invoke_shell()
    _wait_for_prompt(chan, r"--\s*$", 25)
    _send_line(chan, "enter candidate")
    _wait_for_prompt(chan, r"--\s*$", 10)
    _send_line(chan, "load file /dev/stdin")
    _wait_for_prompt(chan, r"Load \.", 15)
    chan.send(config_blob)
    if not config_blob.endswith("\n"):
        chan.send("\n")
    chan.send("\x04")
    _wait_for_prompt(chan, r"--\s*$", 120)
    _send_line(chan, "commit now")
    _wait_for_prompt(chan, r"--\s*$", 120)
    _send_line(chan, "quit")
    _wait_for_prompt(chan, r"[>$#]\s*$", 10)
    chan.close()


def _apply_ceos(ssh: paramiko.SSHClient, lines: list[str]) -> None:
    """Push configuration to Arista cEOS via SSH CLI (same as IOS-style)."""
    # cEOS CLI is EOS — same configure/end/write paradigm as IOS
    chan = ssh.invoke_shell()
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 20)
    _send_line(chan, "terminal length 0")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 10)
    _send_line(chan, "configure terminal")
    _wait_for_prompt(chan, _IOS_CONFIG_PROMPT_PATTERN, 10)
    if lines:
        chan.send("\n".join(lines) + "\n")
        apply_output = _wait_for_prompt_quiet(chan, _IOS_CONFIG_PROMPT_PATTERN, 300)
        if _IOS_APPLY_ERROR_RE.search(apply_output):
            tail = apply_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"IOS config apply failed tail={tail}")
    _send_line(chan, "end")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 15)
    _send_line(chan, "write memory")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 60)
    chan.close()


def _apply_ios(ssh: paramiko.SSHClient, lines: list[str]) -> None:
    chan = ssh.invoke_shell()
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 20)
    _send_line(chan, "terminal length 0")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 10)
    _send_line(chan, "configure terminal")
    _wait_for_prompt(chan, _IOS_CONFIG_PROMPT_PATTERN, 10)
    if lines:
        chan.send("\n".join(lines) + "\n")
        apply_output = _wait_for_prompt_quiet(chan, _IOS_CONFIG_PROMPT_PATTERN, 300)
        if _IOS_APPLY_ERROR_RE.search(apply_output):
            tail = apply_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"IOS config apply failed tail={tail}")
    _send_line(chan, "end")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 15)
    _send_line(chan, "write memory")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 60)
    chan.close()


def _apply_iosxr(ssh: paramiko.SSHClient, lines: list[str]) -> None:
    chan = ssh.invoke_shell()
    _wait_for_prompt(chan, _IOSXR_EXEC_PROMPT_PATTERN, 20)
    _send_line(chan, "terminal length 0")
    _wait_for_prompt(chan, _IOSXR_EXEC_PROMPT_PATTERN, 10)
    _send_line(chan, "configure terminal")
    _wait_for_prompt(chan, _IOSXR_CONFIG_PROMPT_PATTERN, 10)
    for line in lines:
        chan.send(line + "\n")
    apply_output = _wait_for_prompt(chan, _IOSXR_CONFIG_PROMPT_PATTERN, 180)
    if _IOSXR_APPLY_ERROR_RE.search(apply_output):
        tail = apply_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"IOS-XR config apply failed tail={tail}")
    _send_line(chan, "commit")
    commit_output = _wait_for_prompt(
        chan,
        _IOSXR_ANY_PROMPT_PATTERN,
        300,
    )
    if _IOSXR_COMMIT_ERROR_RE.search(commit_output):
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"IOS-XR commit failed tail={tail}")
    _send_line(chan, "end")
    _wait_for_prompt(chan, _IOSXR_EXEC_PROMPT_PATTERN, 10)
    chan.close()


def _apply_iosxr_replace(ssh: paramiko.SSHClient, lines: list[str]) -> None:
    """Replace IOS-XR candidate config from terminal input, then commit.

    This is used for cleanup mode so extra running config not present in the
    baseline is removed by the replace operation itself.
    """
    remote_tmp = f"/tmp/vlm_iosxr_replace_{uuid.uuid4().hex}.cfg"
    payload = "\n".join(lines)
    if payload and not payload.endswith("\n"):
        payload += "\n"

    sftp = None
    chan = None
    try:
        sftp = ssh.open_sftp()
        with sftp.file(remote_tmp, "w") as remote_file:
            remote_file.write(payload)
        sftp.close()
        sftp = None

        chan = ssh.invoke_shell()
        _wait_for_prompt(chan, _IOSXR_EXEC_PROMPT_PATTERN, 20)
        _send_line(chan, "terminal length 0")
        _wait_for_prompt(chan, _IOSXR_EXEC_PROMPT_PATTERN, 10)
        _send_line(chan, "configure terminal")
        _wait_for_prompt(chan, _IOSXR_CONFIG_PROMPT_PATTERN, 10)

        _send_line(chan, f"load {remote_tmp}")
        load_output = _wait_for_prompt_quiet(chan, _IOSXR_CONFIG_PROMPT_PATTERN, 300)
        if _IOSXR_APPLY_ERROR_RE.search(load_output):
            tail = load_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"IOS-XR load replace failed tail={tail}")

        _send_line(chan, "commit replace force")
        latest_output = _wait_for_prompt(
            chan,
            rf"(?im)({_IOSXR_REPLACE_CONFIRM_PATTERN}|(?:^\r*(?:[^\r\n]*\([^)\r\n]*\)#|[^\r\n]*[#>])\s*$))",
            300,
        )
        commit_output = latest_output
        confirm_attempts = 0
        while re.search(_IOSXR_REPLACE_CONFIRM_PATTERN, latest_output, re.IGNORECASE):
            if confirm_attempts >= 3:
                tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
                raise RuntimeError(f"IOS-XR replace confirmation loop exceeded tail={tail}")
            confirm_attempts += 1
            _send_line(chan, "yes")
            latest_output = _wait_for_prompt(
                chan,
                rf"(?im)({_IOSXR_REPLACE_CONFIRM_PATTERN}|(?:^\r*(?:[^\r\n]*\([^)\r\n]*\)#|[^\r\n]*[#>])\s*$))",
                900,
            )
            commit_output += latest_output
        if _IOSXR_COMMIT_ERROR_RE.search(commit_output):
            tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"IOS-XR commit failed tail={tail}")
        _send_line(chan, "end")
        _wait_for_prompt(chan, _IOSXR_EXEC_PROMPT_PATTERN, 10)
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass
        try:
            ssh.exec_command(f"rm -f {remote_tmp}", timeout=10)
        except Exception:
            pass


def _apply_ios_single_session(ssh: paramiko.SSHClient, base_lines: list[str], mode: str, node_name: str) -> None:
    """Capture running config AND apply config in a single IOS shell session.

    Cisco IOL only supports one interactive shell per SSH connection.  Opening
    a second invoke_shell() call raises a channel error.  By doing capture +
    apply in one channel we avoid that limitation entirely.
    """
    chan = ssh.invoke_shell()
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 20)
    _send_line(chan, "terminal length 0")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 10)

    lines_to_apply = base_lines
    if mode == "cleanup":
        _send_line(chan, "show running-config")
        running_raw = _wait_for_prompt_quiet(chan, _IOS_EXEC_PROMPT_PATTERN, 120)
        running_cfg = _normalize_captured_config("ios", running_raw)
        diff = _calculate_config_diff("\n".join(base_lines), running_cfg, "ios")
        if diff.get("match"):
            logger.info("reconfigure noop node=%s reason=desired_config_already_running", node_name)
            chan.close()
            return
        lines_to_apply = (diff.get("delete_commands") or []) + base_lines

    _send_line(chan, "configure terminal")
    _wait_for_prompt(chan, _IOS_CONFIG_PROMPT_PATTERN, 10)
    last_was_indented = False
    if lines_to_apply:
        for line in lines_to_apply:
            raw = line.rstrip("\r")
            if not raw.strip():
                continue

            is_top_level = not raw.startswith((" ", "\t"))
            if is_top_level and last_was_indented:
                # Context reset only when leaving a submode block (e.g. interface)
                # to ensure the next top-level command is interpreted correctly.
                chan.send("\x1a")
                _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 20)
                _send_line(chan, "configure terminal")
                _wait_for_prompt(chan, _IOS_CONFIG_PROMPT_PATTERN, 10)

            _send_line(chan, raw.strip())
            apply_output = _wait_for_prompt_quiet(chan, _IOS_CONFIG_PROMPT_PATTERN, 30)
            if _IOS_APPLY_ERROR_RE.search(apply_output):
                tail = apply_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
                raise RuntimeError(f"IOS config apply failed tail={tail}")
            last_was_indented = not is_top_level

    chan.send("\x1a")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 20)
    _send_line(chan, "end")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 15)
    _send_line(chan, "write memory")
    _wait_for_prompt(chan, _IOS_EXEC_PROMPT_PATTERN, 60)
    chan.close()


def _apply_cisco_reconfigure(ssh: paramiko.SSHClient, family: str, raw_cfg: str, mode: str, node_name: str) -> None:
    requested_mode = (mode or "base").strip().lower()
    if requested_mode == "cleanup":
        # Operational decision for Cisco families:
        # - IOS: single-session cleanup (diff delete commands + baseline apply)
        # - cEOS: base-only apply
        # - IOS-XR: true replace load from terminal
        effective_mode = "replace" if family == "iosxr" else ("cleanup" if family == "ios" else "base")
        logger.warning(
            "reconfigure cisco_cleanup_replace node=%s family=%s requested_mode=cleanup effective_mode=%s",
            node_name,
            family,
            effective_mode,
        )

    if family == "ios":
        # IOS (Cisco IOL): for cleanup mode we need delete+base behavior to
        # remove extra user config (e.g. ad-hoc Loopback999).
        base_lines = _sanitize_ios_like(raw_cfg)
        if requested_mode == "cleanup":
            _apply_ios_single_session(ssh, base_lines, "cleanup", node_name)
        else:
            _apply_ios(ssh, base_lines)
        return

    if family == "iosxr":
        # Keep full sanitized IOS-XR baseline for replace mode. The merge helper
        # strips management blocks and is only suitable for merge-style applies.
        replace_lines = _sanitize_iosxr(raw_cfg)
        merge_lines = _prepare_iosxr_reconfigure(raw_cfg)
    else:
        replace_lines = _sanitize_ios_like(raw_cfg)
        merge_lines = replace_lines

    if family == "iosxr" and requested_mode == "cleanup":
        _apply_iosxr_replace(ssh, replace_lines)
        return

    # Cisco families use replace/base-only apply (no pre-diff delete pass).
    lines_to_apply = merge_lines

    if family == "iosxr":
        _apply_iosxr(ssh, lines_to_apply)
    else:
        _apply_ceos(ssh, lines_to_apply)


def _apply_junos(
    ssh: paramiko.SSHClient,
    config_blob: str,
    lab_name: str | None = None,
    node_name: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> None:
    force_rebuild = str(os.getenv("VLM_JUNOS_FORCE_REBUILD", "0")).strip().lower() in {"1", "true", "yes", "on"}
    allow_rebuild_fallback = str(os.getenv("VLM_JUNOS_ALLOW_REBUILD_FALLBACK", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    apply_once = _apply_junos_once if force_rebuild else _apply_junos_once_merge
    strategy = "rebuild" if force_rebuild else "merge"

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            apply_once(ssh, config_blob)
            return
        except (TimeoutError, RuntimeError) as exc:
            message = str(exc)
            if attempt >= max_attempts or not _try_junos_recovery(
                message,
                lab_name=lab_name,
                node_name_hint=node_name,
                username=username,
                password=password,
            ):
                if (
                    not force_rebuild
                    and allow_rebuild_fallback
                    and _JUNOS_DUP_LOCAL_ADDR_RE.search(message) is None
                ):
                    logger.warning(
                        "junos_apply %s_failed trying_rebuild_fallback reason=%s",
                        strategy,
                        message[:180],
                    )
                    _apply_junos_once(ssh, config_blob)
                    return
                raise
            logger.warning(
                "junos_apply strategy=%s recovery_retry attempt=%s/%s reason=%s",
                strategy,
                attempt + 1,
                max_attempts,
                message[:180],
            )
            time.sleep(2)


def _apply_junos_once(ssh: paramiko.SSHClient, config_blob: str) -> None:
    chan = ssh.invoke_shell()
    initial = _wait_for_prompt(chan, r"[>%] ?$", 20)
    # Some Junos nodes land in shell prompt (%). Enter CLI first.
    if re.search(r"%\s*$", initial):
        _send_line(chan, "cli")
        _wait_for_prompt(chan, r">\s*$", 15)
    _send_line(chan, "configure")
    _wait_for_prompt(chan, r"# ?$", 10)
    _send_line(chan, "delete")
    delete_output = _wait_for_prompt(
        chan,
        r"(# ?$|Delete everything under this level\? \[yes,no\] \(no\))",
        90,
    )
    if "[yes,no]" in delete_output:
        _send_line(chan, "yes")
        _wait_for_prompt(chan, r"# ?$", 240)

    _send_line(chan, "load set terminal")
    _wait_for_prompt(chan, r"\[Type .*\]", 30)
    _send_multiline_terminal_payload(chan, config_blob)
    _wait_for_prompt(chan, r"# ?$", 180)

    # Use commit + explicit exit for compatibility across Junos variants.
    _send_line(chan, "commit")
    commit_output = _wait_for_prompt_quiet(
        chan,
        r"([>%] ?$|# ?$|Exit with uncommitted changes\? \[yes,no\] \(yes\)|error:)",
        900,
        quiet_seconds=1.0,
    )
    if "Exit with uncommitted changes?" in commit_output:
        _send_line(chan, "yes")
        _wait_for_prompt(chan, r"[>%] ?$", 30)
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos commit failed, exited with uncommitted changes tail={tail}")
    if _JUNOS_COMMIT_ERROR_RE.search(commit_output):
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos commit failed tail={tail}")
    final_mode_output = commit_output
    if re.search(r"# ?$", commit_output):
        _send_line(chan, "exit")
        exit_output = _wait_for_prompt(
            chan,
            r"([>%] ?$|Exit with uncommitted changes\? \[yes,no\] \(yes\))",
            30,
        )
        final_mode_output = exit_output
        if "Exit with uncommitted changes?" in exit_output:
            _send_line(chan, "yes")
            _wait_for_prompt(chan, r"[>%] ?$", 30)
            tail = exit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"Junos commit failed, exited with uncommitted changes tail={tail}")
    if not re.search(r"[>%] ?$", final_mode_output):
        tail = final_mode_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Unexpected Junos commit output tail={tail}")
    chan.close()


def _apply_junos_once_merge(ssh: paramiko.SSHClient, config_blob: str) -> None:
    chan = ssh.invoke_shell()
    initial = _wait_for_prompt(chan, r"[>%] ?$", 20)
    if re.search(r"%\s*$", initial):
        _send_line(chan, "cli")
        _wait_for_prompt(chan, r">\s*$", 15)
    _send_line(chan, "configure")
    _wait_for_prompt(chan, r"# ?$", 10)

    _send_line(chan, "load set terminal")
    _wait_for_prompt(chan, r"\[Type .*\]", 30)
    _send_multiline_terminal_payload(chan, config_blob)
    _wait_for_prompt(chan, r"# ?$", 180)

    _send_line(chan, "commit")
    commit_output = _wait_for_prompt_quiet(
        chan,
        r"([>%] ?$|# ?$|Exit with uncommitted changes\? \[yes,no\] \(yes\)|error:)",
        900,
        quiet_seconds=1.0,
    )
    if "Exit with uncommitted changes?" in commit_output:
        _send_line(chan, "yes")
        _wait_for_prompt(chan, r"[>%] ?$", 30)
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos commit failed (merge fallback), exited with uncommitted changes tail={tail}")
    if _JUNOS_COMMIT_ERROR_RE.search(commit_output):
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos commit failed (merge fallback) tail={tail}")

    final_mode_output = commit_output
    if re.search(r"# ?$", commit_output):
        _send_line(chan, "exit")
        exit_output = _wait_for_prompt(
            chan,
            r"([>%] ?$|Exit with uncommitted changes\? \[yes,no\] \(yes\))",
            30,
        )
        final_mode_output = exit_output
        if "Exit with uncommitted changes?" in exit_output:
            _send_line(chan, "yes")
            _wait_for_prompt(chan, r"[>%] ?$", 30)
            tail = exit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"Junos commit failed (merge fallback), exited with uncommitted changes tail={tail}")
    if not re.search(r"[>%] ?$", final_mode_output):
        tail = final_mode_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Unexpected Junos commit output (merge fallback) tail={tail}")
    chan.close()


def _apply_junos_cleanup(
    ssh: paramiko.SSHClient,
    config_blob: str,
    delete_commands: list[str],
    lab_name: str | None = None,
    node_name: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> None:
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            _apply_junos_once_cleanup(ssh, config_blob, delete_commands)
            return
        except (TimeoutError, RuntimeError) as exc:
            message = str(exc)
            if attempt >= max_attempts or not _try_junos_recovery(
                message,
                lab_name=lab_name,
                node_name_hint=node_name,
                username=username,
                password=password,
            ):
                raise
            logger.warning(
                "junos_cleanup recovery_retry attempt=%s/%s reason=%s",
                attempt + 1,
                max_attempts,
                message[:180],
            )
            time.sleep(2)


def _apply_junos_once_cleanup(ssh: paramiko.SSHClient, config_blob: str, delete_commands: list[str]) -> None:
    chan = ssh.invoke_shell()
    initial = _wait_for_prompt(chan, r"[>%] ?$", 20)
    if re.search(r"%\s*$", initial):
        _send_line(chan, "cli")
        _wait_for_prompt(chan, r">\s*$", 15)
    _send_line(chan, "configure")
    _wait_for_prompt(chan, r"# ?$", 10)

    if delete_commands:
        for command in delete_commands:
            _send_line(chan, command)
            delete_output = _wait_for_prompt_quiet(chan, r"(# ?$|error:)", 60, quiet_seconds=0.25)
            if re.search(r"(?im)\berror:\b", delete_output):
                if re.search(r"(?im)statement not found", delete_output):
                    continue
                tail = delete_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
                raise RuntimeError(f"Junos cleanup delete command failed cmd={command} tail={tail}")

    _send_line(chan, "load set terminal")
    _wait_for_prompt(chan, r"\[Type .*\]", 30)
    _send_multiline_terminal_payload(chan, config_blob)
    load_output = _wait_for_prompt_quiet(chan, r"(# ?$|error:)", 240, quiet_seconds=1.0)
    if re.search(r"(?im)\berror:\b", load_output):
        tail = load_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos cleanup load phase failed tail={tail}")

    _send_line(chan, "commit")
    commit_output = _wait_for_prompt_quiet(
        chan,
        r"([>%] ?$|# ?$|Exit with uncommitted changes\? \[yes,no\] \(yes\)|error:)",
        900,
        quiet_seconds=1.0,
    )
    if "Exit with uncommitted changes?" in commit_output:
        _send_line(chan, "yes")
        _wait_for_prompt(chan, r"[>%] ?$", 30)
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos cleanup commit failed, exited with uncommitted changes tail={tail}")
    if _JUNOS_COMMIT_ERROR_RE.search(commit_output):
        tail = commit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Junos cleanup commit failed tail={tail}")

    final_mode_output = commit_output
    if re.search(r"# ?$", commit_output):
        _send_line(chan, "exit")
        exit_output = _wait_for_prompt(
            chan,
            r"([>%] ?$|Exit with uncommitted changes\? \[yes,no\] \(yes\))",
            30,
        )
        final_mode_output = exit_output
        if "Exit with uncommitted changes?" in exit_output:
            _send_line(chan, "yes")
            _wait_for_prompt(chan, r"[>%] ?$", 30)
            tail = exit_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
            raise RuntimeError(f"Junos cleanup commit failed, exited with uncommitted changes tail={tail}")
    if not re.search(r"[>%] ?$", final_mode_output):
        tail = final_mode_output[-240:].replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(f"Unexpected Junos cleanup output tail={tail}")
    chan.close()


def _capture_running_config(ssh: paramiko.SSHClient, family: str) -> str:
    chan = ssh.invoke_shell()
    initial = _wait_for_prompt(chan, r"[#>%$@\-]\s*$", 20)

    if family in {"ios", "iosxr", "ceos"}:
        # IOS / IOS-XR / EOS (cEOS) all share the same show running-config flow
        _send_line(chan, "terminal length 0")
        _wait_for_prompt(chan, r"[#>%]\s*$", 10)
        _send_line(chan, "show running-config")
        output = _wait_for_prompt(chan, r"[#>%]\s*$", 120)
    elif family == "sros":
        # Nokia SR OS — MD-CLI
        _send_line(chan, "environment more false")
        _wait_for_prompt(chan, r"[#\$@\-]\s*$", 10)
        _send_line(chan, "info full-context")
        output = _wait_for_prompt(chan, r"[#\$@\-]\s*$", 120)
    elif family == "srl":
        # Nokia SR Linux
        _send_line(chan, "enter running")
        _wait_for_prompt(chan, r"--\s*$", 10)
        _send_line(chan, "info flat")
        output = _wait_for_prompt(chan, r"--\s*$", 120)
        _send_line(chan, "quit")
        _wait_for_prompt(chan, r"[>$#]\s*$", 10)
    else:
        # Junos / cRPD / vJunos — export can start in shell prompt (%)
        if re.search(r"%\s*$", initial):
            _send_line(chan, "cli")
            _wait_for_prompt(chan, r">\s*$", 15)
        _send_line(chan, "show configuration | display set | no-more")
        output = _wait_for_prompt(chan, r"[>%] ?$", 120)

    chan.close()
    return output


def _resolve_db_root() -> str:
    env_value = os.getenv("LAB_CONFIG_DB_ROOT")
    if env_value:
        return env_value

    project_root = Path(__file__).resolve().parents[2]
    candidate_roots: list[Path] = [
        project_root / "config_dc",
        project_root / "config_db",
        Path.cwd() / "config_dc",
        Path.cwd() / "config_db",
    ]

    seen: set[str] = set()
    for candidate in candidate_roots:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        if candidate.is_dir():
            return candidate_str

    # Fallback if the static DB is not mounted yet.
    return str(project_root / "config_db")


def _resolve_lab_db_dir(lab_name: str, topology_file: str | None = None) -> str:
    env_value = os.getenv("LAB_CONFIG_DB_ROOT")
    if env_value:
        return os.path.join(env_value, lab_name)

    if topology_file:
        topo_path = Path(topology_file).expanduser()
        candidate_topologies = [topo_path]
        if not topo_path.is_absolute():
            candidate_topologies.append(Path.cwd() / topo_path)

        for candidate in candidate_topologies:
            try:
                exists = candidate.exists()
            except OSError:
                exists = False
            if exists:
                return str(candidate.parent / "config_db" / lab_name)

        # Use topology-relative fallback only if the parent dir is accessible.
        try:
            topo_path.parent.stat()
            return str(topo_path.parent / "config_db" / lab_name)
        except OSError:
            pass  # topology is in an inaccessible path (e.g. another user's home)

    root = _resolve_db_root()
    return os.path.join(root, lab_name)


def _ensure_lab_db_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _parse_set_config(config_text: str) -> set[str]:
    """Parse 'set' format config into a normalized set of lines (without comments/empty lines)."""
    lines = set()
    for raw in config_text.splitlines():
        line = raw.rstrip("\r").strip()
        if line and not line.startswith("#"):
            lines.add(line)
    return lines


def _generate_delete_commands_junos(base_lines: set[str], running_lines: set[str]) -> list[str]:
    """Generate Junos 'delete' commands to remove lines present in running but not in base."""
    commands = []
    
    # Lines in running but not in base should be deleted
    lines_to_delete = running_lines - base_lines

    # Prefer subtree deletion for extra interface units to avoid leaving hidden
    # leaves behind on Junos variants that do not render identical set output.
    extra_interface_units: set[tuple[str, str]] = set()
    for line in lines_to_delete:
        match = re.match(r"^set\s+interfaces\s+(\S+)\s+unit\s+(\S+)\b", line)
        if match:
            extra_interface_units.add((match.group(1), match.group(2)))

    for iface, unit in sorted(extra_interface_units):
        commands.append(f"delete interfaces {iface} unit {unit}")
    
    for line in sorted(lines_to_delete):
        if re.match(r"^set\s+interfaces\s+\S+\s+unit\s+\S+\b", line):
            # Already covered by subtree delete command above.
            continue
        # Parse "set interfaces X ..." → "delete interfaces X"
        if line.startswith("set "):
            # Remove "set " prefix and use "delete" instead
            path = line[4:]  # Remove "set "
            commands.append(f"delete {path}")
    
    return commands


def _generate_delete_commands_cisco(base_lines: set[str], running_lines: set[str]) -> list[str]:
    """Generate Cisco 'no' commands to remove lines present in running but not in base."""
    commands = []
    
    # Lines in running but not in base should be removed with 'no'
    lines_to_delete = running_lines - base_lines
    
    for line in sorted(lines_to_delete):
        # For Cisco, prepend "no " to the line
        if line.strip():
            stripped = line.strip()
            if stripped.startswith("no "):
                commands.append(stripped[3:])
            else:
                commands.append(f"no {stripped}")
    
    return commands


def _parse_ios_top_level_commands(config_text: str) -> set[str]:
    """Extract top-level IOS/EOS commands (global config scope only).

    Cleanup deletes must be valid from `(config)#`.  Indented lines belong to
    submodes like `(config-if)#` / `(config-line)#` and cannot be safely turned
    into direct `no ...` commands at global scope.
    """
    commands: set[str] = set()
    for raw in config_text.splitlines():
        line = raw.replace("\x08", "").rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            continue
        if line.startswith(" ") or line.startswith("\t"):
            continue
        if stripped in {"end", "show running-config"}:
            continue
        if stripped.startswith("Building configuration") or stripped.startswith("Current configuration"):
            continue
        if stripped.startswith("Last configuration change"):
            continue
        if stripped.startswith("% "):
            continue
        if _IOS_CAPTURE_PROMPT_RE.match(stripped):
            continue
        commands.add(stripped)
    return commands


def _generate_delete_commands_ios_top_level(base_config: str, running_config: str) -> list[str]:
    """Generate IOS/EOS cleanup deletes only for global/top-level commands."""
    base_top = _parse_ios_top_level_commands(base_config)
    running_top = _parse_ios_top_level_commands(running_config)
    top_level_extra = running_top - base_top

    commands: list[str] = []
    for line in sorted(top_level_extra):
        if line.startswith("no "):
            commands.append(line[3:])
        else:
            commands.append(f"no {line}")
    return commands


def _calculate_config_diff(
    base_config: str,
    running_config: str,
    family: str
) -> dict[str, Any]:
    """Calculate diff between base and running config, return diffs and delete commands."""
    base_lines = _parse_set_config(base_config)
    running_lines = _parse_set_config(running_config)
    
    # Lines in base but not in running (need to be added)
    missing_lines = base_lines - running_lines
    
    # Lines in running but not in base (should be deleted for cleanup)
    extra_lines = running_lines - base_lines
    
    # Generate delete commands based on device family
    delete_commands = []
    if family in ("junos", "vjunos", "crpd"):
        delete_commands = _generate_delete_commands_junos(base_lines, running_lines)
    elif family in ("ios", "ceos"):
        delete_commands = _generate_delete_commands_ios_top_level(base_config, running_config)
    elif family == "iosxr":
        delete_commands = _generate_delete_commands_cisco(base_lines, running_lines)
    else:
        # For other families, generate basic delete format
        delete_commands = list(extra_lines)
    
    return {
        "missing_from_running": sorted(list(missing_lines)),
        "extra_in_running": sorted(list(extra_lines)),
        "delete_commands": delete_commands,
        "match": len(missing_lines) == 0 and len(extra_lines) == 0,
    }


def get_lab_config_diff(
    lab_name: str,
    router_names: list[str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Get config diff (running vs base) for specified routers in a lab."""
    logger.info("get_lab_config_diff lab=%s router_names=%s", lab_name, router_names or "all")

    details = get_lab_details(lab_name)
    if not details:
        return {"ok": False, "error": "Lab not found"}

    routers = details.get("routers", [])
    topology_file = str(details.get("topology_file") or "")
    lab_db_dir = _ensure_lab_db_dir(_resolve_lab_db_dir(lab_name, topology_file=topology_file))

    requested_routers = {r.upper().strip() for r in (router_names or [])}
    target_routers: list[tuple[dict[str, Any], str, str, str]] = []
    for router in routers:
        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        if requested_routers and short_name.upper() not in requested_routers:
            continue
        if _is_linux_srv_node(router, short_name):
            continue
        family = SUPPORTED_FAMILIES.get(str(router.get("kind") or "").lower())
        mgmt_ip = str(router.get("mgmt_ipv4") or "").split("/")[0]
        if not family or not mgmt_ip:
            continue
        target_routers.append((router, short_name, family, mgmt_ip))

    total_expected = len(target_routers)
    results: list[dict[str, Any]] = []
    success_count = 0

    if on_progress:
        try:
            on_progress({
                "phase": "running",
                "total": total_expected,
                "completed_count": 0,
                "success_count": 0,
                "current_node": None,
            })
        except Exception:
            pass

    for _, short_name, family, mgmt_ip in target_routers:
        if should_cancel and should_cancel():
            return {
                "ok": False,
                "cancelled": True,
                "error": "Cancelled by user",
                "lab": lab_name,
                "db_path": lab_db_dir,
                "results": results,
            }

        if on_progress:
            try:
                on_progress({
                    "phase": "processing",
                    "total": total_expected,
                    "completed_count": len(results),
                    "success_count": success_count,
                    "current_node": short_name,
                })
            except Exception:
                pass

        cfg_path = os.path.join(lab_db_dir, f"{short_name}.cfg")
        if not os.path.exists(cfg_path):
            results.append({
                "node": short_name,
                "ok": False,
                "error": f"Base config not found: {cfg_path}",
            })
            if on_progress:
                try:
                    on_progress({
                        "phase": "processing",
                        "total": total_expected,
                        "completed_count": len(results),
                        "success_count": success_count,
                        "current_node": short_name,
                    })
                except Exception:
                    pass
            continue

        try:
            with open(cfg_path, "r", encoding="utf-8") as file:
                base_cfg = file.read()
        except Exception as exc:
            results.append({
                "node": short_name,
                "ok": False,
                "error": f"Read base config error: {exc}",
            })
            if on_progress:
                try:
                    on_progress({
                        "phase": "processing",
                        "total": total_expected,
                        "completed_count": len(results),
                        "success_count": success_count,
                        "current_node": short_name,
                    })
                except Exception:
                    pass
            continue

        username, password = _credentials_for_node(short_name, family)
        ssh = None
        try:
            ssh = _ssh_connect_with_retry(mgmt_ip, username, password, family)
            running_cfg = _capture_running_config(ssh, family)
            ssh.close()
            ssh = None
            diff = _calculate_config_diff(base_cfg, running_cfg, family)

            results.append({
                "node": short_name,
                "ok": True,
                "base_config_lines": len(base_cfg.splitlines()),
                "running_config_lines": len(running_cfg.splitlines()),
                "diff": diff,
            })
            success_count += 1
        except (socket.error, paramiko.SSHException, TimeoutError, RuntimeError) as exc:
            logger.error("config_diff failed node=%s error=%s", short_name, exc)
            results.append({
                "node": short_name,
                "ok": False,
                "error": str(exc),
            })
        finally:
            if ssh is not None:
                ssh.close()

        if on_progress:
            try:
                on_progress({
                    "phase": "processing",
                    "total": total_expected,
                    "completed_count": len(results),
                    "success_count": success_count,
                    "current_node": short_name,
                })
            except Exception:
                pass

    if on_progress:
        try:
            on_progress({
                "phase": "finalizing",
                "total": total_expected,
                "completed_count": len(results),
                "success_count": success_count,
                "current_node": None,
            })
        except Exception:
            pass

    return {
        "ok": len(results) > 0,
        "lab": lab_name,
        "db_path": lab_db_dir,
        "results": results,
    }


def reconfigure_lab_from_static_db(lab_name: str, mode: str = "base") -> dict[str, Any]:
    logger.info("reconfigure_lab start lab=%s", lab_name)
    details = get_lab_details(lab_name)
    if not details:
        return {"ok": False, "error": "Lab not found"}

    routers = details.get("routers", [])
    topology_file = str(details.get("topology_file") or "")
    lab_db_dir = _ensure_lab_db_dir(_resolve_lab_db_dir(lab_name, topology_file=topology_file))

    results: list[dict[str, Any]] = []
    skipped_nodes: list[str] = []
    actionable_routers: list[dict[str, Any]] = []
    for router in routers:
        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        if _is_linux_srv_node(router, short_name):
            skipped_nodes.append(short_name)
            continue
        actionable_routers.append(router)
    success = 0

    for router in actionable_routers:
        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        family = SUPPORTED_FAMILIES.get(str(router.get("kind") or "").lower())
        mgmt_ip = str(router.get("mgmt_ipv4") or "").split("/")[0]

        if not family:
            logger.warning("reconfigure skip node=%s reason=unsupported_model", short_name)
            results.append({"node": short_name, "ok": False, "error": "Unsupported model"})
            continue

        cfg_path = os.path.join(lab_db_dir, f"{short_name}.cfg")
        if not os.path.exists(cfg_path):
            logger.warning("reconfigure missing_config node=%s path=%s", short_name, cfg_path)
            results.append({"node": short_name, "ok": False, "error": f"Missing static config: {cfg_path}"})
            continue

        try:
            with open(cfg_path, "r", encoding="utf-8") as file:
                raw_cfg = file.read()
        except Exception as exc:
            results.append({"node": short_name, "ok": False, "error": f"Read error: {exc}"})
            continue

        if not mgmt_ip:
            logger.warning("reconfigure missing_mgmt_ip node=%s", short_name)
            results.append({"node": short_name, "ok": False, "error": "Missing management IP"})
            continue

        username, password = _credentials_for_node(short_name, family)
        ssh = None
        try:
            ssh = _ssh_connect_with_retry(mgmt_ip, username, password, family)
            if family == "ios":
                logger.warning("reconfigure cisco_path node=%s family=ios mode=%s", short_name, mode)
                _apply_cisco_reconfigure(ssh, family, raw_cfg, mode, short_name)
            elif family == "iosxr":
                _apply_cisco_reconfigure(ssh, family, raw_cfg, mode, short_name)
            elif family == "ceos":
                _apply_cisco_reconfigure(ssh, family, raw_cfg, mode, short_name)
            elif family == "sros":
                _apply_sros(ssh, raw_cfg)
            elif family == "srl":
                _apply_srl(ssh, raw_cfg)
            else:
                # Junos / cRPD / vJunos
                config_blob = _prepare_junos(raw_cfg)
                try:
                    running_cfg_raw = _capture_running_config(ssh, family)
                    running_cfg = _normalize_captured_config(family, running_cfg_raw)
                    diff = _calculate_config_diff(config_blob, running_cfg, family)
                    if diff.get("match"):
                        logger.info("reconfigure noop node=%s reason=desired_config_already_running", short_name)
                    elif mode == "cleanup":
                        _apply_junos_cleanup(
                            ssh,
                            config_blob,
                            diff.get("delete_commands") or [],
                            lab_name=lab_name,
                            node_name=short_name,
                            username=username,
                            password=password,
                        )
                    else:
                        _apply_junos(
                            ssh,
                            config_blob,
                            lab_name=lab_name,
                            node_name=short_name,
                            username=username,
                            password=password,
                        )
                except Exception:
                    _apply_junos(
                        ssh,
                        config_blob,
                        lab_name=lab_name,
                        node_name=short_name,
                        username=username,
                        password=password,
                    )
            success += 1
            logger.info("reconfigure success node=%s", short_name)
            results.append({"node": short_name, "ok": True})
        except (socket.error, paramiko.SSHException, TimeoutError, RuntimeError, ValueError) as exc:
            logger.error("reconfigure failed node=%s error=%s", short_name, exc)
            results.append({"node": short_name, "ok": False, "error": str(exc)})
        finally:
            if ssh is not None:
                ssh.close()

    output = {
        "ok": success == len(actionable_routers) if actionable_routers else False,
        "lab": lab_name,
        "db_path": lab_db_dir,
        "success_count": success,
        "total": len(actionable_routers),
        "skipped_count": len(skipped_nodes),
        "skipped_nodes": skipped_nodes,
        "results": results,
    }
    logger.info(
        "reconfigure_lab done lab=%s ok=%s success=%s total=%s",
        lab_name,
        output["ok"],
        success,
        len(actionable_routers),
    )
    return output


def export_lab_configs_zip(lab_name: str) -> tuple[str, bytes]:
    logger.info("export_lab start lab=%s", lab_name)
    details = get_lab_details(lab_name)
    if not details:
        raise RuntimeError("Lab not found")

    routers = details.get("routers", [])
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_name = f"{lab_name}-configs-{timestamp}.zip"

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        report_lines = []

        for router in routers:
            container_name = str(router.get("name") or "")
            short_name = _short_node_name(container_name, lab_name)
            if _is_linux_srv_node(router, short_name):
                logger.info("export skip node=%s reason=linux_srv", short_name)
                report_lines.append(f"[SKIP] {short_name}: linux server")
                continue
            family = SUPPORTED_FAMILIES.get(str(router.get("kind") or "").lower())
            mgmt_ip = str(router.get("mgmt_ipv4") or "").split("/")[0]

            if not container_name or not family or not mgmt_ip:
                logger.warning("export skip node=%s reason=missing_info", short_name)
                report_lines.append(f"[FAIL] {short_name}: missing info/model")
                continue

            username, password = _credentials_for_node(short_name, family)
            ssh = None
            try:
                ssh = _ssh_connect_with_retry(mgmt_ip, username, password, family)
                config_text = _capture_running_config(ssh, family)
                archive.writestr(f"{lab_name}/{short_name}.cfg", config_text)
                logger.info("export success node=%s", short_name)
                report_lines.append(f"[OK] {short_name}")
            except Exception as exc:
                logger.error("export failed node=%s error=%s", short_name, exc)
                report_lines.append(f"[FAIL] {short_name}: {exc}")
            finally:
                if ssh is not None:
                    ssh.close()

        archive.writestr(f"{lab_name}/export_report.txt", "\n".join(report_lines) + "\n")

    memory_file.seek(0)
    logger.info("export_lab done lab=%s archive=%s", lab_name, archive_name)
    return archive_name, memory_file.read()


def export_lab_topology_yaml(lab_name: str) -> tuple[str, bytes]:
    logger.info("export_lab_yaml start lab=%s", lab_name)
    topology_path = _resolve_topology_path(lab_name)
    content = topology_path.read_bytes()
    logger.info("export_lab_yaml done lab=%s path=%s", lab_name, topology_path)
    return f"{lab_name}.yaml", content


def _resolve_topology_path(lab_name: str) -> Path:
    details = get_lab_details(lab_name)
    topology_file = ""
    if details:
        topology_file = str(details.get("topology_file") or "").strip()

    if not topology_file:
        requested = str(lab_name or "").strip().lower()
        for item in get_topology_inventory():
            if not isinstance(item, dict):
                continue
            candidate_lab = str(item.get("lab_name") or "").strip().lower()
            if candidate_lab != requested:
                continue
            topology_file = str(item.get("topology_file") or "").strip()
            if topology_file:
                break

    if not topology_file:
        raise RuntimeError("Lab not found")

    candidate_paths = [Path(topology_file).expanduser()]
    if not os.path.isabs(topology_file):
        candidate_paths.append(Path.home() / topology_file)

    for candidate in candidate_paths:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise RuntimeError(f"Topology file not accessible: {topology_file}")


def _should_retry_sandbox_deploy(start_result: dict[str, Any]) -> bool:
    stderr = str(start_result.get("stderr") or "").lower()
    if not stderr:
        return False
    markers = (
        "lab has already been deployed",
        "already exist",
        "already exists",
        "already in use",
    )
    return any(marker in stderr for marker in markers)


def _list_lab_containers(lab_name: str) -> list[str]:
    prefix = f"clab-{lab_name}-"
    try:
        process = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            logger.warning("list_lab_containers failed lab=%s rc=%s stderr=%s", lab_name, process.returncode, process.stderr)
            return []
        names = [line.strip() for line in process.stdout.splitlines() if line.strip()]
        return [name for name in names if name.startswith(prefix)]
    except Exception as exc:
        logger.warning("list_lab_containers exception lab=%s err=%s", lab_name, exc)
        return []


def _wait_for_lab_teardown(lab_name: str, timeout_seconds: int = 30, poll_seconds: float = 1.0) -> dict[str, Any]:
    started_at = time.time()
    last_remaining: list[str] = []
    while time.time() - started_at < timeout_seconds:
        remaining = _list_lab_containers(lab_name)
        last_remaining = remaining
        if not remaining:
            return {
                "ok": True,
                "remaining": [],
                "waited_seconds": round(time.time() - started_at, 2),
            }
        time.sleep(poll_seconds)
    return {
        "ok": False,
        "remaining": last_remaining,
        "waited_seconds": round(time.time() - started_at, 2),
    }


def _force_remove_lab_containers(lab_name: str) -> dict[str, Any]:
    remaining = _list_lab_containers(lab_name)
    if not remaining:
        return {"ok": True, "removed": [], "note": "no_containers"}
    try:
        process = subprocess.run(
            ["docker", "rm", "-f", *remaining],
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "ok": process.returncode == 0,
            "removed": remaining,
            "stdout": process.stdout.strip(),
            "stderr": process.stderr.strip(),
            "returncode": process.returncode,
        }
    except Exception as exc:
        return {"ok": False, "removed": remaining, "error": str(exc)}


def validate_sandbox_topology_yaml(
    lab_name: str,
    yaml_text: str,
    expected_management_subnet: str | None = None,
) -> dict[str, Any]:
    """Validate sandbox YAML content against basic containerlab constraints.

    Returns: {ok, errors, warnings, node_count, link_count}
    """
    errors: list[str] = []
    warnings: list[str] = []

    source = str(yaml_text or "")
    if not source.strip():
        return {"ok": False, "errors": ["Le YAML est vide"], "warnings": [], "node_count": 0, "link_count": 0}

    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        return {"ok": False, "errors": [f"Syntaxe YAML invalide: {exc}"], "warnings": [], "node_count": 0, "link_count": 0}

    if not isinstance(parsed, dict):
        return {"ok": False, "errors": ["La racine YAML doit être un objet"], "warnings": [], "node_count": 0, "link_count": 0}

    topology = parsed.get("topology")
    if not isinstance(topology, dict):
        return {"ok": False, "errors": ["Champ 'topology' manquant ou invalide"], "warnings": [], "node_count": 0, "link_count": 0}

    nodes = topology.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return {"ok": False, "errors": ["'topology.nodes' doit être un dictionnaire non vide"], "warnings": [], "node_count": 0, "link_count": 0}

    mgmt = parsed.get("mgmt")
    mgmt_network: ipaddress.IPv4Network | None = None
    expected_management_network: ipaddress.IPv4Network | None = None

    expected_subnet_value = str(expected_management_subnet or "").strip()
    if expected_subnet_value:
        try:
            expected_management_network = ipaddress.ip_network(expected_subnet_value, strict=False)
            if expected_management_network.version != 4:
                errors.append("Le management_subnet de la VM doit être IPv4")
                expected_management_network = None
        except ValueError:
            errors.append(f"management_subnet VM invalide: '{expected_subnet_value}'")

    if not isinstance(mgmt, dict):
        errors.append("Champ 'mgmt' manquant ou invalide")
    else:
        subnet_value = str(mgmt.get("ipv4-subnet") or mgmt.get("ipv4_subnet") or "").strip()
        if not subnet_value:
            errors.append("Champ 'mgmt.ipv4-subnet' manquant")
        else:
            try:
                mgmt_network = ipaddress.ip_network(subnet_value, strict=False)
                if mgmt_network.version != 4:
                    errors.append("Le subnet management doit être IPv4")
                    mgmt_network = None
                elif mgmt_network.prefixlen != 24:
                    errors.append(
                        f"Le subnet management doit être en /24 (reçu: /{mgmt_network.prefixlen})"
                    )
                elif expected_management_network is not None and not mgmt_network.subnet_of(expected_management_network):
                    errors.append(
                        "Le subnet management du YAML doit appartenir au management_subnet de la VM: "
                        f"attendu dans '{expected_management_network}', reçu '{mgmt_network}'"
                    )
            except ValueError:
                errors.append(f"Subnet management invalide: '{subnet_value}'")

    mgmt_ip_to_node: dict[str, str] = {}
    node_kind_map: dict[str, str] = {}  # node_name -> kind (for per-kind link validation)

    for node_name, node_data in nodes.items():
        if not isinstance(node_name, str) or not node_name.strip():
            errors.append("Un nom de noeud est invalide")
            continue
        if not isinstance(node_data, dict):
            errors.append(f"Noeud '{node_name}': structure invalide")
            continue
        kind = str(node_data.get("kind") or "").strip().lower()
        if not kind:
            errors.append(f"Noeud '{node_name}': champ 'kind' manquant")
        elif not re.match(r"^[a-z0-9_-]+$", kind):
            errors.append(f"Noeud '{node_name}': kind '{kind}' invalide")
        elif kind not in SUPPORTED_FAMILIES and kind not in _SKIP_KINDS:
            warnings.append(f"Noeud '{node_name}': kind '{kind}' non reconnu — la topologie sera déployée mais reconfigure/export ne seront pas disponibles pour ce nœud")
        if kind:
            node_kind_map[node_name] = kind

        if kind in {"bridge", "ovs-bridge", "ovs_bridge"}:
            continue

        mgmt_ipv4_raw = str(node_data.get("mgmt-ipv4") or node_data.get("mgmt_ipv4") or "").strip()
        if not mgmt_ipv4_raw:
            errors.append(f"Noeud '{node_name}': champ 'mgmt-ipv4' manquant")
            continue

        mgmt_ip: ipaddress.IPv4Address | None = None
        try:
            if "/" in mgmt_ipv4_raw:
                mgmt_ip = ipaddress.ip_interface(mgmt_ipv4_raw).ip
            else:
                mgmt_ip = ipaddress.ip_address(mgmt_ipv4_raw)
            if mgmt_ip.version != 4:
                errors.append(f"Noeud '{node_name}': mgmt-ipv4 '{mgmt_ipv4_raw}' n'est pas IPv4")
                mgmt_ip = None
        except ValueError:
            errors.append(f"Noeud '{node_name}': mgmt-ipv4 '{mgmt_ipv4_raw}' invalide")

        if mgmt_ip is None:
            continue

        mgmt_ip_key = str(mgmt_ip)
        if mgmt_ip_key in mgmt_ip_to_node:
            errors.append(
                f"mgmt-ipv4 en double: '{mgmt_ip_key}' utilisé par '{mgmt_ip_to_node[mgmt_ip_key]}' et '{node_name}'"
            )
        else:
            mgmt_ip_to_node[mgmt_ip_key] = node_name

        if mgmt_network is not None and mgmt_ip not in mgmt_network:
            errors.append(
                f"Noeud '{node_name}': mgmt-ipv4 '{mgmt_ip_key}' hors subnet management '{mgmt_network}'"
            )
        if expected_management_network is not None and mgmt_ip not in expected_management_network:
            errors.append(
                "Noeud "
                f"'{node_name}': mgmt-ipv4 '{mgmt_ip_key}' hors management_subnet VM '{expected_management_network}'"
            )

    links = topology.get("links", [])
    if links is None:
        links = []
    if not isinstance(links, list):
        errors.append("'topology.links' doit être une liste")
        links = []

    node_names = set(nodes.keys())
    external_endpoint_names = {"host"}
    endpoint_use: dict[str, int] = {}  # ep_key -> first link index
    for idx, link in enumerate(links, start=1):
        if not isinstance(link, dict):
            errors.append(f"Lien #{idx}: format invalide")
            continue
        endpoints = link.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            errors.append(f"Lien #{idx}: 'endpoints' doit contenir exactement 2 entrées")
            continue

        for ep in endpoints:
            if not isinstance(ep, str) or not ep.strip():
                errors.append(f"Lien #{idx}: endpoint '{ep}' invalide")
                continue

            ep_text = ep.strip()
            has_iface = ":" in ep_text

            if has_iface:
                node, iface = ep_text.split(":", 1)
                node = node.strip()
                iface = iface.strip()
            else:
                node = ep_text
                iface = ""

            if node not in node_names and node.lower() not in external_endpoint_names:
                errors.append(f"Lien #{idx}: noeud '{node}' non déclaré")

            if has_iface:
                if not iface or not _IFACE_RE.match(iface):
                    errors.append(f"Lien #{idx}: interface '{iface}' invalide")

                # Per-kind interface naming constraints
                if node in node_names:
                    node_kind = node_kind_map.get(node, "")
                    if node_kind in _SRL_KINDS:
                        srl_m = _SRL_ETHERNET_RE.match(iface)
                        if srl_m:
                            if int(srl_m.group(1)) < 1 or int(srl_m.group(2)) < 1:
                                errors.append(
                                    f"Lien #{idx}: nœud '{node}' (SR Linux) interface '{iface}' invalide "
                                    f"— les indices linecard et port doivent être ≥ 1 "
                                    f"(ex: 'ethernet-1/{max(1, int(srl_m.group(2)))}')"
                                )

                # Keep duplicate interface protection for declared nodes.
                # External host endpoint can legitimately appear multiple times.
                if node in node_names:
                    ep_key = f"{node}:{iface}"
                    if ep_key in endpoint_use:
                        errors.append(f"Interface en double: '{ep_key}' déjà utilisée dans le lien #{endpoint_use[ep_key]}")
                    else:
                        endpoint_use[ep_key] = idx
            else:
                # Accept plain external endpoints such as "host".
                if node.lower() not in external_endpoint_names:
                    errors.append(f"Lien #{idx}: endpoint '{ep}' invalide (attendu node:interface ou host)")

    topo_name = str(parsed.get("name") or "").strip()
    if topo_name and topo_name != lab_name:
        warnings.append(f"Le nom de topo '{topo_name}' diffère du lab '{lab_name}'")

    try:
        ref_yaml_text, ref_source = _load_sandbox_reference_yaml(lab_name)
        ref_parsed = yaml.safe_load(ref_yaml_text)
        ref_network, ref_subnet = _extract_mgmt_signature(ref_parsed)
        new_network, new_subnet = _extract_mgmt_signature(parsed)

        if ref_network and new_network != ref_network:
            errors.append(
                f"Le nom du réseau management est immuable: attendu '{ref_network}', reçu '{new_network or '(vide)'}'"
            )
        if ref_subnet and new_subnet != ref_subnet:
            errors.append(
                f"Le subnet management est immuable: attendu '{ref_subnet}', reçu '{new_subnet or '(vide)'}'"
            )
        if ref_source == "current-topology":
            warnings.append("Référence sandbox: baseline admin non initialisée (topologie courante utilisée)")
    except Exception as exc:
        warnings.append(f"Impossible de vérifier les garde-fous management: {exc}")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "node_count": len(nodes),
        "link_count": len(links),
    }


def apply_sandbox_topology_yaml(
    lab_name: str,
    yaml_text: str,
    expected_management_subnet: str | None = None,
    node_positions: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    def add_step(name: str, ok: bool, message: str, data: Any | None = None) -> None:
        item: dict[str, Any] = {"name": name, "ok": bool(ok), "message": message}
        if data is not None:
            item["data"] = data
        steps.append(item)

    lock = _sandbox_lock(lab_name)
    with lock:
        validation = validate_sandbox_topology_yaml(
            lab_name,
            yaml_text,
            expected_management_subnet=expected_management_subnet,
        )
        if not validation.get("ok"):
            add_step("validation", False, "Validation sandbox échouée", validation)
            return {
                "ok": False,
                "error": "Validation échouée",
                "validation": validation,
                "steps": steps,
            }
        add_step("validation", True, "Validation sandbox OK", validation)

        topology_path = _resolve_topology_path(lab_name)
        add_step("resolve_topology", True, f"Topologie source: {topology_path}")

        baseline_path = _ensure_sandbox_baseline(topology_path)
        add_step("ensure_baseline", True, f"Baseline admin: {baseline_path}")

        original = topology_path.read_text(encoding="utf-8")
        annotations_path, annotations_existed, annotations_original = _snapshot_annotations(topology_path)

        # Skip full destroy/redeploy when the submitted YAML is semantically identical
        # to the currently deployed topology (e.g., user opened the editor and saved
        # without any modification).
        try:
            _new_data = yaml.safe_load(yaml_text)
            _orig_data = yaml.safe_load(original)
            topology_changed = _new_data != _orig_data
        except Exception:
            topology_changed = True  # assume changed on YAML parse error

        if not topology_changed:
            add_step("change_check", True, "Topologie identique à la version déployée – redéploiement ignoré")
            return {
                "ok": True,
                "changed": False,
                "baseline_file": str(baseline_path),
                "topology_file": str(topology_path),
                "validation": validation,
                "steps": steps,
            }
        add_step("change_check", True, "Modification détectée – redéploiement planifié")

        backup_path = _sandbox_backup_path(topology_path, "sandbox-backup")
        backup_path.write_text(original, encoding="utf-8")
        add_step("backup", True, f"Backup créé: {backup_path}")
        backup_prune = _prune_sandbox_backups(topology_path, "sandbox-backup")
        add_step(
            "backup_retention",
            bool(backup_prune.get("ok")),
            "Rétention backups sandbox appliquée",
            backup_prune,
        )

        deploy_result: dict[str, Any] = {}
        rollback_result: dict[str, Any] = {}

        try:
            stop_result = stop_lab(lab_name, cleanup=True)
            deploy_result["stop"] = stop_result
            add_step(
                "destroy",
                bool(stop_result.get("ok")),
                "Destroy du lab terminé" if stop_result.get("ok") else "Destroy du lab en warning (continuation)",
                stop_result,
            )

            teardown_wait = _wait_for_lab_teardown(lab_name)
            add_step(
                "wait_destroy_quiesce",
                bool(teardown_wait.get("ok")),
                "Attente de quiescence après destroy" if teardown_wait.get("ok") else "Conteneurs encore présents après destroy",
                teardown_wait,
            )
            if not teardown_wait.get("ok"):
                forced = _force_remove_lab_containers(lab_name)
                add_step(
                    "force_remove_leftovers",
                    bool(forced.get("ok")),
                    "Nettoyage forcé des conteneurs résiduels" if forced.get("ok") else "Nettoyage forcé partiel/échoué",
                    forced,
                )
                teardown_wait_2 = _wait_for_lab_teardown(lab_name)
                add_step(
                    "wait_destroy_quiesce_retry",
                    bool(teardown_wait_2.get("ok")),
                    "Quiescence confirmée" if teardown_wait_2.get("ok") else "Conteneurs encore présents après nettoyage forcé",
                    teardown_wait_2,
                )

            topology_path.write_text(str(yaml_text), encoding="utf-8")
            add_step("write_yaml", True, f"YAML utilisateur écrit dans {topology_path}")

            annotations_result = _write_node_positions_annotations(topology_path, node_positions)
            add_step(
                "write_annotations",
                bool(annotations_result.get("ok")),
                "Annotations de position mises à jour" if annotations_result.get("ok") else "Échec écriture annotations de position",
                annotations_result,
            )
            if not annotations_result.get("ok"):
                raise RuntimeError("Impossible d'écrire les annotations de position")

            start_result = start_lab(lab_name, topology_file=str(topology_path), reconfigure=True)
            deploy_result["start"] = start_result
            add_step("deploy", bool(start_result.get("ok")), "Déploiement terminé", start_result)

            effective_start = start_result
            if not effective_start.get("ok") and _should_retry_sandbox_deploy(effective_start):
                retry_stop = stop_lab_by_topology(str(topology_path), cleanup=True)
                add_step(
                    "destroy_cleanup_retry",
                    bool(retry_stop.get("ok")),
                    "Destroy cleanup de rattrapage terminé" if retry_stop.get("ok") else "Destroy cleanup de rattrapage en warning",
                    retry_stop,
                )

                retry_wait = _wait_for_lab_teardown(lab_name)
                add_step(
                    "wait_destroy_cleanup_quiesce",
                    bool(retry_wait.get("ok")),
                    "Quiescence après destroy cleanup" if retry_wait.get("ok") else "Conteneurs encore présents après destroy cleanup",
                    retry_wait,
                )
                if not retry_wait.get("ok"):
                    forced_retry = _force_remove_lab_containers(lab_name)
                    add_step(
                        "force_remove_leftovers_retry",
                        bool(forced_retry.get("ok")),
                        "Nettoyage forcé après destroy cleanup" if forced_retry.get("ok") else "Nettoyage forcé après cleanup partiel/échoué",
                        forced_retry,
                    )

                retry_start = start_lab(lab_name, topology_file=str(topology_path), reconfigure=True)
                add_step("deploy_retry", bool(retry_start.get("ok")), "Redeploy de rattrapage terminé", retry_start)
                deploy_result["retry"] = {"stop": retry_stop, "start": retry_start, "ok": bool(retry_start.get("ok"))}
                effective_start = retry_start

            deploy_result["ok"] = bool(effective_start.get("ok"))

            if not effective_start.get("ok"):
                topology_path.write_text(original, encoding="utf-8")
                add_step("rollback_write", True, "Restauration du YAML original effectuée")
                annotations_restore = _restore_annotations_snapshot(annotations_path, annotations_existed, annotations_original)
                add_step(
                    "rollback_annotations",
                    bool(annotations_restore.get("ok")),
                    "Restauration des annotations effectuée" if annotations_restore.get("ok") else "Restauration annotations échouée",
                    annotations_restore,
                )

                rb_stop = stop_lab(lab_name, cleanup=False)
                rb_start = start_lab(lab_name, topology_file=str(topology_path), reconfigure=True)
                rollback_result = {
                    "stop": rb_stop,
                    "start": rb_start,
                    "ok": bool(rb_start.get("ok")),
                }
                add_step("rollback_deploy", bool(rb_start.get("ok")), "Rollback de déploiement exécuté", rollback_result)

                deploy_stderr = effective_start.get("stderr", "") or ""
                clab_error = ""
                m = re.search(r"\n\s*ERROR\s*\n\s*\n(.+?)(?:\n\s*\d{2}:\d{2}:\d{2}|\Z)", deploy_stderr, re.DOTALL)
                if m:
                    clab_error = " ".join(m.group(1).split())[:250]
                else:
                    for line in deploy_stderr.splitlines():
                        if re.match(r"\d{2}:\d{2}:\d{2} ERRO ", line):
                            clab_error = line.split(" ERRO ", 1)[1].strip()
                            break

                error_msg = "Déploiement sandbox échoué"
                if clab_error:
                    error_msg += f": {clab_error}"
                error_msg += (" – rollback OK, lab restauré" if rollback_result.get("ok")
                              else " – rollback incomplet, vérifier l'état du lab")

                return {
                    "ok": False,
                    "error": error_msg,
                    "deploy": deploy_result,
                    "rollback": rollback_result,
                    "backup_file": str(backup_path),
                    "baseline_file": str(baseline_path),
                    "validation": validation,
                    "steps": steps,
                }

            return {
                "ok": True,
                "changed": True,
                "backup_file": str(backup_path),
                "baseline_file": str(baseline_path),
                "topology_file": str(topology_path),
                "deploy": deploy_result,
                "validation": validation,
                "steps": steps,
            }
        except Exception as exc:
            logger.exception("apply_sandbox_topology_yaml unexpected error lab=%s", lab_name)
            try:
                topology_path.write_text(original, encoding="utf-8")
                add_step("rollback_write", True, "Restauration du YAML original après exception")
            except Exception:
                logger.exception("sandbox rollback write failed lab=%s path=%s", lab_name, topology_path)
            annotations_restore = _restore_annotations_snapshot(annotations_path, annotations_existed, annotations_original)
            add_step(
                "rollback_annotations",
                bool(annotations_restore.get("ok")),
                "Restauration des annotations après exception" if annotations_restore.get("ok") else "Restauration annotations après exception échouée",
                annotations_restore,
            )
            return {
                "ok": False,
                "error": f"Erreur sandbox inattendue ({type(exc).__name__}): {exc}",
                "backup_file": str(backup_path),
                "baseline_file": str(baseline_path),
                "validation": validation,
                "steps": steps,
            }


def update_lab_topology_yaml(
    lab_name: str,
    yaml_text: str,
    expected_management_subnet: str | None = None,
    node_positions: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Validate and save lab topology YAML without redeploying the lab."""
    validation = validate_sandbox_topology_yaml(
        lab_name,
        yaml_text,
        expected_management_subnet=expected_management_subnet,
    )
    if not validation.get("ok"):
        return {
            "ok": False,
            "error": "Validation échouée",
            "validation": validation,
        }

    topology_path = _resolve_topology_path(lab_name)
    annotations_path, annotations_existed, annotations_original = _snapshot_annotations(topology_path)
    original_yaml = topology_path.read_text(encoding="utf-8")

    topology_path.write_text(str(yaml_text), encoding="utf-8")
    annotations_result = _write_node_positions_annotations(topology_path, node_positions)
    if not annotations_result.get("ok"):
        topology_path.write_text(original_yaml, encoding="utf-8")
        _restore_annotations_snapshot(annotations_path, annotations_existed, annotations_original)
        return {
            "ok": False,
            "error": "Échec de mise à jour des annotations de position",
            "validation": validation,
            "annotations": annotations_result,
        }

    logger.info("update_lab_topology_yaml saved lab=%s path=%s", lab_name, topology_path)
    return {
        "ok": True,
        "topology_file": str(topology_path),
        "validation": validation,
        "annotations": annotations_result,
    }


def reset_sandbox_lab_to_baseline(lab_name: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    def add_step(name: str, ok: bool, message: str, data: Any | None = None) -> None:
        item: dict[str, Any] = {"name": name, "ok": bool(ok), "message": message}
        if data is not None:
            item["data"] = data
        steps.append(item)

    lock = _sandbox_lock(lab_name)
    with lock:
        topology_path = _resolve_topology_path(lab_name)
        baseline_path = _sandbox_baseline_path(topology_path)
        if not baseline_path.exists() or not baseline_path.is_file():
            add_step("load_baseline", False, "Baseline admin introuvable")
            return {
                "ok": False,
                "error": "Baseline admin introuvable pour ce lab",
                "steps": steps,
                "topology_file": str(topology_path),
            }

        current_yaml = topology_path.read_text(encoding="utf-8")
        baseline_yaml = baseline_path.read_text(encoding="utf-8")
        add_step("load_baseline", True, f"Baseline chargée: {baseline_path}")

        reset_backup_path = _sandbox_backup_path(topology_path, "sandbox-reset-backup")
        reset_backup_path.write_text(current_yaml, encoding="utf-8")
        add_step("backup_current", True, f"Backup avant RAZ: {reset_backup_path}")
        reset_backup_prune = _prune_sandbox_backups(topology_path, "sandbox-reset-backup")
        add_step(
            "backup_retention",
            bool(reset_backup_prune.get("ok")),
            "Rétention backups reset appliquée",
            reset_backup_prune,
        )

        stop_result = stop_lab(lab_name, cleanup=True)
        add_step(
            "destroy",
            bool(stop_result.get("ok")),
            "Destroy avant remise à zéro terminé" if stop_result.get("ok") else "Destroy en warning (continuation)",
            stop_result,
        )

        teardown_wait = _wait_for_lab_teardown(lab_name)
        add_step(
            "wait_destroy_quiesce",
            bool(teardown_wait.get("ok")),
            "Attente de quiescence après destroy" if teardown_wait.get("ok") else "Conteneurs encore présents après destroy",
            teardown_wait,
        )
        if not teardown_wait.get("ok"):
            forced = _force_remove_lab_containers(lab_name)
            add_step(
                "force_remove_leftovers",
                bool(forced.get("ok")),
                "Nettoyage forcé des conteneurs résiduels" if forced.get("ok") else "Nettoyage forcé partiel/échoué",
                forced,
            )
            teardown_wait_2 = _wait_for_lab_teardown(lab_name)
            add_step(
                "wait_destroy_quiesce_retry",
                bool(teardown_wait_2.get("ok")),
                "Quiescence confirmée" if teardown_wait_2.get("ok") else "Conteneurs encore présents après nettoyage forcé",
                teardown_wait_2,
            )

        topology_path.write_text(baseline_yaml, encoding="utf-8")
        add_step("restore_baseline_yaml", True, f"YAML baseline restauré dans {topology_path}")

        start_result = start_lab(lab_name, topology_file=str(topology_path), reconfigure=True)
        add_step("deploy_baseline", bool(start_result.get("ok")), "Déploiement baseline terminé", start_result)

        effective_start = start_result
        retry_deploy: dict[str, Any] | None = None
        if not effective_start.get("ok") and _should_retry_sandbox_deploy(effective_start):
            retry_stop = stop_lab_by_topology(str(topology_path), cleanup=True)
            add_step(
                "destroy_cleanup_retry",
                bool(retry_stop.get("ok")),
                "Destroy cleanup de rattrapage terminé" if retry_stop.get("ok") else "Destroy cleanup de rattrapage en warning",
                retry_stop,
            )

            retry_wait = _wait_for_lab_teardown(lab_name)
            add_step(
                "wait_destroy_cleanup_quiesce",
                bool(retry_wait.get("ok")),
                "Quiescence après destroy cleanup" if retry_wait.get("ok") else "Conteneurs encore présents après destroy cleanup",
                retry_wait,
            )
            if not retry_wait.get("ok"):
                forced_retry = _force_remove_lab_containers(lab_name)
                add_step(
                    "force_remove_leftovers_retry",
                    bool(forced_retry.get("ok")),
                    "Nettoyage forcé après destroy cleanup" if forced_retry.get("ok") else "Nettoyage forcé après cleanup partiel/échoué",
                    forced_retry,
                )

            retry_start = start_lab(lab_name, topology_file=str(topology_path), reconfigure=True)
            add_step("deploy_retry", bool(retry_start.get("ok")), "Redeploy baseline de rattrapage terminé", retry_start)
            retry_deploy = {"stop": retry_stop, "start": retry_start, "ok": bool(retry_start.get("ok"))}
            effective_start = retry_start

        if effective_start.get("ok"):
            deploy_payload = {"stop": stop_result, "start": effective_start, "ok": True}
            if retry_deploy is not None:
                deploy_payload["retry"] = retry_deploy
            return {
                "ok": True,
                "steps": steps,
                "topology_file": str(topology_path),
                "baseline_file": str(baseline_path),
                "backup_file": str(reset_backup_path),
                "deploy": deploy_payload,
            }

        # Best-effort rollback to previous current YAML if baseline deploy fails.
        topology_path.write_text(current_yaml, encoding="utf-8")
        rb_stop = stop_lab(lab_name, cleanup=False)
        rb_start = start_lab(lab_name, topology_file=str(topology_path), reconfigure=True)
        rollback_ok = bool(rb_start.get("ok"))
        rollback = {"stop": rb_stop, "start": rb_start, "ok": rollback_ok}
        add_step("rollback_previous", rollback_ok, "Rollback vers l'état précédent exécuté", rollback)

        return {
            "ok": False,
            "error": "Échec de remise à zéro sandbox vers la baseline admin",
            "steps": steps,
            "topology_file": str(topology_path),
            "baseline_file": str(baseline_path),
            "backup_file": str(reset_backup_path),
            "deploy": {
                "stop": stop_result,
                "start": effective_start,
                "ok": False,
                **({"retry": retry_deploy} if retry_deploy is not None else {}),
            },
            "rollback": rollback,
        }


def set_current_config_as_default(lab_name: str) -> dict[str, Any]:
    """Capture running configs and replace static reconfigure defaults.

    If an existing default config directory is present, create a .tgz backup before overwrite.
    """
    logger.info("set_current_config_as_default start lab=%s", lab_name)
    details = get_lab_details(lab_name)
    if not details:
        return {"ok": False, "error": "Lab not found"}

    routers = details.get("routers", [])
    topology_file = str(details.get("topology_file") or "")
    lab_db_dir = Path(_ensure_lab_db_dir(_resolve_lab_db_dir(lab_name, topology_file=topology_file)))

    skipped_nodes: list[str] = []
    actionable_routers: list[dict[str, Any]] = []
    for router in routers:
        container_name = str(router.get("name") or "")
        if not container_name:
            continue
        short_name = _short_node_name(container_name, lab_name)
        if _is_linux_srv_node(router, short_name):
            skipped_nodes.append(short_name)
            continue
        actionable_routers.append(router)

    captured_configs: dict[str, str] = {}
    failures: list[dict[str, str]] = []

    for router in actionable_routers:
        container_name = str(router.get("name") or "")
        short_name = _short_node_name(container_name, lab_name)
        family = SUPPORTED_FAMILIES.get(str(router.get("kind") or "").lower())
        mgmt_ip = str(router.get("mgmt_ipv4") or "").split("/")[0]

        if not container_name or not family or not mgmt_ip:
            failures.append({"node": short_name or "unknown", "error": "missing info/model"})
            continue

        username, password = _credentials_for_node(short_name, family)
        ssh = None
        try:
            ssh = _ssh_connect_with_retry(mgmt_ip, username, password, family)
            running_cfg = _capture_running_config(ssh, family)
            captured_configs[short_name] = _normalize_captured_config(family, running_cfg)
        except Exception as exc:
            logger.error("set_current_config_as_default failed node=%s error=%s", short_name, exc)
            failures.append({"node": short_name, "error": str(exc)})
        finally:
            if ssh is not None:
                ssh.close()

    if failures:
        return {
            "ok": False,
            "error": "Unable to capture all running configurations",
            "captured_count": len(captured_configs),
            "total": len(actionable_routers),
            "skipped_count": len(skipped_nodes),
            "skipped_nodes": skipped_nodes,
            "failures": failures,
        }

    backup_file = None
    if lab_db_dir.exists() and any(lab_db_dir.iterdir()):
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_file = lab_db_dir.parent / f"{lab_name}-default-backup-{timestamp}.tgz"
        with tarfile.open(backup_file, "w:gz") as archive:
            archive.add(lab_db_dir, arcname=lab_db_dir.name)

    # Remove old cfg files, then write the new baseline.
    for cfg_file in lab_db_dir.glob("*.cfg"):
        try:
            cfg_file.unlink()
        except OSError:
            logger.warning("set_current_config_as_default unable_to_remove file=%s", cfg_file)

    for short_name, content in captured_configs.items():
        cfg_path = lab_db_dir / f"{short_name}.cfg"
        cfg_path.write_text(content, encoding="utf-8")

    output = {
        "ok": True,
        "lab": lab_name,
        "db_path": str(lab_db_dir),
        "backup_file": str(backup_file) if backup_file else None,
        "saved_count": len(captured_configs),
        "total": len(actionable_routers),
        "skipped_count": len(skipped_nodes),
        "skipped_nodes": skipped_nodes,
    }
    logger.info(
        "set_current_config_as_default done lab=%s saved=%s total=%s backup=%s",
        lab_name,
        output["saved_count"],
        output["total"],
        output["backup_file"],
    )
    return output
