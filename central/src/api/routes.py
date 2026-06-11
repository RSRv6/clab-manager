from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import secrets
import string
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
import logging
from pydantic import BaseModel
from threading import Lock, Thread
import yaml

from src.config import get_settings, normalize_agent, normalize_settings
from src.services.collector import cancel_reconfigure_job, cancel_set_default_job, collect_all, download_lab_export, download_lab_yaml, fetch_agent_docker_images, fetch_agent_state, fetch_agent_topology_inventory, fetch_config_diff, fetch_reconfigure_job, fetch_set_default_job, invoke_lab_action, reset_sandbox_lab, submit_sandbox_yaml, update_lab_topology_yaml
from src.services import docker_images_cache as _docker_cache
from src.services import central_metrics
from src.services import stats as stats_service
from src.services import jump_access
from src.services import lab_locks
from src.services import local_dns
from src.services import sandbox_labs
from src.services.sandbox_capacity import estimate_sandbox_capacity
from src.services.audit import list_events, record_event
from src.services.auth import AuthenticatedUser, authenticate_user, can_group_admin_manage_user, change_password, consume_reset_token, create_group, create_user, delete_group, delete_user, filter_items_for_user, generate_reset_token, get_current_user, list_groups, list_users, require_admin, require_admin_or_group_admin, reset_user_password, update_group, update_user, user_visible_vm_ids
from src.services import linux_accounts
from src.services.reservations import (
    MAX_RESERVATION_HOURS,
    MAX_RESERVATION_TOTAL_HOURS,
    RESERVATION_EXTENSION_WINDOW_HOURS,
    MAX_SIMULTANEOUS_LAB_RESERVATIONS,
    attach_reservations,
    extend_reservation,
    get_reservation,
    list_scheduled_reservations,
    cancel_scheduled_reservation,
    update_scheduled_reservation,
    release_lab,
    reservation_belongs_to,
    reserve_lab,
)
from src.api.admin_lab_controls_routes import router as admin_lab_controls_router
from src.api.admin_groups_routes import router as admin_groups_router
from src.api.admin_users_routes import router as admin_users_router
from src.api.admin_vm_management_routes import router as admin_vm_management_router
from src.api.auth_misc_routes import router as auth_misc_router
from src.api.auth_ssh_keys_routes import router as auth_ssh_keys_router
from src.api.docs_routes import router as docs_router
from src.api.lab_requests_routes import router as lab_requests_router
from src.api.monitoring_activity_routes import router as monitoring_activity_router
from src.api.lab_runtime_routes import router as lab_runtime_router
from src.api.lab_yaml_routes import router as lab_yaml_router
from src.api.reservations_routes import router as reservations_router
from src.api.state_routes import router as state_router
from src.api.topology_builder_routes import router as topology_builder_router

router = APIRouter()
logger = logging.getLogger(__name__)
_SETTINGS_WRITE_LOCK = Lock()
_LOGIN_ATTEMPTS_LOCK = Lock()
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateVMRequest(BaseModel):
    id: str | None = None
    name: str
    ip: str
    port: int = 8081
    token: str | None = None
    base_url: str | None = None
    management_subnet: str | None = None
    ssh_user: str | None = None
    ssh_password: str | None = None
    sudo_password: str | None = None


class UpdateVMRequest(BaseModel):
    id: str | None = None
    name: str | None = None
    ip: str | None = None
    port: int | None = None
    token: str | None = None
    base_url: str | None = None
    management_subnet: str | None = None
    ssh_user: str | None = None
    ssh_password: str | None = None
    sudo_password: str | None = None


def _login_window_seconds() -> int:
    raw = os.getenv("CENTRAL_LOGIN_WINDOW_SECONDS", "300").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 300


def _login_max_attempts() -> int:
    raw = os.getenv("CENTRAL_LOGIN_MAX_ATTEMPTS", "5").strip()
    try:
        return max(3, int(raw))
    except ValueError:
        return 5


def _login_lockout_seconds() -> int:
    raw = os.getenv("CENTRAL_LOGIN_LOCKOUT_SECONDS", "900").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 900


def _login_attempt_key(username: str, request: Request) -> str:
    remote_ip = getattr(request.client, "host", "unknown") or "unknown"
    return f"{str(username or '').strip().lower()}|{remote_ip}"


def _check_login_rate_limit(username: str, request: Request) -> int | None:
    now = time.monotonic()
    key = _login_attempt_key(username, request)
    with _LOGIN_ATTEMPTS_LOCK:
        locked_until = _LOGIN_LOCKED_UNTIL.get(key)
        if locked_until and locked_until > now:
            return int(max(1, locked_until - now))
        if locked_until:
            _LOGIN_LOCKED_UNTIL.pop(key, None)
    return None


def _register_login_failure(username: str, request: Request) -> None:
    now = time.monotonic()
    key = _login_attempt_key(username, request)
    window = _login_window_seconds()
    lockout = _login_lockout_seconds()
    max_attempts = _login_max_attempts()

    with _LOGIN_ATTEMPTS_LOCK:
        attempts = [ts for ts in _LOGIN_ATTEMPTS.get(key, []) if (now - ts) <= window]
        attempts.append(now)
        _LOGIN_ATTEMPTS[key] = attempts
        if len(attempts) >= max_attempts:
            _LOGIN_LOCKED_UNTIL[key] = now + lockout
            _LOGIN_ATTEMPTS[key] = []


def _clear_login_failures(username: str, request: Request) -> None:
    key = _login_attempt_key(username, request)
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)
        _LOGIN_LOCKED_UNTIL.pop(key, None)


def _settings_path() -> str:
    return os.getenv("CENTRAL_CONFIG", "config.yaml")


def _load_settings_from_disk() -> dict:
    path = _settings_path()
    try:
        with open(path, "r", encoding="utf-8") as file:
            parsed = yaml.safe_load(file) or {}
    except FileNotFoundError:
        parsed = {}
    return normalize_settings(parsed)


def _persist_settings(settings: dict) -> None:
    path = _settings_path()
    normalized = normalize_settings(settings)
    with _SETTINGS_WRITE_LOCK:
        with open(path, "w", encoding="utf-8") as file:
            yaml.safe_dump(normalized, file, sort_keys=False, allow_unicode=True)


def _list_all_agents() -> list[dict]:
    agents = _load_settings_from_disk().get("agents", [])
    return [dict(agent) for agent in agents if isinstance(agent, dict)]


def _generate_next_vm_id(agents: list[dict]) -> str:
    taken = {
        str(item.get("id") or "").strip()
        for item in agents
        if isinstance(item, dict)
    }
    max_index = 0
    for vm_id in taken:
        match = re.fullmatch(r"vm(\d+)", vm_id)
        if match:
            max_index = max(max_index, int(match.group(1)))

    candidate_index = max_index + 1
    while True:
        candidate = f"vm{candidate_index}"
        if candidate not in taken:
            return candidate
        candidate_index += 1


def _generate_vm_token(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _normalize_vm_payload(payload: dict, existing: dict | None = None) -> dict:
    merged = dict(existing or {})
    for key, value in payload.items():
        merged[key] = value

    vm_id = str(merged.get("id") or "").strip()
    vm_name = str(merged.get("name") or "").strip()
    vm_ip = str(merged.get("ip") or "").strip()
    vm_token = str(merged.get("token") or "").strip()
    if not vm_id:
        raise ValueError("Le champ id est obligatoire")
    if not vm_name:
        raise ValueError("Le champ name est obligatoire")
    if not vm_ip:
        raise ValueError("Le champ ip est obligatoire")
    if not vm_token:
        raise ValueError("Le champ token est obligatoire")

    merged["id"] = vm_id
    merged["name"] = vm_name
    merged["ip"] = vm_ip
    merged["token"] = vm_token

    port_value = merged.get("port", 8081)
    try:
        port_value = int(port_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Le champ port doit être un entier") from exc
    if port_value < 1 or port_value > 65535:
        raise ValueError("Le port doit être compris entre 1 et 65535")
    merged["port"] = port_value

    if str(merged.get("base_url") or "").strip() == "":
        merged.pop("base_url", None)

    management_subnet_raw = str(merged.get("management_subnet") or "").strip()
    if management_subnet_raw:
        try:
            network = ipaddress.ip_network(management_subnet_raw, strict=False)
        except ValueError as exc:
            raise ValueError("Le subnet management est invalide (ex: 172.35.0.0/16)") from exc
        merged["management_subnet"] = str(network)
    else:
        merged.pop("management_subnet", None)

    ssh_user = str(merged.get("ssh_user") or "").strip()
    if ssh_user:
        merged["ssh_user"] = ssh_user
    else:
        merged.pop("ssh_user", None)

    ssh_password = str(merged.get("ssh_password") or "").strip()
    if ssh_password:
        merged["ssh_password"] = ssh_password
    else:
        merged.pop("ssh_password", None)

    sudo_password = str(merged.get("sudo_password") or "").strip()
    if sudo_password:
        merged["sudo_password"] = sudo_password
    else:
        merged.pop("sudo_password", None)

    return normalize_agent(merged)


def _netplan_file_path() -> str:
    return str(os.getenv("CENTRAL_NETPLAN_FILE", "/etc/netplan/50-cloud-init.yaml")).strip() or "/etc/netplan/50-cloud-init.yaml"


def _read_netplan_text(path: str) -> str:
    process = subprocess.run(
        ["sudo", "cat", path],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"Lecture impossible: {path}"
        raise RuntimeError(detail)
    return process.stdout


def _write_netplan_text(path: str, content: str) -> None:
    process = subprocess.run(
        ["sudo", "tee", path],
        input=str(content),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"Ecriture impossible: {path}"
        raise RuntimeError(detail)


def _load_netplan_data(path: str) -> dict:
    raw_text = _read_netplan_text(path)
    parsed = yaml.safe_load(raw_text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("Le fichier netplan est invalide")
    return parsed


def _save_netplan_data(path: str, data: dict) -> None:
    serialized = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    _write_netplan_text(path, serialized)


def _extract_netplan_subnets(data: dict) -> set[str]:
    subnets: set[str] = set()
    network = data.get("network") if isinstance(data, dict) else {}
    ethernets = network.get("ethernets") if isinstance(network, dict) else {}
    if not isinstance(ethernets, dict):
        return subnets
    for iface_data in ethernets.values():
        routes = iface_data.get("routes") if isinstance(iface_data, dict) else None
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            target = str(route.get("to") or "").strip()
            if not target or target == "default":
                continue
            try:
                subnets.add(str(ipaddress.ip_network(target, strict=False)))
            except ValueError:
                continue
    return subnets


def _ensure_management_subnet_available(
    agents: list[dict],
    subnet: str | None,
    *,
    exclude_vm_id: str | None = None,
    netplan_subnets: set[str] | None = None,
    allow_netplan_subnets: set[str] | None = None,
) -> None:
    if not subnet:
        return
    requested = ipaddress.ip_network(subnet, strict=False)
    excluded = str(exclude_vm_id or "").strip()
    for item in agents:
        vm_id = str(item.get("id") or "").strip()
        if excluded and vm_id == excluded:
            continue
        other_raw = str(item.get("management_subnet") or "").strip()
        if not other_raw:
            continue
        try:
            other = ipaddress.ip_network(other_raw, strict=False)
        except ValueError:
            continue
        if requested.overlaps(other):
            raise HTTPException(
                status_code=409,
                detail=f"Le subnet management {requested} est déjà utilisé par la VM {vm_id}",
            )

    if netplan_subnets is not None:
        allowed = allow_netplan_subnets or set()
        for existing in netplan_subnets:
            if existing in allowed:
                continue
            try:
                existing_net = ipaddress.ip_network(existing, strict=False)
            except ValueError:
                continue
            if requested.overlaps(existing_net):
                raise HTTPException(
                    status_code=409,
                    detail=f"Le subnet management {requested} est déjà présent dans le netplan ({existing_net})",
                )


def _find_netplan_routes_container(data: dict) -> list:
    network = data.setdefault("network", {})
    if not isinstance(network, dict):
        raise ValueError("Section netplan 'network' invalide")
    ethernets = network.setdefault("ethernets", {})
    if not isinstance(ethernets, dict) or not ethernets:
        raise ValueError("Aucune interface ethernet netplan trouvée")

    selected_iface = None
    for iface_name, iface_data in ethernets.items():
        if not isinstance(iface_data, dict):
            continue
        routes = iface_data.get("routes")
        if not isinstance(routes, list):
            continue
        if any(str(route.get("to") or "").strip() == "default" for route in routes if isinstance(route, dict)):
            selected_iface = iface_name
            break

    if selected_iface is None:
        selected_iface = next(iter(ethernets.keys()))

    iface_data = ethernets.setdefault(selected_iface, {})
    if not isinstance(iface_data, dict):
        raise ValueError(f"Configuration interface {selected_iface} invalide")
    routes = iface_data.setdefault("routes", [])
    if not isinstance(routes, list):
        raise ValueError(f"Routes netplan invalides sur {selected_iface}")
    return routes


def _upsert_netplan_route(data: dict, subnet: str, via_ip: str) -> None:
    routes = _find_netplan_routes_container(data)
    normalized_subnet = str(ipaddress.ip_network(subnet, strict=False))
    normalized_via = str(ipaddress.ip_address(str(via_ip).strip()))

    for route in routes:
        if not isinstance(route, dict):
            continue
        target = str(route.get("to") or "").strip()
        if not target or target == "default":
            continue
        try:
            target = str(ipaddress.ip_network(target, strict=False))
        except ValueError:
            continue
        if target == normalized_subnet:
            route["to"] = normalized_subnet
            route["via"] = normalized_via
            return

    routes.append({"to": normalized_subnet, "via": normalized_via})


def _remove_netplan_route(data: dict, subnet: str) -> None:
    routes = _find_netplan_routes_container(data)
    normalized_subnet = str(ipaddress.ip_network(subnet, strict=False))
    filtered = []
    for route in routes:
        if not isinstance(route, dict):
            filtered.append(route)
            continue
        target = str(route.get("to") or "").strip()
        if not target or target == "default":
            filtered.append(route)
            continue
        try:
            target = str(ipaddress.ip_network(target, strict=False))
        except ValueError:
            filtered.append(route)
            continue
        if target != normalized_subnet:
            filtered.append(route)
    routes[:] = filtered


def _apply_netplan() -> None:
    process = subprocess.run(
        ["sudo", "netplan", "apply"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "netplan apply échoué"
        raise RuntimeError(detail)


def _find_agent(vm_id: str) -> dict:
    agents = get_settings().get("agents", [])
    agent = next((item for item in agents if item.get("id") == vm_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail="VM not found")
    return agent


def _create_config_diff_job(vm_id: str, lab_name: str, agent: dict, router_names: list[str] | None = None) -> str:
    job_id = secrets.token_hex(16)
    requested = list(router_names or [])
    job = {
        "job_id": job_id,
        "vm_id": vm_id,
        "lab": lab_name,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "error": None,
        "requested_routers": requested,
        "requested_count": len(requested),
        "total": 0,
        "completed_count": 0,
        "success_count": 0,
        "result": None,
    }
    with _CONFIG_DIFF_JOBS_LOCK:
        _CONFIG_DIFF_JOBS[job_id] = job
        if len(_CONFIG_DIFF_JOBS) > _CONFIG_DIFF_JOBS_MAX:
            oldest_key = next(iter(_CONFIG_DIFF_JOBS))
            _CONFIG_DIFF_JOBS.pop(oldest_key, None)

    def _runner() -> None:
        try:
            result = asyncio.run(fetch_config_diff(agent=agent, lab_name=lab_name, router_names=router_names))
            if not result.get("ok"):
                with _CONFIG_DIFF_JOBS_LOCK:
                    job["status"] = "error"
                    job["error"] = result.get("error") or "Config diff failed"
                    job["finished_at"] = datetime.now(timezone.utc).isoformat()
                return

            payload = result.get("data") or {}
            rows = payload.get("results") if isinstance(payload, dict) else []
            rows = rows if isinstance(rows, list) else []
            success_count = sum(1 for item in rows if isinstance(item, dict) and item.get("ok"))
            with _CONFIG_DIFF_JOBS_LOCK:
                job["status"] = "completed"
                job["result"] = payload
                job["total"] = len(rows)
                job["completed_count"] = len(rows)
                job["success_count"] = success_count
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            logger.exception("config_diff_job runner failed vm=%s lab=%s job=%s", vm_id, lab_name, job_id, exc_info=exc)
            with _CONFIG_DIFF_JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(exc) or "Config diff failed"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()

    Thread(target=_runner, daemon=True).start()
    return job_id


def _get_config_diff_job(job_id: str) -> dict | None:
    with _CONFIG_DIFF_JOBS_LOCK:
        job = _CONFIG_DIFF_JOBS.get(job_id)
        return dict(job) if isinstance(job, dict) else None


def _require_vm_visibility(user: AuthenticatedUser, vm_id: str) -> None:
    if user.can_access_vm(vm_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a cette VM")


def _require_lab_visibility(user: AuthenticatedUser, vm_id: str, lab_name: str) -> None:
    if user.can_access_lab(vm_id, lab_name):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a ce LAB")


def _require_lab_operator(user: AuthenticatedUser, vm_id: str, lab_name: str) -> dict:
    _require_lab_visibility(user, vm_id, lab_name)
    reservation = get_reservation(vm_id, lab_name)
    if user.role == "admin":
        return reservation or {}
    if reservation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le LAB doit etre reserve avant cette action",
        )
    if not reservation_belongs_to(reservation, user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce LAB est reserve par un autre utilisateur",
        )
    return reservation


def _require_sandbox_editor(user: AuthenticatedUser, vm_id: str, lab_name: str) -> dict:
    _require_lab_visibility(user, vm_id, lab_name)
    if not sandbox_labs.is_enabled(vm_id, lab_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce LAB n'est pas activé en mode sandbox",
        )
    reservation = get_reservation(vm_id, lab_name)
    if user.role == "admin":
        return reservation or {}
    if reservation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le LAB doit être réservé avant d'éditer son YAML",
        )
    if not reservation_belongs_to(reservation, user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce LAB est réservé par un autre utilisateur",
        )
    return reservation


def _require_lock_override_for_non_admin(user: AuthenticatedUser, vm_id: str, lab_name: str, action_name: str) -> None:
    if user.role == "admin":
        return
    if not lab_locks.is_locked(vm_id, lab_name):
        return
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=f"Ce LAB est verrouille: seul un admin peut executer l'action {action_name}",
    )


@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request):
    logger.info("api login username=%s", body.username)
    retry_after = _check_login_rate_limit(body.username, request)
    if retry_after is not None:
        record_event(
            request=request,
            user=None,
            action="auth.login",
            status="throttled",
            details={"username": body.username, "retry_after_seconds": retry_after},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": "Trop de tentatives de connexion", "retry_after_seconds": retry_after},
        )

    user = authenticate_user(body.username, body.password)
    if user is None:
        _register_login_failure(body.username, request)
        record_event(
            request=request,
            user=None,
            action="auth.login",
            status="denied",
            details={"username": body.username},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Identifiants invalides")

    _clear_login_failures(body.username, request)
    request.session.clear()
    request.session["username"] = user.username
    request.session["last_activity"] = time.monotonic()
    record_event(request=request, user=user, action="auth.login", status="ok")
    return {"ok": True, "user": user.to_dict()}


@router.post("/api/admin/vms")
async def admin_create_vm(
    body: CreateVMRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    agents = _list_all_agents()
    payload = body.model_dump(exclude_none=True)

    raw_vm_id = str(payload.get("id") or "").strip()
    if not raw_vm_id:
        payload["id"] = _generate_next_vm_id(agents)
    elif any(str(item.get("id") or "") == raw_vm_id for item in agents):
        raise HTTPException(status_code=409, detail=f"La VM {raw_vm_id} existe déjà")

    raw_token = str(payload.get("token") or "").strip()
    if not raw_token:
        payload["token"] = _generate_vm_token()

    try:
        created = _normalize_vm_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    management_subnet = str(created.get("management_subnet") or "").strip()

    netplan_path = _netplan_file_path()
    netplan_backup = None
    netplan_data = None
    netplan_subnets: set[str] = set()
    if management_subnet:
        try:
            netplan_backup = _read_netplan_text(netplan_path)
            netplan_data = _load_netplan_data(netplan_path)
            netplan_subnets = _extract_netplan_subnets(netplan_data)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    _ensure_management_subnet_available(
        agents=agents,
        subnet=management_subnet or None,
        netplan_subnets=netplan_subnets,
    )

    settings = _load_settings_from_disk()
    settings["agents"] = [*agents, created]

    netplan_changed = False
    if management_subnet and netplan_data is not None:
        try:
            _upsert_netplan_route(netplan_data, management_subnet, str(created.get("ip") or ""))
            _save_netplan_data(netplan_path, netplan_data)
            _apply_netplan()
            netplan_changed = True
        except Exception as exc:
            if netplan_backup is not None:
                try:
                    _write_netplan_text(netplan_path, netplan_backup)
                    _apply_netplan()
                except Exception:
                    logger.exception("netplan rollback failed after create_vm error")
            raise HTTPException(status_code=500, detail=f"Mise à jour netplan impossible: {exc}") from exc

    try:
        _persist_settings(settings)
    except Exception as exc:
        if netplan_changed and netplan_backup is not None:
            try:
                _write_netplan_text(netplan_path, netplan_backup)
                _apply_netplan()
            except Exception:
                logger.exception("netplan rollback failed after settings persist error")
        raise HTTPException(status_code=500, detail=f"Impossible d'enregistrer la VM: {exc}") from exc

    record_event(
        request=request,
        user=user,
        action="admin.create_vm",
        status="ok",
        details={"target_vm": created.get("id")},
    )
    return {"ok": True, "item": created}


@router.patch("/api/admin/vms/{vm_id}")
async def admin_update_vm(
    vm_id: str,
    body: UpdateVMRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    agents = _list_all_agents()
    index = next((idx for idx, item in enumerate(agents) if str(item.get("id") or "") == vm_id), -1)
    if index < 0:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} introuvable")

    payload = body.model_dump(exclude_none=True)
    try:
        updated = _normalize_vm_payload(payload, existing=agents[index])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_vm_id = str(updated.get("id") or "")
    if new_vm_id != vm_id and any(str(item.get("id") or "") == new_vm_id for idx, item in enumerate(agents) if idx != index):
        raise HTTPException(status_code=409, detail=f"La VM {new_vm_id} existe déjà")

    previous = dict(agents[index])
    previous_subnet = str(previous.get("management_subnet") or "").strip()
    next_subnet = str(updated.get("management_subnet") or "").strip()

    netplan_path = _netplan_file_path()
    netplan_backup = None
    netplan_data = None
    netplan_subnets: set[str] = set()
    needs_netplan_change = bool(previous_subnet or next_subnet)
    if needs_netplan_change:
        try:
            netplan_backup = _read_netplan_text(netplan_path)
            netplan_data = _load_netplan_data(netplan_path)
            netplan_subnets = _extract_netplan_subnets(netplan_data)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    allowed_netplan = {previous_subnet} if previous_subnet else set()
    _ensure_management_subnet_available(
        agents=agents,
        subnet=next_subnet or None,
        exclude_vm_id=vm_id,
        netplan_subnets=netplan_subnets,
        allow_netplan_subnets=allowed_netplan,
    )

    agents[index] = updated
    settings = _load_settings_from_disk()
    settings["agents"] = agents

    netplan_changed = False
    if needs_netplan_change and netplan_data is not None:
        try:
            if previous_subnet and previous_subnet != next_subnet:
                _remove_netplan_route(netplan_data, previous_subnet)
            if next_subnet:
                _upsert_netplan_route(netplan_data, next_subnet, str(updated.get("ip") or ""))
            _save_netplan_data(netplan_path, netplan_data)
            _apply_netplan()
            netplan_changed = True
        except Exception as exc:
            if netplan_backup is not None:
                try:
                    _write_netplan_text(netplan_path, netplan_backup)
                    _apply_netplan()
                except Exception:
                    logger.exception("netplan rollback failed after update_vm error")
            raise HTTPException(status_code=500, detail=f"Mise à jour netplan impossible: {exc}") from exc

    try:
        _persist_settings(settings)
    except Exception as exc:
        if netplan_changed and netplan_backup is not None:
            try:
                _write_netplan_text(netplan_path, netplan_backup)
                _apply_netplan()
            except Exception:
                logger.exception("netplan rollback failed after settings persist error")
        raise HTTPException(status_code=500, detail=f"Impossible d'enregistrer la VM: {exc}") from exc

    record_event(
        request=request,
        user=user,
        action="admin.update_vm",
        status="ok",
        details={"source_vm": vm_id, "target_vm": new_vm_id},
    )
    return {"ok": True, "item": updated}


@router.delete("/api/admin/vms/{vm_id}")
async def admin_delete_vm(
    vm_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    agents = _list_all_agents()
    index = next((idx for idx, item in enumerate(agents) if str(item.get("id") or "") == vm_id), -1)
    if index < 0:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} introuvable")

    removed = agents.pop(index)
    settings = _load_settings_from_disk()
    settings["agents"] = agents

    removed_subnet = str(removed.get("management_subnet") or "").strip()
    netplan_path = _netplan_file_path()
    netplan_backup = None
    netplan_changed = False
    if removed_subnet:
        try:
            netplan_backup = _read_netplan_text(netplan_path)
            netplan_data = _load_netplan_data(netplan_path)
            _remove_netplan_route(netplan_data, removed_subnet)
            _save_netplan_data(netplan_path, netplan_data)
            _apply_netplan()
            netplan_changed = True
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            if netplan_backup is not None:
                try:
                    _write_netplan_text(netplan_path, netplan_backup)
                    _apply_netplan()
                except Exception:
                    logger.exception("netplan rollback failed after delete_vm error")
            raise HTTPException(status_code=500, detail=f"Mise à jour netplan impossible: {exc}") from exc

    try:
        _persist_settings(settings)
    except Exception as exc:
        if netplan_changed and netplan_backup is not None:
            try:
                _write_netplan_text(netplan_path, netplan_backup)
                _apply_netplan()
            except Exception:
                logger.exception("netplan rollback failed after settings persist error")
        raise HTTPException(status_code=500, detail=f"Impossible de supprimer la VM: {exc}") from exc

    record_event(
        request=request,
        user=user,
        action="admin.delete_vm",
        status="ok",
        details={"target_vm": vm_id},
    )
    return {"ok": True, "item": removed}

router.include_router(admin_lab_controls_router)
router.include_router(admin_groups_router)
router.include_router(admin_users_router)
router.include_router(admin_vm_management_router)
router.include_router(auth_misc_router)
router.include_router(auth_ssh_keys_router)
router.include_router(docs_router)
router.include_router(lab_requests_router)
router.include_router(monitoring_activity_router)
router.include_router(lab_runtime_router)
router.include_router(lab_yaml_router)
router.include_router(reservations_router)
router.include_router(state_router)
router.include_router(topology_builder_router)
