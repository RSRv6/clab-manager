from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import get_settings
from src.services import docker_images_cache as _docker_cache
from src.services import topology_builder as _topo_builder
from src.services.audit import record_event
from src.services.auth import AuthenticatedUser, get_current_user, user_visible_vm_ids
from src.services.collector import fetch_agent_docker_images, submit_sandbox_yaml


router = APIRouter()
logger = logging.getLogger(__name__)


class TopologyNodeDef(BaseModel):
    name: str
    family: str
    image: str
    mgmt_ip: str | None = None
    startup_config: str | None = None
    original_kind: str | None = None
    extra_params: dict | None = None


class TopologyLinkDef(BaseModel):
    src_node: str
    src_iface: str | None = None
    dst_node: str
    dst_iface: str | None = None


class TopologyDefinition(BaseModel):
    lab_name: str
    mgmt_network: str
    mgmt_subnet: str
    nodes: list[TopologyNodeDef] = []
    links: list[TopologyLinkDef] = []
    node_positions: dict[str, dict[str, float]] = {}
    imported_node_count: int | None = None
    imported_link_count: int | None = None


class TopologyGenerateRequest(BaseModel):
    topology: TopologyDefinition
    vm_id: str | None = None
    create_sandbox: bool = False


@router.get("/api/topology-builder/metadata")
async def topology_builder_metadata(_: AuthenticatedUser = Depends(get_current_user)):
    settings = get_settings()
    agents = settings.get("agents") or []
    vms = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "management_subnet": a.get("management_subnet", ""),
        }
        for a in agents
        if a.get("id")
    ]
    return {"vms": vms, "families": _topo_builder.get_all_families()}


@router.post("/api/topology-builder/validate")
async def topology_builder_validate(
    body: TopologyDefinition,
    _: AuthenticatedUser = Depends(get_current_user),
):
    return _topo_builder.validate_topology(body.model_dump())


@router.post("/api/topology-builder/generate-yaml")
async def topology_builder_generate_yaml(
    body: TopologyDefinition,
    _: AuthenticatedUser = Depends(get_current_user),
):
    validation = _topo_builder.validate_topology(body.model_dump())
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"errors": validation["errors"]})

    yaml_text = _topo_builder.generate_clab_yaml(body.model_dump())
    return {"yaml": yaml_text}


@router.post("/api/topology-builder/create-sandbox")
async def topology_builder_create_sandbox(
    body: TopologyGenerateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not body.vm_id:
        raise HTTPException(status_code=400, detail="vm_id est obligatoire pour créer un sandbox.")

    settings = get_settings()
    agents = settings.get("agents") or []
    agent = next((a for a in agents if a.get("id") == body.vm_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail=f"VM '{body.vm_id}' inconnue.")

    visible_vm_ids = user_visible_vm_ids(user)
    if body.vm_id not in visible_vm_ids:
        raise HTTPException(status_code=403, detail="Accès refusé à cette VM.")

    topology_data = body.topology.model_dump()

    expected_subnet = (agent.get("management_subnet") or "").strip()
    if expected_subnet and topology_data.get("mgmt_subnet"):
        try:
            topo_net = ipaddress.IPv4Network(topology_data["mgmt_subnet"], strict=False)
            agent_net = ipaddress.IPv4Network(expected_subnet, strict=False)
            if not (topo_net.overlaps(agent_net) or topo_net == agent_net or agent_net.overlaps(topo_net)):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Le subnet de management '{topology_data['mgmt_subnet']}' n'est pas "
                        f"compatible avec le subnet attendu par la VM '{expected_subnet}'."
                    ),
                )
        except ValueError:
            pass

    validation = _topo_builder.validate_topology(topology_data)
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"errors": validation["errors"]})

    yaml_text = _topo_builder.generate_clab_yaml(topology_data)
    lab_name = topology_data["lab_name"]

    result = await submit_sandbox_yaml(
        agent,
        lab_name,
        yaml_text,
        apply=False,
        node_positions=topology_data.get("node_positions") or None,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=result.get("status_code", 500),
            detail=result.get("error", "Échec de la création du sandbox."),
        )

    record_event(
        request=request,
        user=user,
        action="topology_builder.create_sandbox",
        status="ok",
        vm_id=body.vm_id,
        lab_name=lab_name,
        details={"nodes": len(topology_data.get("nodes") or []), "links": len(topology_data.get("links") or [])},
    )

    return {"ok": True, "lab_name": lab_name, "yaml": yaml_text, "agent_result": result.get("data")}


@router.get("/api/topology-builder/images/{vm_id}")
async def topology_builder_images(
    vm_id: str,
    refresh: bool = False,
    user: AuthenticatedUser = Depends(get_current_user),
):
    settings = get_settings()
    agents = settings.get("agents") or []
    agent = next((a for a in agents if a.get("id") == vm_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' inconnue.")

    visible_vm_ids = user_visible_vm_ids(user)
    if vm_id not in visible_vm_ids:
        raise HTTPException(status_code=403, detail="Accès refusé à cette VM.")

    if refresh:
        import time as _time

        result = await fetch_agent_docker_images(agent)
        with _docker_cache._LOCK:
            _docker_cache._CACHE[vm_id] = {
                "images": result.get("images", []),
                "fetched_at": _time.monotonic(),
                "error": None if result.get("ok") else result.get("error"),
            }
        return {"images": result.get("images", []), "source": "live"}

    cached = _docker_cache.get_images_for_vm(vm_id)
    return {"images": cached, "source": "cache"}
