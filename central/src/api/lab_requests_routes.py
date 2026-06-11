from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.services import lab_requests
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, get_current_user, require_admin


router = APIRouter()


class LabRequestCreateRequest(BaseModel):
    suggestion_title: str = ""
    need_details: str
    resources_requirements: str = ""
    requested_router_images: str = ""


class LabRequestAdminUpdateRequest(BaseModel):
    status: str | None = None
    admin_response: str | None = None


@router.get("/api/lab-requests")
async def list_lab_requests(user: AuthenticatedUser = Depends(get_current_user)):
    items = lab_requests.list_requests(
        actor_username=user.username,
        actor_is_admin=(user.role == "admin"),
    )
    return {
        "ok": True,
        "items": items,
        "viewer": {
            "username": user.username,
            "role": user.role,
            "is_admin": user.role == "admin",
        },
        "statuses": lab_requests.allowed_statuses(),
    }


@router.post("/api/lab-requests")
async def create_lab_request(
    body: LabRequestCreateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        item = lab_requests.create_request(
            owner_username=user.username,
            owner_full_name=user.full_name,
            owner_email=user.email,
            suggestion_title=body.suggestion_title,
            need_details=body.need_details,
            resources_requirements=body.resources_requirements,
            requested_router_images=body.requested_router_images,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="lab_request.create",
        status="ok",
        details={"request_id": item.get("id")},
    )
    return {"ok": True, "item": item}


@router.post("/api/admin/lab-requests/{request_id}/claim")
async def admin_claim_lab_request(
    request_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    try:
        item = lab_requests.claim_request(
            request_id=request_id,
            admin_username=user.username,
            admin_full_name=user.full_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="lab_request.claim",
        status="ok",
        details={"request_id": request_id},
    )
    return {"ok": True, "item": item}


@router.patch("/api/admin/lab-requests/{request_id}")
async def admin_update_lab_request(
    request_id: str,
    body: LabRequestAdminUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    if body.status is None and body.admin_response is None:
        raise HTTPException(status_code=400, detail="Aucune mise a jour demandee")

    try:
        item = lab_requests.update_request_by_admin(
            request_id=request_id,
            admin_username=user.username,
            admin_full_name=user.full_name,
            status=body.status,
            admin_response=body.admin_response,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="lab_request.update",
        status="ok",
        details={"request_id": request_id, "status": item.get("status")},
    )
    return {"ok": True, "item": item}


@router.delete("/api/admin/lab-requests/{request_id}")
async def admin_delete_lab_request(
    request_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    try:
        deleted = lab_requests.delete_request(request_id=request_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record_event(
        request=request,
        user=user,
        action="lab_request.delete",
        status="ok",
        details={"request_id": request_id, "owner_username": deleted.get("owner_username")},
    )
    return {"ok": True}