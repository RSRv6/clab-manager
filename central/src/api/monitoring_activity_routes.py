from __future__ import annotations

import os
from typing import Optional

import logging
from fastapi import APIRouter, Depends, HTTPException, status

from src.services import central_metrics
from src.services import state_snapshot
from src.services.audit import list_events
from src.services.auth import AuthenticatedUser, get_current_user, require_admin, require_admin_or_group_admin


router = APIRouter()
logger = logging.getLogger(__name__)


def _require_vm_visibility(user: AuthenticatedUser, vm_id: str) -> None:
    if user.can_access_vm(vm_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a cette VM")


@router.get("/api/admin/audit")
async def admin_audit(
    limit: int = 200,
    username: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_admin),
):
    logger.info("api admin_audit by=%s limit=%s username=%s", user.username, limit, username)
    return {"ok": True, "items": list_events(limit, username=username)}


@router.get("/api/activity/history")
async def user_activity_history(
    limit: int = 300,
    action_prefix: Optional[str] = "lab.",
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api user_activity_history by=%s limit=%s action_prefix=%s", user.username, limit, action_prefix)
    items = list_events(limit=limit, username=user.username, action_prefix=action_prefix)
    return {"ok": True, "items": items}


@router.get("/api/admin/central/resources")
async def admin_central_resources(
    hours: int = 24,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    logger.info("api admin_central_resources by=%s hours=%s", user.username, hours)
    return central_metrics.get_snapshot_with_history(hours=hours)


@router.get("/api/admin/vms/{vm_id}/resources")
async def admin_vm_resources(
    vm_id: str,
    hours: int = 24,
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    logger.info("api admin_vm_resources by=%s vm_id=%s hours=%s", user.username, vm_id, hours)
    _require_vm_visibility(user, vm_id)
    return central_metrics.get_vm_snapshot_with_history(vm_id=vm_id, hours=hours)


@router.get("/api/admin/vms/{vm_id}/resources/aggregated")
async def admin_vm_resources_aggregated(
    vm_id: str,
    hours: int = 24,
    bucket: str = "auto",
    user: AuthenticatedUser = Depends(require_admin_or_group_admin),
):
    logger.info(
        "api admin_vm_resources_aggregated by=%s vm_id=%s hours=%s bucket=%s",
        user.username,
        vm_id,
        hours,
        bucket,
    )
    _require_vm_visibility(user, vm_id)
    return central_metrics.get_vm_snapshot_with_aggregated_history(vm_id=vm_id, hours=hours, bucket=bucket)


@router.get("/api/admin/snapshot-status")
async def admin_snapshot_status(user: AuthenticatedUser = Depends(require_admin)):
    logger.info("api admin_snapshot_status by=%s", user.username)

    status_payload = state_snapshot.get_snapshot_status()

    def _read_int_env(name: str, default: int) -> int:
        raw = os.getenv(name, str(default)).strip()
        try:
            return int(raw)
        except ValueError:
            return default

    return {
        "ok": True,
        "snapshot": status_payload,
        "settings": {
            "snapshot_interval_seconds": _read_int_env("CENTRAL_STATE_SNAPSHOT_INTERVAL", 3),
            "snapshot_max_age_seconds": _read_int_env("CENTRAL_STATE_SNAPSHOT_MAX_AGE", 6),
            "offline_confirmation_failures": _read_int_env("CENTRAL_VM_OFFLINE_CONFIRMATION_FAILURES", 4),
            "online_confirmation_successes": _read_int_env("CENTRAL_VM_ONLINE_CONFIRMATION_SUCCESSES", 2),
        },
    }