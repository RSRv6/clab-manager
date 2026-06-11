from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, create_group, delete_group, list_groups, require_admin, update_group


router = APIRouter()


class GroupLabPermissionRequest(BaseModel):
    vm_id: str
    labs: list[str]


class CreateGroupRequest(BaseModel):
    name: str
    description: str = ""
    members: list[str] = []
    vm_ids: list[str] = []
    lab_permissions: list[GroupLabPermissionRequest] = []


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    members: list[str] | None = None
    vm_ids: list[str] | None = None
    lab_permissions: list[GroupLabPermissionRequest] | None = None


@router.get("/api/admin/groups")
async def admin_list_groups(user: AuthenticatedUser = Depends(require_admin)):
    return {"ok": True, "items": list_groups()}


@router.post("/api/admin/groups")
async def admin_create_group(
    body: CreateGroupRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    try:
        created = create_group(
            name=body.name,
            description=body.description,
            members=body.members,
            vm_ids=body.vm_ids,
            lab_permissions=[item.model_dump() for item in body.lab_permissions],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.create_group",
        status="ok",
        details={"target_group": created["name"]},
    )
    return {"ok": True, "group": created}


@router.patch("/api/admin/groups/{group_name}")
async def admin_update_group(
    group_name: str,
    body: UpdateGroupRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    try:
        updated = update_group(
            group_name,
            new_name=body.name,
            description=body.description,
            members=body.members,
            vm_ids=body.vm_ids,
            lab_permissions=None if body.lab_permissions is None else [item.model_dump() for item in body.lab_permissions],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        is_not_found = str(exc) == "Groupe introuvable"
        raise HTTPException(status_code=404 if is_not_found else 409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.update_group",
        status="ok",
        details={"target_group": updated["name"], "source_group": group_name},
    )
    return {"ok": True, "group": updated}


@router.delete("/api/admin/groups/{group_name}")
async def admin_delete_group(
    group_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    try:
        delete_group(group_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.delete_group",
        status="ok",
        details={"target_group": group_name},
    )
    return {"ok": True}