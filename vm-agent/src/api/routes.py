from __future__ import annotations

from typing import Annotated, Optional
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from src.audit.collector import get_docker_images, get_lab_details, get_labs, get_resources, get_topology_inventory
from src.config_management_service import apply_sandbox_topology_yaml, cancel_config_diff_job, cancel_reconfigure_job, cancel_set_default_job, export_lab_configs_zip, export_lab_topology_yaml, get_lab_config_diff, get_config_diff_job, reconfigure_lab_from_static_db, reset_sandbox_lab_to_baseline, set_current_config_as_default, start_config_diff_job, start_reconfigure_job, get_reconfigure_job, start_set_default_job, get_set_default_job, update_lab_topology_yaml, validate_sandbox_topology_yaml
from src.management.controller import redeploy_lab, restart_lab, start_lab, stop_lab
from src.security import verify_token

router = APIRouter()
logger = logging.getLogger(__name__)
AuthDep = Annotated[str, Depends(verify_token)]


def _internal_error(operation: str, exc: Exception) -> HTTPException:
    logger.exception("%s failed", operation, exc_info=exc)
    return HTTPException(status_code=500, detail="Internal agent error")


def _raise_lab_error(result: dict) -> None:
    """Raise HTTPException with appropriate status code based on operation result."""
    detail_payload: dict[str, object] = {}
    error_text = str(result.get("error") or "").strip()
    stderr_text = str(result.get("stderr") or "").strip()

    if error_text:
        detail_payload["error"] = error_text
    elif stderr_text:
        detail_payload["error"] = stderr_text
    else:
        detail_payload["error"] = "Lab action failed"

    if result.get("command"):
        detail_payload["command"] = result.get("command")
    if stderr_text:
        detail_payload["stderr"] = stderr_text
    if result.get("returncode") is not None:
        detail_payload["returncode"] = result.get("returncode")
    if result.get("stage"):
        detail_payload["stage"] = result.get("stage")

    if result.get("returncode") == 124:
        raise HTTPException(status_code=504, detail=detail_payload)

    status_code = result.get("status_code")
    try:
        parsed_status = int(status_code) if status_code is not None else 500
    except (TypeError, ValueError):
        parsed_status = 500
    raise HTTPException(status_code=parsed_status, detail=detail_payload)


class LabActionRequest(BaseModel):
    topology_file: Optional[str] = None


class ReconfigureRequest(BaseModel):
    router_names: list[str] | None = None
    mode: str | None = None


class ConfigDiffRequest(BaseModel):
    router_names: list[str] | None = None


class SandboxYamlRequest(BaseModel):
    yaml_text: str
    apply: bool = False
    expected_management_subnet: str | None = None
    node_positions: dict[str, dict[str, float]] | None = None


class TopologyYamlUpdateRequest(BaseModel):
    yaml_text: str
    expected_management_subnet: str | None = None
    node_positions: dict[str, dict[str, float]] | None = None


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/resources")
def resources(_: AuthDep):
    logger.info("api resources")
    return get_resources()


@router.get("/labs")
def labs(_: AuthDep):
    logger.info("api labs")
    return {"labs": get_labs()}


@router.get("/labs/inventory")
def labs_inventory(_: AuthDep):
    logger.info("api labs_inventory")
    return {"items": get_topology_inventory()}


@router.get("/labs/{lab_name}")
def lab_details(lab_name: str, _: AuthDep):
    logger.info("api lab_details lab=%s", lab_name)
    details = get_lab_details(lab_name)
    if not details:
        raise HTTPException(status_code=404, detail="Lab not found")
    return details


@router.post("/labs/{lab_name}/start")
def lab_start(lab_name: str, body: LabActionRequest, _: AuthDep):
    logger.info("api lab_start lab=%s", lab_name)
    result = start_lab(lab_name, topology_file=body.topology_file)
    if not result.get("ok"):
        _raise_lab_error(result)
    return result


@router.post("/labs/{lab_name}/stop")
def lab_stop(lab_name: str, _: AuthDep):
    logger.info("api lab_stop lab=%s", lab_name)
    result = stop_lab(lab_name)
    if not result.get("ok"):
        _raise_lab_error(result)
    return result


@router.post("/labs/{lab_name}/restart")
def lab_restart(lab_name: str, body: LabActionRequest, _: AuthDep):
    logger.info("api lab_restart lab=%s", lab_name)
    result = restart_lab(lab_name, topology_file=body.topology_file)
    if not result.get("ok"):
        _raise_lab_error(result)
    return result


@router.post("/labs/{lab_name}/redeploy")
def lab_redeploy(lab_name: str, body: LabActionRequest, _: AuthDep):
    logger.info("api lab_redeploy lab=%s", lab_name)
    result = redeploy_lab(lab_name, topology_file=body.topology_file)
    if not result.get("ok"):
        _raise_lab_error(result)
    return result


@router.post("/labs/{lab_name}/reconfigure")
def lab_reconfigure(lab_name: str, body: ReconfigureRequest, _: AuthDep):
    logger.info(
        "api lab_reconfigure lab=%s mode=%s router_names=%s",
        lab_name,
        body.mode or "base",
        body.router_names or "all",
    )
    result = start_reconfigure_job(lab_name, router_names=body.router_names, mode=body.mode)
    if not result.get("ok"):
        _raise_lab_error(result)
    return result


@router.post("/labs/{lab_name}/set-default-config")
def lab_set_default_config(lab_name: str, _: AuthDep):
    logger.info("api lab_set_default_config lab=%s", lab_name)
    result = start_set_default_job(lab_name)
    if not result.get("ok"):
        _raise_lab_error(result)
    return result


@router.get("/labs/{lab_name}/set-default-job/{job_id}")
def lab_set_default_job(lab_name: str, job_id: str, _: AuthDep):
    logger.info("api lab_set_default_job lab=%s job=%s", lab_name, job_id)
    job = get_set_default_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/labs/{lab_name}/set-default-job/{job_id}/cancel")
def lab_set_default_job_cancel(lab_name: str, job_id: str, _: AuthDep):
    logger.info("api lab_set_default_job_cancel lab=%s job=%s", lab_name, job_id)
    result = cancel_set_default_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Job not found"))
    return result


@router.get("/labs/{lab_name}/reconfigure-job/{job_id}")
def lab_reconfigure_job(lab_name: str, job_id: str, _: AuthDep):
    logger.info("api lab_reconfigure_job lab=%s job=%s", lab_name, job_id)
    job = get_reconfigure_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/labs/{lab_name}/reconfigure-job/{job_id}/cancel")
def lab_reconfigure_job_cancel(lab_name: str, job_id: str, _: AuthDep):
    logger.info("api lab_reconfigure_job_cancel lab=%s job=%s", lab_name, job_id)
    result = cancel_reconfigure_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Job not found"))
    return result


@router.post("/labs/{lab_name}/config-diff")
def lab_config_diff(lab_name: str, body: ConfigDiffRequest, _: AuthDep):
    logger.info("api lab_config_diff lab=%s router_names=%s", lab_name, body.router_names or "all")
    result = get_lab_config_diff(lab_name, router_names=body.router_names)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to get config diff"))
    return result


@router.post("/labs/{lab_name}/config-diff-async")
def lab_config_diff_async(lab_name: str, body: ConfigDiffRequest, _: AuthDep):
    logger.info("api lab_config_diff_async lab=%s router_names=%s", lab_name, body.router_names or "all")
    result = start_config_diff_job(lab_name, router_names=body.router_names)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to start config diff job"))
    return result


@router.get("/labs/{lab_name}/config-diff-job/{job_id}")
def lab_config_diff_job(lab_name: str, job_id: str, _: AuthDep):
    logger.info("api lab_config_diff_job lab=%s job=%s", lab_name, job_id)
    job = get_config_diff_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/labs/{lab_name}/config-diff-job/{job_id}/cancel")
def lab_config_diff_job_cancel(lab_name: str, job_id: str, _: AuthDep):
    logger.info("api lab_config_diff_job_cancel lab=%s job=%s", lab_name, job_id)
    result = cancel_config_diff_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Job not found"))
    return result


@router.get("/labs/{lab_name}/export-config")
def lab_export_config(lab_name: str, _: AuthDep):
    logger.info("api lab_export_config lab=%s", lab_name)
    try:
        archive_name, content = export_lab_configs_zip(lab_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _internal_error("lab_export_config", exc) from exc

    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{archive_name}"'},
    )


@router.get("/labs/{lab_name}/export-yaml")
def lab_export_yaml(lab_name: str, _: AuthDep):
    logger.info("api lab_export_yaml lab=%s", lab_name)
    try:
        file_name, content = export_lab_topology_yaml(lab_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _internal_error("lab_export_yaml", exc) from exc

    return Response(
        content=content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.put("/labs/{lab_name}/topology-yaml")
def lab_update_yaml(lab_name: str, body: TopologyYamlUpdateRequest, _: AuthDep):
    logger.info("api lab_update_yaml lab=%s", lab_name)
    try:
        result = update_lab_topology_yaml(
            lab_name,
            body.yaml_text,
            expected_management_subnet=body.expected_management_subnet,
            node_positions=body.node_positions,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _internal_error("lab_update_yaml", exc) from exc

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/labs/{lab_name}/sandbox-yaml")
def lab_sandbox_yaml(lab_name: str, body: SandboxYamlRequest, _: AuthDep):
    logger.info("api lab_sandbox_yaml lab=%s apply=%s", lab_name, body.apply)
    try:
        if body.apply:
            result = apply_sandbox_topology_yaml(
                lab_name,
                body.yaml_text,
                expected_management_subnet=body.expected_management_subnet,
                node_positions=body.node_positions,
            )
        else:
            result = {
                "ok": True,
                "validation": validate_sandbox_topology_yaml(
                    lab_name,
                    body.yaml_text,
                    expected_management_subnet=body.expected_management_subnet,
                ),
            }
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _internal_error("lab_sandbox_yaml", exc) from exc

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/labs/{lab_name}/sandbox-reset")
def lab_sandbox_reset(lab_name: str, _: AuthDep):
    logger.info("api lab_sandbox_reset lab=%s", lab_name)
    try:
        result = reset_sandbox_lab_to_baseline(lab_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _internal_error("lab_sandbox_reset", exc) from exc

    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/docker-images")
def docker_images(_: AuthDep):
    logger.info("api docker_images")
    return {"images": get_docker_images()}