from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
import yaml

from src.config import normalize_settings
from src.services import provisioning as _provisioning
from src.services.audit import list_events, record_event
from src.services.auth import AuthenticatedUser, require_admin, require_admin_or_group_admin, user_visible_vm_ids
from src.services.collector import fetch_agent_topology_inventory

router = APIRouter()
logger = logging.getLogger(__name__)


class ProvisionVMRequest(BaseModel):
    ssh_user: str
    ssh_password: str | None = None
    sudo_password: str | None = None
    repo_ssh_password: str | None = None
    app_dir: str | None = None
    branch: str = "main"
    repo_url: str


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


def _list_all_agents() -> list[dict]:
    agents = _load_settings_from_disk().get("agents", [])
    return [dict(agent) for agent in agents if isinstance(agent, dict)]


def _require_vm_visibility(user: AuthenticatedUser, vm_id: str) -> None:
    if user.can_access_vm(vm_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a cette VM")


@router.get("/api/admin/vms")
async def admin_list_vms(user: AuthenticatedUser = Depends(require_admin_or_group_admin)):
    logger.info("api admin_list_vms by=%s", user.username)
    visible_vm_ids = user_visible_vm_ids(user)
    is_admin = str(user.role or "").strip().lower() == "admin"
    items = []
    for vm in _list_all_agents():
        vm_id = str(vm.get("id") or "").strip()
        if vm_id not in visible_vm_ids:
            continue
        copy = dict(vm)
        if not is_admin:
            copy.pop("token", None)
        copy.pop("ssh_password", None)
        copy.pop("sudo_password", None)
        items.append(copy)
    return {"ok": True, "items": items}


@router.get("/api/admin/topologies/inventory")
async def admin_topology_inventory(user: AuthenticatedUser = Depends(require_admin)):
    logger.info("api admin_topology_inventory by=%s", user.username)
    agents = _list_all_agents()
    if not agents:
        return {"ok": True, "items": []}

    results = await asyncio.gather(
        *(fetch_agent_topology_inventory(agent) for agent in agents),
        return_exceptions=True,
    )

    items: list[dict] = []
    for index, result in enumerate(results):
        agent = agents[index] if index < len(agents) else {}
        vm_id = str(agent.get("id") or "")
        vm_name = str(agent.get("name") or vm_id)
        if isinstance(result, Exception):
            items.append(
                {
                    "vm_id": vm_id,
                    "vm_name": vm_name,
                    "error": f"Erreur inventaire: {type(result).__name__}",
                    "topologies": [],
                }
            )
            continue

        payload = result if isinstance(result, dict) else {}
        if not payload.get("ok"):
            items.append(
                {
                    "vm_id": vm_id,
                    "vm_name": vm_name,
                    "error": str(payload.get("error") or "Inventaire indisponible"),
                    "topologies": [],
                }
            )
            continue

        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
        topologies = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            topologies.append(
                {
                    "lab_name": str(entry.get("lab_name") or ""),
                    "topology_file": str(entry.get("topology_file") or ""),
                    "file_name": str(entry.get("file_name") or ""),
                    "running": bool(entry.get("running")),
                }
            )

        items.append(
            {
                "vm_id": vm_id,
                "vm_name": vm_name,
                "error": None,
                "topologies": topologies,
            }
        )

    return {"ok": True, "items": items}


@router.get("/api/admin/vms/{vm_id}/logs")
async def admin_vm_logs(
    vm_id: str,
    limit: int = 200,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    logger.info("api admin_vm_logs by=%s vm_id=%s limit=%s", user.username, vm_id, limit)
    _require_vm_visibility(user, vm_id)
    return {"ok": True, "items": list_events(limit, vm_id=vm_id)}


@router.get("/api/admin/provision-defaults")
async def admin_provision_defaults(user: AuthenticatedUser = Depends(require_admin)):
    return {
        "ok": True,
        "repo_url": _provisioning.detect_default_repo_url(),
        "branch": "main",
    }


@router.post("/api/admin/vms/{vm_id}/provision")
async def admin_provision_vm(
    vm_id: str,
    body: ProvisionVMRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    agents = _list_all_agents()
    vm = next((item for item in agents if str(item.get("id") or "") == vm_id), None)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} introuvable")

    app_dir = (
        str(body.app_dir).strip()
        if body.app_dir and str(body.app_dir).strip()
        else f"/home/{body.ssh_user}/virtual-labs-management"
    )

    try:
        job_id = _provisioning.start_provision_job(
            vm=dict(vm),
            ssh_user=body.ssh_user,
            ssh_password=body.ssh_password or None,
            sudo_password=body.sudo_password or None,
            repo_ssh_password=body.repo_ssh_password or None,
            app_dir=app_dir,
            branch=body.branch,
            repo_url=body.repo_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.provision_vm",
        status="ok",
        details={"target_vm": vm_id, "ssh_user": body.ssh_user, "job_id": job_id},
    )
    return {"ok": True, "job_id": job_id}


@router.get("/api/admin/provision-jobs")
async def admin_list_provision_jobs(
    vm_id: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_admin),
):
    return {"ok": True, "items": _provisioning.list_jobs(vm_id=vm_id)}


@router.get("/api/admin/provision-jobs/{job_id}")
async def admin_get_provision_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_admin),
):
    job = _provisioning.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return {"ok": True, "item": _provisioning.job_to_dict(job)}