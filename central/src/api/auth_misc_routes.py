from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.services import linux_accounts
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, authenticate_user, change_password, consume_reset_token, get_current_user
from src.services.reservations import (
    MAX_RESERVATION_HOURS,
    MAX_RESERVATION_TOTAL_HOURS,
    MAX_SIMULTANEOUS_LAB_RESERVATIONS,
    RESERVATION_EXTENSION_WINDOW_HOURS,
)


router = APIRouter()


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class TokenResetRequest(BaseModel):
    token: str
    new_password: str


@router.post("/api/auth/logout")
async def logout(request: Request):
    user = None
    try:
        user = get_current_user(request)
    except HTTPException:
        user = None
    request.session.clear()
    record_event(request=request, user=user, action="auth.logout", status="ok")
    return {"ok": True}


@router.get("/api/auth/me")
async def me(user: AuthenticatedUser = Depends(get_current_user)):
    return {"ok": True, "user": user.to_dict()}


@router.get("/api/config/client")
async def client_config():
    return {
        "max_reservation_hours": MAX_RESERVATION_HOURS,
        "max_reservation_total_hours": MAX_RESERVATION_TOTAL_HOURS,
        "reservation_extension_window_hours": RESERVATION_EXTENSION_WINDOW_HOURS,
        "max_simultaneous_lab_reservations": MAX_SIMULTANEOUS_LAB_RESERVATIONS,
    }


@router.post("/api/auth/change-password")
async def update_my_password(
    body: ChangePasswordRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if authenticate_user(user.username, body.current_password) is None:
        record_event(request=request, user=user, action="auth.change_password", status="denied")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Mot de passe actuel invalide")

    try:
        change_password(user.username, body.new_password)
        linux_accounts.sync_password(user.username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Sync Linux impossible: {exc}") from exc

    record_event(request=request, user=user, action="auth.change_password", status="ok")
    return {"ok": True}


@router.post("/api/auth/reset-with-token")
async def reset_password_with_token(body: TokenResetRequest, request: Request):
    try:
        username = consume_reset_token(body.token)
        change_password(username, body.new_password)
        linux_accounts.sync_password(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record_event(
        request=request,
        user=None,
        action="auth.reset_password_with_token",
        status="ok",
        details={"target_username": username},
    )
    return {"ok": True, "username": username}