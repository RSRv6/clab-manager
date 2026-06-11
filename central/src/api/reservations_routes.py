from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.config import get_settings
from src.services import jump_access, lab_locks, local_dns, sandbox_labs
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, can_group_admin_manage_user, get_current_user
from src.services.collector import reset_sandbox_lab
from src.services.reservations import (
    MAX_RESERVATION_HOURS,
    MAX_RESERVATION_TOTAL_HOURS,
    RESERVATION_EXTENSION_WINDOW_HOURS,
    MAX_SIMULTANEOUS_LAB_RESERVATIONS,
    cancel_scheduled_reservation,
    extend_reservation,
    get_reservation,
    list_scheduled_reservations,
    release_lab,
    reservation_belongs_to,
    reserve_lab,
    update_scheduled_reservation,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class LabReservationRequest(BaseModel):
    duration_hours: float
    starts_at: str | None = None


class LabReservationExtendRequest(BaseModel):
    additional_hours: float


class LabScheduledReservationUpdateRequest(BaseModel):
    starts_at: str | None = None
    duration_hours: float | None = None


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


def _require_lock_override_for_non_admin(user: AuthenticatedUser, vm_id: str, lab_name: str, action_name: str) -> None:
    if user.role == "admin":
        return
    if not lab_locks.is_locked(vm_id, lab_name):
        return
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=f"Ce LAB est verrouille: seul un admin peut executer l'action {action_name}",
    )


@router.get("/api/vms/{vm_id}/labs/{lab_name}/reservation")
async def get_lab_reservation(vm_id: str, lab_name: str, user: AuthenticatedUser = Depends(get_current_user)):
    logger.info("api get_reservation vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    return {
        "ok": True,
        "reservation": get_reservation(vm_id, lab_name),
        "scheduled_reservations": list_scheduled_reservations(vm_id, lab_name),
        "max_duration_hours": MAX_RESERVATION_HOURS,
        "max_total_duration_hours": MAX_RESERVATION_TOTAL_HOURS,
        "extension_window_hours": RESERVATION_EXTENSION_WINDOW_HOURS,
        "max_simultaneous_labs": MAX_SIMULTANEOUS_LAB_RESERVATIONS,
    }


@router.get("/api/vms/{vm_id}/labs/{lab_name}/reservation/scheduled")
async def get_lab_scheduled_reservations(vm_id: str, lab_name: str, user: AuthenticatedUser = Depends(get_current_user)):
    logger.info("api get_scheduled_reservations vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    return {
        "ok": True,
        "items": list_scheduled_reservations(vm_id, lab_name),
        "max_duration_hours": MAX_RESERVATION_HOURS,
        "max_simultaneous_labs": MAX_SIMULTANEOUS_LAB_RESERVATIONS,
    }


@router.delete("/api/vms/{vm_id}/labs/{lab_name}/reservation/scheduled/{reservation_id}")
async def cancel_lab_scheduled_reservation(
    vm_id: str,
    lab_name: str,
    reservation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api cancel_scheduled_reservation vm=%s lab=%s reservation_id=%s user=%s", vm_id, lab_name, reservation_id, user.username)
    _require_lab_visibility(user, vm_id, lab_name)

    scheduled = list_scheduled_reservations(vm_id, lab_name)
    target = next((item for item in scheduled if str(item.get("reservation_id") or "") == reservation_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Réservation planifiée introuvable")

    target_owner = str(target.get("owner_username") or "")
    if user.role != "admin" and str(user.username or "").lower() != target_owner.lower():
        record_event(
            request=request,
            user=user,
            action="lab.cancel_scheduled_reservation",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"reservation_id": reservation_id},
        )
        raise HTTPException(status_code=403, detail="Seul le propriétaire ou un admin peut annuler cette réservation planifiée")

    removed = cancel_scheduled_reservation(vm_id, lab_name, reservation_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="Réservation planifiée introuvable")

    record_event(
        request=request,
        user=user,
        action="lab.cancel_scheduled_reservation",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"reservation_id": reservation_id},
    )
    return {"ok": True, "removed": removed}


@router.patch("/api/vms/{vm_id}/labs/{lab_name}/reservation/scheduled/{reservation_id}")
async def update_lab_scheduled_reservation_route(
    vm_id: str,
    lab_name: str,
    reservation_id: str,
    body: LabScheduledReservationUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api update_scheduled_reservation vm=%s lab=%s reservation_id=%s user=%s", vm_id, lab_name, reservation_id, user.username)
    _require_lab_visibility(user, vm_id, lab_name)

    if body.starts_at is None and body.duration_hours is None:
        raise HTTPException(status_code=400, detail="Aucune modification demandée")

    scheduled = list_scheduled_reservations(vm_id, lab_name)
    target = next((item for item in scheduled if str(item.get("reservation_id") or "") == reservation_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Réservation planifiée introuvable")

    target_owner = str(target.get("owner_username") or "")
    if user.role != "admin" and str(user.username or "").lower() != target_owner.lower():
        record_event(
            request=request,
            user=user,
            action="lab.update_scheduled_reservation",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"reservation_id": reservation_id},
        )
        raise HTTPException(status_code=403, detail="Seul le propriétaire ou un admin peut modifier cette réservation planifiée")

    try:
        updated = update_scheduled_reservation(
            vm_id,
            lab_name,
            reservation_id,
            starts_at=body.starts_at,
            duration_hours=body.duration_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if updated is None:
        raise HTTPException(status_code=404, detail="Réservation planifiée introuvable")

    record_event(
        request=request,
        user=user,
        action="lab.update_scheduled_reservation",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"reservation_id": reservation_id, "starts_at": updated.get("starts_at"), "duration_hours": updated.get("duration_hours")},
    )
    return {"ok": True, "reservation": updated}


@router.post("/api/vms/{vm_id}/labs/{lab_name}/reservation")
async def create_lab_reservation(
    vm_id: str,
    lab_name: str,
    body: LabReservationRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api reserve vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    try:
        reservation = reserve_lab(
            vm_id=vm_id,
            lab_name=lab_name,
            reserved_by=user.full_name,
            email=user.email,
            duration_hours=body.duration_hours,
            owner_username=user.username,
            starts_at=body.starts_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        record_event(
            request=request,
            user=user,
            action="lab.reserve",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="lab.reserve",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"starts_at": reservation.get("starts_at"), "expires_at": reservation.get("expires_at")},
    )
    try:
        jump_access.reconcile_now()
    except Exception as exc:
        logger.warning("jump_access reconcile after reserve failed vm=%s lab=%s err=%s", vm_id, lab_name, exc)
    try:
        await local_dns.reconcile_now_async()
    except Exception as exc:
        logger.warning("local_dns reconcile after reserve failed vm=%s lab=%s err=%s", vm_id, lab_name, exc)
    return {
        "ok": True,
        "reservation": reservation,
        "max_simultaneous_labs": MAX_SIMULTANEOUS_LAB_RESERVATIONS,
        "max_duration_hours": MAX_RESERVATION_HOURS,
        "max_total_duration_hours": MAX_RESERVATION_TOTAL_HOURS,
        "extension_window_hours": RESERVATION_EXTENSION_WINDOW_HOURS,
    }


@router.post("/api/vms/{vm_id}/labs/{lab_name}/reservation/extend")
async def extend_lab_reservation(
    vm_id: str,
    lab_name: str,
    body: LabReservationExtendRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api extend_reservation vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    reservation = get_reservation(vm_id, lab_name)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Aucune réservation active")

    if user.role != "admin" and not reservation_belongs_to(reservation, user.username):
        record_event(
            request=request,
            user=user,
            action="lab.extend_reservation",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
        )
        raise HTTPException(status_code=403, detail="Seul le proprietaire ou un admin peut etendre cette reservation")

    try:
        updated = extend_reservation(vm_id, lab_name, body.additional_hours)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        record_event(
            request=request,
            user=user,
            action="lab.extend_reservation",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="lab.extend_reservation",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"additional_hours": body.additional_hours, "new_expires_at": updated.get("expires_at")},
    )
    return {
        "ok": True,
        "reservation": updated,
        "max_total_duration_hours": MAX_RESERVATION_TOTAL_HOURS,
        "extension_window_hours": RESERVATION_EXTENSION_WINDOW_HOURS,
    }


@router.delete("/api/vms/{vm_id}/labs/{lab_name}/reservation")
async def delete_lab_reservation(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api release_reservation vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    reservation = get_reservation(vm_id, lab_name)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Aucune réservation active")
    if user.role == "group-admin":
        owner_username = str(reservation.get("owner_username") or "")
        if not reservation_belongs_to(reservation, user.username) and not can_group_admin_manage_user(user, owner_username):
            record_event(
                request=request,
                user=user,
                action="lab.release_reservation",
                status="denied",
                vm_id=vm_id,
                lab_name=lab_name,
            )
            raise HTTPException(status_code=403, detail="Vous pouvez libérer uniquement les réservations de votre groupe")
    elif user.role != "admin" and not reservation_belongs_to(reservation, user.username):
        record_event(
            request=request,
            user=user,
            action="lab.release_reservation",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
        )
        raise HTTPException(status_code=403, detail="Seul le proprietaire ou un admin peut liberer cette reservation")

    reset_payload = None
    sandbox_enabled = sandbox_labs.is_enabled(vm_id, lab_name)
    sandbox_modified = sandbox_labs.is_modified(vm_id, lab_name)
    if sandbox_enabled and sandbox_modified:
        _require_lock_override_for_non_admin(user, vm_id, lab_name, "sandbox-reset")
        agent = _find_agent(vm_id)
        reset_result = await reset_sandbox_lab(agent=agent, lab_name=lab_name)
        if not reset_result.get("ok"):
            detail = reset_result.get("error", "Remise à zéro sandbox échouée")
            record_event(
                request=request,
                user=user,
                action="lab.release_reservation",
                status="error",
                vm_id=vm_id,
                lab_name=lab_name,
                details={"error": str(detail), "sandbox_reset": "failed"},
            )
            raise HTTPException(status_code=reset_result.get("status_code", 409), detail=detail)
        reset_payload = reset_result.get("data")
        sandbox_labs.set_modified(vm_id, lab_name, False)

    removed = release_lab(vm_id, lab_name)
    if not removed:
        raise HTTPException(status_code=404, detail="Aucune réservation active")
    record_event(
        request=request,
        user=user,
        action="lab.release_reservation",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={
            "sandbox_reset": "ok" if reset_payload is not None else "skipped",
            "sandbox_enabled": sandbox_enabled,
            "sandbox_yaml_modified": sandbox_modified,
        },
    )
    try:
        jump_access.reconcile_now()
    except Exception as exc:
        logger.warning("jump_access reconcile after release failed vm=%s lab=%s err=%s", vm_id, lab_name, exc)
    try:
        await local_dns.reconcile_now_async()
    except Exception as exc:
        logger.warning("local_dns reconcile after release failed vm=%s lab=%s err=%s", vm_id, lab_name, exc)
    return {"ok": True, "sandbox_reset": reset_payload}
