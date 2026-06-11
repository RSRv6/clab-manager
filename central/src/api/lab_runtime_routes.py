from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from threading import Lock, Thread

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from src.config import get_settings
from src.services import lab_locks
from src.services.audit import list_events, record_event
from src.services.auth import AuthenticatedUser, get_current_user
from src.services.collector import (
    cancel_reconfigure_job,
    cancel_set_default_job,
    download_lab_export,
    download_lab_yaml,
    fetch_config_diff,
    fetch_reconfigure_job,
    fetch_set_default_job,
    invoke_lab_action,
)
from src.services.reservations import get_reservation, reservation_belongs_to

router = APIRouter()
logger = logging.getLogger(__name__)

_CONFIG_DIFF_JOBS_LOCK = Lock()
_CONFIG_DIFF_JOBS: dict[str, dict] = {}
_CONFIG_DIFF_JOBS_MAX = 100


class LabActionRequest(BaseModel):
    topology_file: str | None = None


class ReconfigureRequest(BaseModel):
    router_names: list[str] | None = None
    mode: str | None = None


class ConfigDiffRequest(BaseModel):
    router_names: list[str] | None = None


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


def _require_lab_operator(user: AuthenticatedUser, vm_id: str, lab_name: str) -> dict:
    _require_lab_visibility(user, vm_id, lab_name)
    reservation = get_reservation(vm_id, lab_name)
    if user.role == "admin":
        return reservation or {}
    if reservation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le LAB doit etre reserve avant cette action",
        )
    if not reservation_belongs_to(reservation, user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce LAB est reserve par un autre utilisateur",
        )
    return reservation


def _require_lock_override_for_non_admin(user: AuthenticatedUser, vm_id: str, lab_name: str, action_name: str) -> None:
    if user.role == "admin":
        return
    if not lab_locks.is_locked(vm_id, lab_name):
        return
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=f"Ce LAB est verrouille: seul un admin peut executer l'action {action_name}",
    )


def _create_config_diff_job(vm_id: str, lab_name: str, agent: dict, router_names: list[str] | None = None) -> str:
    job_id = secrets.token_hex(16)
    requested = list(router_names or [])
    job = {
        "job_id": job_id,
        "vm_id": vm_id,
        "lab": lab_name,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "error": None,
        "requested_routers": requested,
        "requested_count": len(requested),
        "total": 0,
        "completed_count": 0,
        "success_count": 0,
        "result": None,
    }
    with _CONFIG_DIFF_JOBS_LOCK:
        _CONFIG_DIFF_JOBS[job_id] = job
        if len(_CONFIG_DIFF_JOBS) > _CONFIG_DIFF_JOBS_MAX:
            oldest_key = next(iter(_CONFIG_DIFF_JOBS))
            _CONFIG_DIFF_JOBS.pop(oldest_key, None)

    def _runner() -> None:
        try:
            result = asyncio.run(fetch_config_diff(agent=agent, lab_name=lab_name, router_names=router_names))
            if not result.get("ok"):
                with _CONFIG_DIFF_JOBS_LOCK:
                    job["status"] = "error"
                    job["error"] = result.get("error") or "Config diff failed"
                    job["finished_at"] = datetime.now(timezone.utc).isoformat()
                return

            payload = result.get("data") or {}
            rows = payload.get("results") if isinstance(payload, dict) else []
            rows = rows if isinstance(rows, list) else []
            success_count = sum(1 for item in rows if isinstance(item, dict) and item.get("ok"))
            with _CONFIG_DIFF_JOBS_LOCK:
                job["status"] = "completed"
                job["result"] = payload
                job["total"] = len(rows)
                job["completed_count"] = len(rows)
                job["success_count"] = success_count
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            logger.exception("config_diff_job runner failed vm=%s lab=%s job=%s", vm_id, lab_name, job_id, exc_info=exc)
            with _CONFIG_DIFF_JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(exc) or "Config diff failed"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()

    Thread(target=_runner, daemon=True).start()
    return job_id


def _get_config_diff_job(job_id: str) -> dict | None:
    with _CONFIG_DIFF_JOBS_LOCK:
        job = _CONFIG_DIFF_JOBS.get(job_id)
        return dict(job) if isinstance(job, dict) else None


@router.post("/api/vms/{vm_id}/labs/{lab_name}/redeploy")
async def redeploy_lab(
    vm_id: str,
    lab_name: str,
    body: LabActionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api redeploy vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    _require_lock_override_for_non_admin(user, vm_id, lab_name, "redeploy")
    if not user.can_redeploy_lab():
        record_event(
            request=request,
            user=user,
            action="lab.redeploy",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Le redeploy est reserve aux administrateurs et group-admin")
    agent = _find_agent(vm_id)
    if user.role == "admin":
        _require_lab_operator(user, vm_id, lab_name)

    stop_result = await invoke_lab_action(agent=agent, lab_name=lab_name, action="stop")
    if not stop_result.get("ok"):
        record_event(
            request=request,
            user=user,
            action="lab.redeploy",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"stage": "stop", "error": stop_result.get("error", "Stop failed")},
        )
        raise HTTPException(status_code=stop_result.get("status_code", 400), detail=stop_result.get("error", "Stop failed"))

    start_result = await invoke_lab_action(
        agent=agent,
        lab_name=lab_name,
        action="start",
        topology_file=body.topology_file,
    )
    if not start_result.get("ok"):
        record_event(
            request=request,
            user=user,
            action="lab.redeploy",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"stage": "start", "error": start_result.get("error", "Start failed")},
        )
        raise HTTPException(status_code=start_result.get("status_code", 400), detail=start_result.get("error", "Start failed"))

    record_event(request=request, user=user, action="lab.redeploy", status="ok", vm_id=vm_id, lab_name=lab_name)
    return {
        "ok": True,
        "mode": "sequential_stop_start",
        "steps": [
            {"stage": "stop", "ok": True, "result": stop_result.get("data")},
            {"stage": "start", "ok": True, "result": start_result.get("data")},
        ],
        "result": start_result.get("data"),
    }


@router.post("/api/vms/{vm_id}/labs/{lab_name}/start")
async def start_lab(
    vm_id: str,
    lab_name: str,
    body: LabActionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api start vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    _require_lock_override_for_non_admin(user, vm_id, lab_name, "start")
    if not user.can_redeploy_lab():
        record_event(
            request=request,
            user=user,
            action="lab.start",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Le start est reserve aux administrateurs et group-admin")
    agent = _find_agent(vm_id)
    if user.role == "admin":
        _require_lab_operator(user, vm_id, lab_name)

    result = await invoke_lab_action(agent=agent, lab_name=lab_name, action="start", topology_file=body.topology_file)
    if not result.get("ok"):
        record_event(
            request=request,
            user=user,
            action="lab.start",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": result.get("error", "Start failed")},
        )
        raise HTTPException(
            status_code=result.get("status_code", 400),
            detail=result.get("error_payload", result.get("error", "Start failed")),
        )

    record_event(request=request, user=user, action="lab.start", status="ok", vm_id=vm_id, lab_name=lab_name)
    return {"ok": True, "result": result.get("data")}


@router.post("/api/vms/{vm_id}/labs/{lab_name}/stop")
async def stop_lab(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api stop vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    _require_lock_override_for_non_admin(user, vm_id, lab_name, "stop")
    if not user.can_redeploy_lab():
        record_event(
            request=request,
            user=user,
            action="lab.stop",
            status="denied",
            vm_id=vm_id,
            lab_name=lab_name,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Le stop est reserve aux administrateurs et group-admin")
    agent = _find_agent(vm_id)
    if user.role == "admin":
        _require_lab_operator(user, vm_id, lab_name)

    result = await invoke_lab_action(agent=agent, lab_name=lab_name, action="stop")
    if not result.get("ok"):
        record_event(
            request=request,
            user=user,
            action="lab.stop",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": result.get("error", "Stop failed")},
        )
        raise HTTPException(
            status_code=result.get("status_code", 400),
            detail=result.get("error_payload", result.get("error", "Stop failed")),
        )

    record_event(request=request, user=user, action="lab.stop", status="ok", vm_id=vm_id, lab_name=lab_name)
    return {"ok": True, "result": result.get("data")}


@router.post("/api/vms/{vm_id}/labs/{lab_name}/reconfigure")
async def reconfigure_lab(
    vm_id: str,
    lab_name: str,
    body: ReconfigureRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info(
        "api reconfigure vm=%s lab=%s user=%s mode=%s router_names=%s",
        vm_id, lab_name, user.username, body.mode or "base", body.router_names or "all",
    )
    _require_lock_override_for_non_admin(user, vm_id, lab_name, "reconfigure")
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)

    result = await invoke_lab_action(
        agent=agent,
        lab_name=lab_name,
        action="reconfigure",
        router_names=body.router_names,
        mode=body.mode,
    )

    if not result.get("ok"):
        record_event(
            request=request,
            user=user,
            action="lab.reconfigure",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": result.get("error", "Reconfiguration failed")},
        )
        raise HTTPException(
            status_code=result.get("status_code", 400),
            detail=result.get("error_payload", result.get("error", "Reconfiguration failed")),
        )

    data = result.get("data") or {}
    job_id = data.get("job_id")
    record_event(
        request=request,
        user=user,
        action="lab.reconfigure",
        status="started",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"job_id": job_id},
    )
    return {"ok": True, "job_id": job_id}


@router.post("/api/vms/{vm_id}/labs/{lab_name}/config-diff")
async def config_diff_lab(
    vm_id: str,
    lab_name: str,
    body: ConfigDiffRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api config_diff vm=%s lab=%s user=%s router_names=%s", vm_id, lab_name, user.username, body.router_names or "all")
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)

    result = await fetch_config_diff(agent=agent, lab_name=lab_name, router_names=body.router_names)
    if not result.get("ok"):
        error_detail = result.get("error") or "Config diff failed"
        record_event(
            request=request,
            user=user,
            action="lab.config_diff",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": error_detail},
        )
        raise HTTPException(status_code=result.get("status_code", 502), detail=error_detail)

    payload = result.get("data") or {}
    selected_count = len(body.router_names or [])
    results_count = len(payload.get("results") or []) if isinstance(payload, dict) else 0
    record_event(
        request=request,
        user=user,
        action="lab.config_diff",
        status="ok",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"selected_count": selected_count, "results_count": results_count},
    )
    return payload


@router.post("/api/vms/{vm_id}/labs/{lab_name}/config-diff-async")
async def config_diff_lab_async(
    vm_id: str,
    lab_name: str,
    body: ConfigDiffRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api config_diff_async vm=%s lab=%s user=%s router_names=%s", vm_id, lab_name, user.username, body.router_names or "all")
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)

    job_id = _create_config_diff_job(vm_id=vm_id, lab_name=lab_name, agent=agent, router_names=body.router_names)
    record_event(
        request=request,
        user=user,
        action="lab.config_diff",
        status="started",
        vm_id=vm_id,
        lab_name=lab_name,
        details={"stage": "start", "job_id": job_id, "selected_count": len(body.router_names or [])},
    )
    return {"ok": True, "job_id": job_id}


@router.get("/api/vms/{vm_id}/labs/{lab_name}/config-diff-job/{job_id}")
async def config_diff_job_status(
    vm_id: str,
    lab_name: str,
    job_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api config_diff_job_status vm=%s lab=%s job=%s user=%s", vm_id, lab_name, job_id, user.username)
    _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)

    payload = _get_config_diff_job(job_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Job unavailable")
    if payload.get("vm_id") != vm_id or payload.get("lab") != lab_name:
        raise HTTPException(status_code=404, detail="Job unavailable")

    status_value = str(payload.get("status") or "")
    if status_value in {"completed", "error"}:
        completion_status = "ok" if status_value == "completed" else "error"
        recent = list_events(limit=200, username=user.username, vm_id=vm_id, action_prefix="lab.config_diff")
        already_recorded = any(
            str(event.get("action") or "") == "lab.config_diff"
            and str(event.get("status") or "") == completion_status
            and str(event.get("lab_name") or "") == lab_name
            and isinstance(event.get("details"), dict)
            and str((event.get("details") or {}).get("stage") or "") == "completed"
            and str((event.get("details") or {}).get("job_id") or "") == job_id
            for event in recent
        )
        if not already_recorded:
            record_event(
                request=request,
                user=user,
                action="lab.config_diff",
                status=completion_status,
                vm_id=vm_id,
                lab_name=lab_name,
                details={
                    "stage": "completed",
                    "job_id": job_id,
                    "processed": payload.get("completed_count", 0),
                    "total": payload.get("total", 0),
                    "success": payload.get("success_count", 0),
                },
            )
    return payload


@router.post("/api/vms/{vm_id}/labs/{lab_name}/set-default-config")
async def set_lab_default_config(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api set_default_config vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    _require_lab_visibility(user, vm_id, lab_name)
    _require_lock_override_for_non_admin(user, vm_id, lab_name, "set-default-config")
    if user.role not in {"admin", "group-admin"}:
        record_event(
            request=request, user=user, action="lab.set_default_config", status="denied", vm_id=vm_id, lab_name=lab_name,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Action reservee aux administrateurs et group-admin")

    agent = _find_agent(vm_id)
    result = await invoke_lab_action(agent=agent, lab_name=lab_name, action="set-default-config")

    if not result.get("ok"):
        record_event(
            request=request,
            user=user,
            action="lab.set_default_config",
            status="error",
            vm_id=vm_id,
            lab_name=lab_name,
            details={"error": result.get("error", "Set default config failed")},
        )
        raise HTTPException(
            status_code=result.get("status_code", 400),
            detail=result.get("error_payload", result.get("error", "Set default config failed")),
        )

    payload = result.get("data") or {}
    job_id = payload.get("job_id")
    record_event(
        request=request, user=user, action="lab.set_default_config", status="started", vm_id=vm_id, lab_name=lab_name,
        details={"job_id": job_id},
    )
    return {"ok": True, "job_id": job_id}


@router.get("/api/vms/{vm_id}/labs/{lab_name}/set-default-job/{job_id}")
async def set_default_job_status(
    vm_id: str,
    lab_name: str,
    job_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api set_default_job_status vm=%s lab=%s job=%s user=%s", vm_id, lab_name, job_id, user.username)
    agent = _find_agent(vm_id)
    _require_lab_visibility(user, vm_id, lab_name)
    result = await fetch_set_default_job(agent=agent, lab_name=lab_name, job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=result.get("status_code", 502),
            detail=result.get("error_payload", result.get("error", "Job unavailable")),
        )
    payload = result.get("data") or {}
    status_value = str(payload.get("status") or "").strip().lower()
    terminal_statuses = {"completed", "completed_with_errors", "failed", "error", "cancelled", "canceled"}
    if status_value in terminal_statuses:
        total_count = int(payload.get("total") or 0)
        saved_count = int(payload.get("saved_count") or 0)
        success_count = int(payload.get("success_count") or 0)
        failed_count = max(total_count - max(saved_count, success_count), 0)

        completion_status = "warning" if status_value in {"cancelled", "canceled"} else (
            "ok" if payload.get("ok") is True and failed_count <= 0 else "error"
        )
        recent = list_events(limit=200, username=user.username, vm_id=vm_id, action_prefix="lab.set_default_config")
        already_recorded = any(
            str(event.get("action") or "") == "lab.set_default_config"
            and str(event.get("status") or "") == completion_status
            and str(event.get("lab_name") or "") == lab_name
            and isinstance(event.get("details"), dict)
            and str((event.get("details") or {}).get("stage") or "") == "completed"
            and str((event.get("details") or {}).get("job_id") or "") == job_id
            for event in recent
        )
        if not already_recorded:
            record_event(
                request=request,
                user=user,
                action="lab.set_default_config",
                status=completion_status,
                vm_id=vm_id,
                lab_name=lab_name,
                details={
                    "stage": "completed",
                    "job_id": job_id,
                    "processed": payload.get("completed_count", payload.get("processed", 0)),
                    "total": payload.get("total", 0),
                    "success": success_count,
                    "saved_count": saved_count,
                    "failed": failed_count,
                    "status": status_value,
                },
            )
    return payload


@router.post("/api/vms/{vm_id}/labs/{lab_name}/set-default-job/{job_id}/cancel")
async def cancel_set_default_job_status(
    vm_id: str,
    lab_name: str,
    job_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api cancel_set_default_job vm=%s lab=%s job=%s user=%s", vm_id, lab_name, job_id, user.username)
    agent = _find_agent(vm_id)
    _require_lab_visibility(user, vm_id, lab_name)
    if user.role not in {"admin", "group-admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Action reservee aux administrateurs et group-admin")
    result = await cancel_set_default_job(agent=agent, lab_name=lab_name, job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=result.get("status_code", 502),
            detail=result.get("error_payload", result.get("error", "Cancel request failed")),
        )

    payload = result.get("data") or {}
    record_event(
        request=request, user=user, action="lab.set_default_config", status="warning", vm_id=vm_id, lab_name=lab_name,
        details={"stage": "cancel_requested", "job_id": job_id},
    )
    return payload


@router.get("/api/vms/{vm_id}/labs/{lab_name}/reconfigure-job/{job_id}")
async def reconfigure_job_status(
    vm_id: str,
    lab_name: str,
    job_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api reconfigure_job_status vm=%s lab=%s job=%s user=%s", vm_id, lab_name, job_id, user.username)
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)
    result = await fetch_reconfigure_job(agent=agent, lab_name=lab_name, job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=result.get("status_code", 502),
            detail=result.get("error_payload", result.get("error", "Job unavailable")),
        )
    payload = result.get("data") or {}
    status_value = str(payload.get("status") or "").strip().lower()
    terminal_statuses = {"completed", "completed_with_errors", "failed", "error", "cancelled", "canceled"}
    if status_value in terminal_statuses:
        total_count = int(payload.get("total") or 0)
        success_count = int(payload.get("success_count") or 0)
        failed_count = max(total_count - success_count, 0)

        completion_status = "warning" if status_value in {"cancelled", "canceled"} else (
            "ok" if payload.get("ok") is True and failed_count <= 0 else "error"
        )
        recent = list_events(limit=200, username=user.username, vm_id=vm_id, action_prefix="lab.reconfigure")
        already_recorded = any(
            str(event.get("action") or "") == "lab.reconfigure"
            and str(event.get("status") or "") == completion_status
            and str(event.get("lab_name") or "") == lab_name
            and isinstance(event.get("details"), dict)
            and str((event.get("details") or {}).get("stage") or "") == "completed"
            and str((event.get("details") or {}).get("job_id") or "") == job_id
            for event in recent
        )
        if not already_recorded:
            record_event(
                request=request,
                user=user,
                action="lab.reconfigure",
                status=completion_status,
                vm_id=vm_id,
                lab_name=lab_name,
                details={
                    "stage": "completed",
                    "job_id": job_id,
                    "processed": payload.get("completed_count", payload.get("processed", 0)),
                    "total": total_count,
                    "success": success_count,
                    "failed": failed_count,
                    "status": status_value,
                },
            )
    return payload


@router.post("/api/vms/{vm_id}/labs/{lab_name}/reconfigure-job/{job_id}/cancel")
async def cancel_reconfigure_job_status(
    vm_id: str,
    lab_name: str,
    job_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api cancel_reconfigure_job vm=%s lab=%s job=%s user=%s", vm_id, lab_name, job_id, user.username)
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)
    result = await cancel_reconfigure_job(agent=agent, lab_name=lab_name, job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=result.get("status_code", 502),
            detail=result.get("error_payload", result.get("error", "Cancel request failed")),
        )

    payload = result.get("data") or {}
    record_event(
        request=request, user=user, action="lab.reconfigure", status="warning", vm_id=vm_id, lab_name=lab_name,
        details={"stage": "cancel_requested", "job_id": job_id},
    )
    return payload


@router.get("/api/vms/{vm_id}/labs/{lab_name}/export-config")
async def export_config(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api export_config vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)

    result = await download_lab_export(agent=agent, lab_name=lab_name)
    if not result.get("ok"):
        record_event(
            request=request, user=user, action="lab.export_config", status="error", vm_id=vm_id, lab_name=lab_name,
            details={"error": result.get("error", "Export failed")},
        )
        raise HTTPException(
            status_code=result.get("status_code", 400),
            detail=result.get("error_payload", result.get("error", "Export failed")),
        )

    record_event(request=request, user=user, action="lab.export_config", status="ok", vm_id=vm_id, lab_name=lab_name)
    return Response(
        content=result.get("content", b""),
        media_type=result.get("content_type", "application/zip"),
        headers={"Content-Disposition": result.get("content_disposition", f'attachment; filename="{lab_name}-configs.zip"')},
    )


@router.get("/api/vms/{vm_id}/labs/{lab_name}/export-yaml")
async def export_yaml(
    vm_id: str,
    lab_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    logger.info("api export_yaml vm=%s lab=%s user=%s", vm_id, lab_name, user.username)
    agent = _find_agent(vm_id)
    _require_lab_operator(user, vm_id, lab_name)

    result = await download_lab_yaml(agent=agent, lab_name=lab_name)
    if not result.get("ok"):
        record_event(
            request=request, user=user, action="lab.export_yaml", status="error", vm_id=vm_id, lab_name=lab_name,
            details={"error": result.get("error", "Export YAML failed")},
        )
        raise HTTPException(
            status_code=result.get("status_code", 400),
            detail=result.get("error_payload", result.get("error", "Export YAML failed")),
        )

    record_event(request=request, user=user, action="lab.export_yaml", status="ok", vm_id=vm_id, lab_name=lab_name)
    return Response(
        content=result.get("content", b""),
        media_type=result.get("content_type", "application/x-yaml"),
        headers={"Content-Disposition": result.get("content_disposition", f'attachment; filename="{lab_name}.yaml"')},
    )
