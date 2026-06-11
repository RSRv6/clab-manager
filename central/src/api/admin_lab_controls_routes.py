from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import get_settings
from src.services import lab_locks
from src.services import sandbox_labs
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, require_admin


router = APIRouter()


class SandboxToggleRequest(BaseModel):
    enabled: bool


class LabLockToggleRequest(BaseModel):
    locked: bool


def _find_agent(vm_id: str) -> dict:
    agents = get_settings().get("agents", [])
    agent = next((item for item in agents if item.get("id") == vm_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail="VM not found")
    return agent


@router.put("/api/admin/vms/{vm_id}/labs/{lab_name}/sandbox")
async def admin_toggle_sandbox_lab(
    vm_id: str,
    lab_name: str,
    body: SandboxToggleRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    _find_agent(vm_id)
    sandbox_labs.set_enabled(vm_id, lab_name, bool(body.enabled))
    record_event(
        request=request,
        user=user,
        action="admin.sandbox_lab.update",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"enabled": bool(body.enabled)},
    )
    return {"ok": True, "vm_id": vm_id, "lab_name": lab_name, "sandbox_enabled": bool(body.enabled)}


@router.put("/api/admin/vms/{vm_id}/labs/{lab_name}/lock")
async def admin_toggle_lab_lock(
    vm_id: str,
    lab_name: str,
    body: LabLockToggleRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    _find_agent(vm_id)
    lock_info = lab_locks.set_locked(vm_id, lab_name, bool(body.locked), actor_username=user.username)
    record_event(
        request=request,
        user=user,
        action="admin.lab_lock.update",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"locked": bool(lock_info.get("locked", False))},
    )
    return {"ok": True, "vm_id": vm_id, "lab_name": lab_name, "lab_lock": lock_info}