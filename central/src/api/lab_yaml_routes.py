from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.config import get_settings
from src.api.agent_resource_cache import get_cached_agent_resources, update_agent_resource_cache
from src.services import lab_locks, local_dns, sandbox_labs
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, get_current_user, require_admin
from src.services.collector import download_lab_yaml, fetch_agent_state, submit_sandbox_yaml, update_lab_topology_yaml
from src.services.reservations import get_reservation, reservation_belongs_to
from src.services.sandbox_capacity import estimate_sandbox_capacity

router = APIRouter()
logger = logging.getLogger(__name__)


class SandboxYamlUpdateRequest(BaseModel):
    yaml_text: str
    apply: bool = False
    node_positions: dict[str, dict[str, float]] | None = None


class AdminTopologyYamlUpdateRequest(BaseModel):
    yaml_text: str
    node_positions: dict[str, dict[str, float]] | None = None


def _find_agent(vm_id: str) -> dict:
    agents = get_settings().get("agents", [])
    agent = next((item for item in agents if item.get("id") == vm_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail="VM not found")
    return agent


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


@router.get("/api/admin/vms/{vm_id}/labs/{lab_name}/topology-yaml")
async def admin_get_topology_yaml(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api admin_get_topology_yaml vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    require_admin(request)
    agent = _find_agent(vm_id)

    result = await download_lab_yaml(agent=agent, lab_name=lab_name)
    if not result.get("ok"):
        record_event(
            request=request, user=user, action="admin.lab.topology_yaml.get", status="error", vm_id=vm_id, lab_name=lab_name,
            details={"error": result.get("error", "Export YAML failed")},
        )
        raise HTTPException(status_code=result.get("status_code", 400), detail=result.get("error", "Export YAML failed"))

    record_event(request=request, user=user, action="admin.lab.topology_yaml.get", status="ok", vm_id=vm_id, lab_name=lab_name)
    return {"ok": True, "yaml_text": result.get("content", b"").decode("utf-8", errors="replace")}


@router.put("/api/admin/vms/{vm_id}/labs/{lab_name}/topology-yaml")
async def admin_update_topology_yaml(
    vm_id: str,
    lab_name: str,
    body: AdminTopologyYamlUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api admin_update_topology_yaml vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    require_admin(request)
    agent = _find_agent(vm_id)

    result = await update_lab_topology_yaml(
        agent=agent,
        lab_name=lab_name,
        yaml_text=body.yaml_text,
        node_positions=body.node_positions,
    )
    if not result.get("ok"):
        detail = result.get("error", "Topology YAML update failed")
        record_event(
            request=request, user=user, action="admin.lab.topology_yaml.update", status="error", vm_id=vm_id, lab_name=lab_name,
            details={"error": str(detail)},
        )
        raise HTTPException(status_code=result.get("status_code", 400), detail=detail)

    record_event(request=request, user=user, action="admin.lab.topology_yaml.update", status="ok", vm_id=vm_id, lab_name=lab_name)
    return {"ok": True, "result": result.get("data") or {}}


@router.get("/api/vms/{vm_id}/labs/{lab_name}/sandbox-yaml")
async def get_sandbox_yaml(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api get_sandbox_yaml vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_sandbox_editor(user, vm_id, lab_name)
    agent = _find_agent(vm_id)

    result = await download_lab_yaml(agent=agent, lab_name=lab_name)
    if not result.get("ok"):
        raise HTTPException(status_code=result.get("status_code", 400), detail=result.get("error", "Export YAML failed"))

    return {"ok": True, "yaml_text": result.get("content", b"").decode("utf-8", errors="replace")}


@router.post("/api/vms/{vm_id}/labs/{lab_name}/sandbox-yaml")
async def update_sandbox_yaml(
    vm_id: str,
    lab_name: str,
    body: SandboxYamlUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api update_sandbox_yaml vm=%s lab=%s user=%s apply=%s", vm_id, lab_name, user.username, body.apply)
    _require_sandbox_editor(user, vm_id, lab_name)
    if bool(body.apply):
        _require_lock_override_for_non_admin(user, vm_id, lab_name, "sandbox-apply")
    agent = _find_agent(vm_id)

    capacity_check = None
    cached_state = get_cached_agent_resources(vm_id)
    state = cached_state if isinstance(cached_state, dict) else None
    if not state:
        state = await fetch_agent_state(agent)
        if isinstance(state, dict):
            update_agent_resource_cache(state)

    if not state.get("online"):
        if body.apply:
            detail = "VM agent indisponible: impossible d'évaluer la capacité avant déploiement sandbox"
            record_event(
                request=request, user=user, action="lab.sandbox_yaml.capacity_check", status="error",
                vm_id=vm_id, lab_name=lab_name, details={"error": detail},
            )
            raise HTTPException(status_code=409, detail={"error": detail})
    else:
        capacity_check = estimate_sandbox_capacity(
            yaml_text=body.yaml_text,
            resources=state.get("resources") or {},
            guard_band=0.20,
            comparison_mode="physical",
        )
        if body.apply and not capacity_check.get("ok"):
            detail = {
                "error": "Capacité VM insuffisante pour déployer cette topologie sandbox (marge de sécurité 20%)",
                "capacity_check": capacity_check,
            }
            record_event(
                request=request, user=user, action="lab.sandbox_yaml.capacity_check", status="denied",
                vm_id=vm_id, lab_name=lab_name, details=detail,
            )
            raise HTTPException(status_code=409, detail=detail)

        record_event(
            request=request, user=user,
            action="lab.sandbox_yaml.capacity_check",
            status="ok" if capacity_check.get("ok") else "warning",
            vm_id=vm_id, lab_name=lab_name,
            details={
                "guard_band_percent": capacity_check.get("guard_band_percent"),
                "required": capacity_check.get("required"),
                "fits": capacity_check.get("fits"),
            },
        )

    result = await submit_sandbox_yaml(
        agent=agent,
        lab_name=lab_name,
        yaml_text=body.yaml_text,
        apply=bool(body.apply),
        node_positions=body.node_positions,
    )
    if not result.get("ok"):
        detail = result.get("error", "Sandbox YAML update failed")
        record_event(
            request=request, user=user, action="lab.sandbox_yaml.update", status="error",
            vm_id=vm_id, lab_name=lab_name, details={"apply": bool(body.apply), "error": str(detail)},
        )
        raise HTTPException(status_code=result.get("status_code", 400), detail=detail)

    action = "lab.sandbox_yaml.apply" if body.apply else "lab.sandbox_yaml.validate"
    if body.apply:
        result_data = result.get("data") or {}
        if result_data.get("changed", True):
            sandbox_labs.set_modified(vm_id, lab_name, True)
        try:
            await local_dns.reconcile_now_async()
        except Exception as exc:
            logger.warning("local_dns reconcile after sandbox apply failed vm=%s lab=%s err=%s", vm_id, lab_name, exc)
    record_event(request=request, user=user, action=action, status="ok", vm_id=vm_id, lab_name=lab_name)
    payload = result.get("data")
    if isinstance(payload, dict) and isinstance(capacity_check, dict):
        payload = {**payload, "capacity_check": capacity_check}
    return {"ok": True, "result": payload}
