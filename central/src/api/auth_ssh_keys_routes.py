from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.services import linux_accounts
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, get_current_user


router = APIRouter()


class UpdateSshKeysRequest(BaseModel):
    keys: list[str] = []


@router.get("/api/auth/ssh-keys")
async def get_my_ssh_keys(
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        keys = linux_accounts.list_ssh_keys(user.username)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Lecture des clés SSH impossible: {exc}") from exc
    return {"ok": True, "keys": keys, "linux": linux_accounts.status()}


@router.put("/api/auth/ssh-keys")
async def update_my_ssh_keys(
    body: UpdateSshKeysRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        keys = linux_accounts.replace_ssh_keys(user.username, body.keys)
    except RuntimeError as exc:
        record_event(request=request, user=user, action="auth.ssh_keys_update", status="error")
        raise HTTPException(status_code=500, detail=f"Mise à jour des clés SSH impossible: {exc}") from exc

    record_event(
        request=request,
        user=user,
        action="auth.ssh_keys_update",
        status="ok",
        details={"keys_count": len(keys)},
    )
    return {"ok": True, "keys": keys}