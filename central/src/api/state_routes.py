from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.config import get_settings
from src.api.agent_resource_cache import update_agent_resource_cache
from src.services import lab_locks, sandbox_labs, state_snapshot
from src.services.auth import AuthenticatedUser, filter_items_for_user, get_current_user, user_visible_vm_ids
from src.services.collector import fetch_agent_state
from src.services.reservations import attach_reservations

router = APIRouter()
logger = logging.getLogger(__name__)


def _find_agent(vm_id: str) -> dict:
    agents = get_settings().get("agents", [])
    agent = next((item for item in agents if item.get("id") == vm_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail="VM not found")
    return agent


def _require_vm_visibility(user: AuthenticatedUser, vm_id: str) -> None:
    if user.can_access_vm(vm_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a cette VM")


@router.get("/api/vms")
def get_vms(user: AuthenticatedUser = Depends(get_current_user)):
    logger.info("api get_vms user=%s", user.username)
    agents = get_settings().get("agents", [])
    visible_vm_ids = user_visible_vm_ids(user)
    return {
        "vms": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "ip": a.get("ip"),
                "port": a.get("port"),
                "base_url": a.get("base_url"),
            }
            for a in agents
            if str(a.get("id") or "") in visible_vm_ids
        ]
    }


@router.get("/api/state")
async def get_state(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    logger.info("api get_state user=%s", user.username)
    items = await state_snapshot.get_or_refresh_snapshot_async()
    for item in items:
        if isinstance(item, dict):
            update_agent_resource_cache(item)
    visible = filter_items_for_user(items, user)
    with_reservations = attach_reservations(visible)
    with_sandbox = sandbox_labs.attach_flags(with_reservations)
    with_locks = lab_locks.attach_flags(with_sandbox)
    return {"items": with_locks}


@router.get("/api/vms/{vm_id}/state")
async def get_vm_state(vm_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    logger.info("api get_vm_state vm=%s user=%s", vm_id, user.username)
    _require_vm_visibility(user, vm_id)
    agent = _find_agent(vm_id)
    item = await fetch_agent_state(agent)
    update_agent_resource_cache(item)
    filtered_items = filter_items_for_user([item], user)
    if not filtered_items:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acces refuse a cette VM")
    filtered_item = filtered_items[0]
    labs = filtered_item.get("labs") or []
    filtered_item["labs"] = attach_reservations([{"id": filtered_item.get("id"), "labs": labs}])[0]["labs"] if labs else []
    filtered_item = sandbox_labs.attach_flags([filtered_item])[0]
    filtered_item = lab_locks.attach_flags([filtered_item])[0]
    item = filtered_item
    return item