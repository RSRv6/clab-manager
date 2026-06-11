from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.services import linux_accounts
from src.services import stats as stats_service
from src.services.audit import record_event
from src.services.auth import (
    AuthenticatedUser,
    can_group_admin_manage_user,
    create_user,
    delete_user,
    generate_reset_token,
    list_groups,
    list_users,
    require_admin,
    require_admin_or_group_admin,
    reset_user_password,
    update_user,
)


router = APIRouter()
logger = logging.getLogger(__name__)


class CreateUserRequest(BaseModel):
    username: str
    full_name: str
    email: str
    password: str
    role: str
    group_name: str | None = None


class UpdateUserRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    role: str | None = None
    active: bool | None = None
    group_name: str | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str


def _filter_users_for_group_admin(items: list[dict], actor: AuthenticatedUser) -> list[dict]:
    actor_groups = {str(name or "").strip().lower() for name in actor.groups}
    if not actor_groups:
        return []
    filtered = []
    for item in items:
        groups = {str(name or "").strip().lower() for name in item.get("groups") or []}
        if groups & actor_groups:
            filtered.append(item)
    return filtered


@router.get("/api/admin/users")
async def admin_list_users(user: AuthenticatedUser = Depends(require_admin_or_group_admin)):
    logger.info("api admin_list_users by=%s", user.username)
    users = list_users()
    if user.role == "group-admin":
        users = _filter_users_for_group_admin(users, user)
    return {"ok": True, "items": users}


@router.post("/api/admin/users")
async def admin_create_user(
    body: CreateUserRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    created = None
    role_to_create = body.role
    group_name_to_create = body.group_name
    if user.role == "group-admin":
        actor_groups = list(user.groups)
        if not actor_groups:
            raise HTTPException(status_code=403, detail="Aucun groupe assigné à votre compte")
        if group_name_to_create is None:
            group_name_to_create = actor_groups[0]
        if str(group_name_to_create).strip().lower() not in {str(item).lower() for item in actor_groups}:
            raise HTTPException(status_code=403, detail="Vous ne pouvez créer que des comptes dans votre groupe")
        if str(role_to_create or "user").strip().lower() != "user":
            raise HTTPException(status_code=403, detail="Le rôle group-admin ne peut créer que des comptes utilisateur")
        role_to_create = "user"
    try:
        created = create_user(
            username=body.username,
            full_name=body.full_name,
            email=body.email,
            password=body.password,
            role=role_to_create,
            group_name=group_name_to_create,
        )
        linux_accounts.sync_create_or_update_user(
            created["username"],
            body.password,
            is_admin=created.get("role") == "admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if created is not None:
            try:
                delete_user(created["username"], actor_username=user.username)
            except Exception:
                logger.exception("rollback create_user failed username=%s", created["username"])
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.create_user",
        status="ok",
        details={"target_username": created["username"], "target_role": created["role"]},
    )
    return {"ok": True, "user": created}


@router.patch("/api/admin/users/{username}")
async def admin_update_user(
    username: str,
    body: UpdateUserRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    provided_fields = getattr(body, "model_fields_set", getattr(body, "__fields_set__", set()))
    try:
        updated = update_user(
            username=username,
            full_name=body.full_name,
            email=body.email,
            role=body.role,
            active=body.active,
            group_name=body.group_name,
            set_group_name="group_name" in provided_fields,
            actor_username=user.username,
        )
        if body.role is not None:
            linux_accounts.sync_role(username, is_admin=updated.get("role") == "admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.update_user",
        status="ok",
        details={"target_username": updated["username"]},
    )
    return {"ok": True, "user": updated}


@router.post("/api/admin/users/{username}/reset-password")
async def admin_reset_password(
    username: str,
    body: ResetPasswordRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    if user.role == "group-admin" and not can_group_admin_manage_user(user, username):
        raise HTTPException(status_code=403, detail="Vous ne pouvez réinitialiser que les comptes utilisateur de votre groupe")
    try:
        updated = reset_user_password(username, body.new_password)
        linux_accounts.sync_password(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.reset_password",
        status="ok",
        details={"target_username": updated["username"]},
    )
    return {"ok": True, "user": updated}


@router.post("/api/admin/users/{username}/generate-reset-link")
async def admin_generate_reset_link(
    username: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    if user.role == "group-admin" and not can_group_admin_manage_user(user, username):
        raise HTTPException(status_code=403, detail="Vous ne pouvez générer un lien que pour les utilisateurs de votre groupe")
    try:
        token = generate_reset_token(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    base_url = str(request.base_url).rstrip("/")
    reset_url = f"{base_url}/?reset_token={token}"

    record_event(
        request=request,
        user=user,
        action="admin.generate_reset_link",
        status="ok",
        details={"target_username": username},
    )
    return {"ok": True, "reset_url": reset_url, "expires_in_seconds": 7200}


@router.get("/api/admin/stats")
async def admin_get_stats(
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    try:
        if user.role == "group-admin":
            groups = list_groups()
            actor_groups = {
                str(group_name or "").strip().lower()
                for group_name in (user.groups or [])
                if str(group_name or "").strip()
            }

            scoped_usernames: set[str] = set()
            scoped_vm_ids: set[str] = set()
            for group in groups:
                group_name = str(group.get("name") or "").strip().lower()
                if group_name not in actor_groups:
                    continue

                for member in group.get("members") or []:
                    member_username = str(member or "").strip().lower()
                    if member_username:
                        scoped_usernames.add(member_username)

                for vm_id in group.get("vm_ids") or []:
                    vm_key = str(vm_id or "").strip()
                    if vm_key:
                        scoped_vm_ids.add(vm_key)

                for permission in group.get("lab_permissions") or []:
                    vm_key = str((permission or {}).get("vm_id") or "").strip()
                    if vm_key:
                        scoped_vm_ids.add(vm_key)

            scoped_usernames.add(str(user.username or "").strip().lower())

            data = stats_service.compute_stats(
                allowed_usernames=scoped_usernames,
                allowed_vm_ids=scoped_vm_ids,
            )
        else:
            data = stats_service.compute_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Impossible de calculer les statistiques: {exc}") from exc
    return data


@router.delete("/api/admin/users/{username}")
async def admin_delete_user(
    username: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    if user.role == "group-admin" and not can_group_admin_manage_user(user, username):
        raise HTTPException(status_code=403, detail="Vous ne pouvez supprimer que les comptes utilisateur de votre groupe")
    try:
        linux_accounts.sync_delete_user(username)
        delete_user(username, actor_username=user.username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="admin.delete_user",
        status="ok",
        details={"target_username": username},
    )
    return {"ok": True}