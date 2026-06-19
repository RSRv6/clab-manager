from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

import httpx
import yaml

from src.services import clab_api_auth

logger = logging.getLogger(__name__)


# ── Generic helpers ────────────────────────────────────────────────────────────

def _extract_error_text(payload: object) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, dict):
            for key in ("error", "stderr", "message"):
                value = detail.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            compact = json.dumps(detail, ensure_ascii=True)
            return compact if compact else "Agent request failed"
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        compact = json.dumps(payload, ensure_ascii=True)
        return compact if compact else "Agent request failed"
    if isinstance(payload, str):
        return payload.strip() or "Agent request failed"
    return "Agent request failed"


def _agent_poll_concurrency() -> int:
    raw = os.getenv("CENTRAL_AGENT_POLL_CONCURRENCY", "8").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def _default_limits() -> httpx.Limits:
    concurrency = _agent_poll_concurrency()
    return httpx.Limits(max_connections=concurrency * 4, max_keepalive_connections=concurrency * 2)


def _agent_base_url(agent: dict) -> str:
    base_url = str(agent.get("base_url") or "").strip()
    if base_url:
        return base_url.rstrip("/")
    ip = str(agent.get("ip") or "").strip()
    port = agent.get("port", 8081)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8081
    return f"http://{ip}:{port}".rstrip("/")


# ── clab-api-server: routing state ────────────────────────────────────────────

# Actions that must always go to vm-agent (SSH-based, no clab-api equivalent)
_VM_AGENT_ONLY = frozenset({"reconfigure", "set-default-config"})

# Per-agent availability state (updated every poll cycle by fetch_agent_state)
_state_lock = threading.Lock()
_clab_avail: dict[str, bool] = {}       # agent_id → True if clab-api-server is reachable
_clab_labs: dict[str, set[str]] = {}    # agent_id → lab names owned by clab-api-server


def _clab_base(agent: dict) -> str:
    custom = str(agent.get("clab_api_base_url") or "").strip()
    return custom.rstrip("/") if custom else f"http://{agent.get('ip', '')}:8080"


def _clab_available(agent: dict) -> bool:
    with _state_lock:
        return _clab_avail.get(str(agent.get("id") or ""), False)


def _clab_owns_lab(agent: dict, lab_name: str) -> bool:
    with _state_lock:
        return lab_name in _clab_labs.get(str(agent.get("id") or ""), set())


async def _clab_request(
    client: httpx.AsyncClient,
    agent: dict,
    method: str,
    path: str,
    **kwargs,
) -> httpx.Response:
    """Authenticated request to clab-api-server; retries once on 401 (token expiry)."""
    token = await clab_api_auth.get_token(agent, client)
    kwargs.setdefault("headers", {})["Authorization"] = f"Bearer {token}"
    resp = await client.request(method, f"{_clab_base(agent)}{path}", **kwargs)
    if resp.status_code == 401:
        clab_api_auth.invalidate(str(agent.get("id") or ""))
        token = await clab_api_auth.get_token(agent, client)
        kwargs["headers"]["Authorization"] = f"Bearer {token}"
        resp = await client.request(method, f"{_clab_base(agent)}{path}", **kwargs)
    return resp


# ── clab-api-server: data normalisation ───────────────────────────────────────

def _norm_clab_resources(data: dict) -> dict:
    """Map GET /api/v1/health/metrics → VLM resources format (same as vm-agent).

    clab-api-server v0.4.0 structure:
      {"metrics": {"cpu": {"usagePercent": ...},
                   "mem": {"totalMem": ..., "usedMem": ..., "usagePercent": ...},
                   "disk": {"totalDisk": ..., "usedDisk": ..., "usagePercent": ...}}}
    """
    m = data.get("metrics") or {}
    cpu = m.get("cpu") or {}
    mem = m.get("mem") or {}
    disk = m.get("disk") or {}
    return {
        "cpu_percent": round(float(cpu.get("usagePercent") or 0.0), 1),
        "memory": {
            "total": int(mem.get("totalMem") or 0),
            "used": int(mem.get("usedMem") or 0),
            "percent": round(float(mem.get("usagePercent") or 0.0), 1),
        },
        "disk": {
            "total": int(disk.get("totalDisk") or 0),
            "used": int(disk.get("usedDisk") or 0),
            "percent": round(float(disk.get("usagePercent") or 0.0), 1),
        },
    }


def _ep_node(v: object) -> str | None:
    if not isinstance(v, str) or not v:
        return None
    return v.split(":", 1)[0]


def _ep_iface(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    parts = v.split(":", 1)
    return parts[1] if len(parts) >= 2 else None


_GRAPH_POSITIONS_FILE = os.path.join(
    os.path.dirname(__file__), "../../data/lab_graph_positions.json"
)


def _load_graph_positions(lab_name: str) -> dict[str, dict[str, float]]:
    """Load saved node positions for a lab from the central data store."""
    try:
        path = os.path.normpath(_GRAPH_POSITIONS_FILE)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        lab_positions = data.get(lab_name) or {}
        return {k: v for k, v in lab_positions.items() if isinstance(v, dict)}
    except Exception:
        return {}


_positions_file_lock = threading.Lock()


def _save_graph_positions(lab_name: str, positions: dict[str, dict[str, float]]) -> None:
    """Persist node positions for a lab into the central data store (thread-safe)."""
    if not positions:
        return
    path = os.path.normpath(_GRAPH_POSITIONS_FILE)
    try:
        with _positions_file_lock:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[lab_name] = positions
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        logger.info("_save_graph_positions: saved %d positions for lab '%s'", len(positions), lab_name)
    except Exception as exc:
        logger.warning("_save_graph_positions: failed for lab '%s': %s", lab_name, exc)


def _positions_from_vm_graph(vm_lab: dict | None) -> dict[str, dict[str, float]]:
    """Extract node positions from a vm-agent lab graph (used as fallback for clab-api)."""
    if not vm_lab:
        return {}
    nodes = (vm_lab.get("graph") or {}).get("nodes") or []
    return {
        n["name"]: {"x": float(n["x"]), "y": float(n["y"])}
        for n in nodes
        if isinstance(n, dict) and n.get("name") and n.get("x") is not None and n.get("y") is not None
    }


def _graph_from_yaml(yaml_text: str, positions: dict[str, dict[str, float]] | None = None) -> dict:
    """Parse topology YAML → {nodes, links} for the UI graph."""
    try:
        topo = yaml.safe_load(yaml_text) or {}
    except Exception:
        return {"nodes": [], "links": []}
    topo_nodes = (topo.get("topology") or {}).get("nodes") or {}
    topo_links = (topo.get("topology") or {}).get("links") or []
    pos = positions or {}
    nodes: list[dict] = []
    known: set[str] = set()
    if isinstance(topo_nodes, dict):
        for name, nd in topo_nodes.items():
            nd = nd if isinstance(nd, dict) else {}
            p = pos.get(name, {})
            nodes.append({"name": name, "kind": nd.get("kind"), "image": nd.get("image"),
                          "x": p.get("x"), "y": p.get("y")})
            known.add(name)
    links: list[dict] = []
    if isinstance(topo_links, list):
        for lnk in topo_links:
            if not isinstance(lnk, dict):
                continue
            eps = lnk.get("endpoints") or []
            if not isinstance(eps, list) or len(eps) < 2:
                continue
            src = _ep_node(eps[0])
            tgt = _ep_node(eps[1])
            if not src or not tgt:
                continue
            links.append({
                "source": src, "target": tgt,
                "source_if": _ep_iface(eps[0]), "target_if": _ep_iface(eps[1]),
            })
            for ep in (src, tgt):
                if ep not in known:
                    p = pos.get(ep, {})
                    nodes.append({"name": ep, "kind": "external", "image": None,
                                  "x": p.get("x"), "y": p.get("y")})
                    known.add(ep)
    return {"nodes": nodes, "links": links}


def _norm_clab_labs(data: object) -> list[dict]:
    """Normalize GET /api/v1/labs response → VLM lab list format."""
    if not isinstance(data, dict):
        return []
    labs = []
    for lab_name, containers in data.items():
        if not isinstance(containers, list):
            continue
        states = [str(c.get("state") or "") for c in containers if isinstance(c, dict)]
        if any(s == "running" for s in states):
            status = "running"
        elif states:
            status = "stopped"
        else:
            status = "unknown"
        topo_file = ""
        for c in containers:
            if isinstance(c, dict) and c.get("absLabPath"):
                topo_file = str(c["absLabPath"])
                break
        routers = []
        for i, c in enumerate(containers):
            if not isinstance(c, dict):
                continue
            raw_ip = str(c.get("ipv4_address") or "")
            mgmt_ip = raw_ip.split("/")[0] if raw_ip else None
            routers.append({
                "name": str(c.get("nodeName") or c.get("name") or f"node{i}"),
                "state": c.get("state"),
                "kind": c.get("kind"),
                "image": c.get("image"),
                "mgmt_ipv4": mgmt_ip or None,
                "uptime": None,
            })
        labs.append({
            "name": str(lab_name),
            "status": status,
            "topology_file": topo_file,
            "node_count": len(routers),
            "routers": routers,
            "graph": {"nodes": [], "links": []},
        })
    return labs


async def _enrich_graph(
    client: httpx.AsyncClient,
    agent: dict,
    lab: dict,
    vm_lab: dict | None = None,
) -> None:
    """Fetch topology/yaml from clab-api and build the UI graph.

    Position resolution order (first non-empty wins):
    1. Central data/lab_graph_positions.json  (manually curated or previously auto-saved)
    2. vm-agent graph positions                (from local .annotations.json on the VM)
    If positions are found via the vm-agent fallback they are automatically persisted
    into the central JSON so future polls don't need the vm-agent data.
    """
    try:
        resp = await _clab_request(
            client, agent, "GET", f"/api/v1/labs/{lab['name']}/topology/yaml",
        )
        if resp.status_code < 400:
            positions = _load_graph_positions(lab["name"])
            if not positions and vm_lab:
                positions = _positions_from_vm_graph(vm_lab)
                if positions:
                    _save_graph_positions(lab["name"], positions)
            lab["graph"] = _graph_from_yaml(resp.text, positions)
    except Exception:
        pass


# ── State collection ───────────────────────────────────────────────────────────

async def fetch_agent_state(agent, client: httpx.AsyncClient | None = None):
    agent_id = str(agent.get("id") or "")
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    vm_headers = {"Authorization": f"Bearer {token}"} if token else {}
    out = {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "base_url": base,
        "online": False,
        "resources": {},
        "labs": [],
        "error": None,
    }
    logger.info("fetch_agent_state start vm=%s base=%s", out["name"], base)
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=4.0, limits=_default_limits()) as owned_client:
                return await fetch_agent_state(agent, client=owned_client)

        # All probes in parallel; clab probe uses a shorter timeout to not block the poll cycle
        clab_probe, vm_health, vm_res, vm_labs = await asyncio.gather(
            client.get(f"{_clab_base(agent)}/health", timeout=2.0),
            client.get(f"{base}/health"),
            client.get(f"{base}/resources", headers=vm_headers),
            client.get(f"{base}/labs", headers=vm_headers),
            return_exceptions=True,
        )

        clab_ok = isinstance(clab_probe, httpx.Response) and clab_probe.status_code < 400
        vm_ok = isinstance(vm_health, httpx.Response) and vm_health.status_code < 400

        with _state_lock:
            _clab_avail[agent_id] = clab_ok

        if not clab_ok and not vm_ok:
            if isinstance(vm_health, Exception):
                raise vm_health
            vm_health.raise_for_status()  # type: ignore[union-attr]

        clab_labs_map: dict[str, dict] = {}

        # ── clab-api-server: authoritative source for migrated labs ──────────
        if clab_ok:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as cc:
                try:
                    metrics_r, labs_r = await asyncio.gather(
                        _clab_request(cc, agent, "GET", "/api/v1/health/metrics"),
                        _clab_request(cc, agent, "GET", "/api/v1/labs"),
                    )
                    if metrics_r.status_code < 400:
                        out["resources"] = _norm_clab_resources(metrics_r.json())
                    if labs_r.status_code < 400:
                        clab_labs = _norm_clab_labs(labs_r.json())
                        # Fetch vm-agent labs with a dedicated timeout (parallel probe
                        # uses only 4s which can be too short for `clab inspect`).
                        _vm_lab_by_name: dict[str, dict] = {}
                        try:
                            _vm_labs_r = await cc.get(
                                f"{base}/labs",
                                headers={"Authorization": f"Bearer {agent.get('token', '')}"},
                                timeout=httpx.Timeout(15.0, connect=5.0),
                            )
                            if _vm_labs_r.status_code < 400:
                                _vd = _vm_labs_r.json()
                                _vlist = _vd if isinstance(_vd, list) else _vd.get("labs", [])
                                _vm_lab_by_name = {
                                    str(l.get("name") or ""): l for l in _vlist if l.get("name")
                                }
                        except Exception as _ve:
                            logger.debug("vm-labs fetch for positions failed vm=%s: %s", out["name"], _ve)
                        await asyncio.gather(*(
                            _enrich_graph(cc, agent, lab, _vm_lab_by_name.get(lab["name"]))
                            for lab in clab_labs
                        ))
                        clab_labs_map = {lab["name"]: lab for lab in clab_labs}
                except Exception as exc:
                    logger.warning("fetch_agent_state clab-api error vm=%s: %s", out["name"], exc)

        with _state_lock:
            _clab_labs[agent_id] = set(clab_labs_map.keys())

        # ── vm-agent: fill gaps (not-yet-migrated labs, resources fallback) ──
        vm_labs_list: list[dict] = []
        if vm_ok:
            if not out["resources"] and isinstance(vm_res, httpx.Response) and vm_res.status_code < 400:
                out["resources"] = vm_res.json()
            if isinstance(vm_labs, httpx.Response) and vm_labs.status_code < 400:
                data = vm_labs.json()
                vm_labs_list = data.get("labs", []) if isinstance(data, dict) else []

        # clab-api labs take precedence (authoritative); vm-agent fills the rest
        merged: dict[str, dict] = {
            str(lab.get("name") or ""): lab
            for lab in vm_labs_list
            if lab.get("name")
        }
        merged.update(clab_labs_map)
        out["labs"] = list(merged.values())
        out["online"] = clab_ok or vm_ok
        logger.info(
            "fetch_agent_state done vm=%s online=%s clab=%s vm=%s labs=%d",
            out["name"], out["online"], clab_ok, vm_ok, len(out["labs"]),
        )
    except httpx.TimeoutException:
        out["error"] = "Agent injoignable (timeout)"
        logger.error("fetch_agent_state timeout vm=%s base=%s", out["name"], base)
    except httpx.ConnectError:
        out["error"] = "Agent injoignable (connexion refusée)"
        logger.error("fetch_agent_state connect_error vm=%s base=%s", out["name"], base)
    except Exception as e:
        out["error"] = f"Erreur inattendue: {type(e).__name__}"
        logger.exception("fetch_agent_state failed vm=%s error_type=%s", out["name"], type(e).__name__)
    return out


async def collect_all(agents):
    semaphore = asyncio.Semaphore(_agent_poll_concurrency())

    async def _bounded_fetch(agent: dict, client: httpx.AsyncClient):
        async with semaphore:
            return await fetch_agent_state(agent, client=client)

    async with httpx.AsyncClient(timeout=4.0, limits=_default_limits()) as client:
        results = await asyncio.gather(
            *(_bounded_fetch(agent, client) for agent in agents),
            return_exceptions=True,
        )
    return [r for r in results if not isinstance(r, Exception)]


# ── Action routing ─────────────────────────────────────────────────────────────

async def invoke_lab_action(agent, lab_name, action, topology_file=None, router_names=None, mode=None):
    if action not in _VM_AGENT_ONLY and _clab_available(agent) and _clab_owns_lab(agent, lab_name):
        return await _invoke_via_clab_api(agent, lab_name, action)
    return await _invoke_via_vm_agent(agent, lab_name, action, topology_file, router_names, mode)


async def _invoke_via_clab_api(agent: dict, lab_name: str, action: str) -> dict:
    """Execute a lifecycle action on clab-api-server (start / stop / restart)."""
    _map = {
        "start":   ("POST", f"/api/v1/labs/{lab_name}/start"),
        "stop":    ("POST", f"/api/v1/labs/{lab_name}/stop"),
        "restart": ("POST", f"/api/v1/labs/{lab_name}/restart"),
    }
    if action not in _map:
        logger.warning("_invoke_via_clab_api: unhandled action '%s', falling back to vm-agent", action)
        return await _invoke_via_vm_agent(agent, lab_name, action)

    method, path = _map[action]
    logger.info("invoke_lab_action(clab-api) vm=%s lab=%s action=%s", agent.get("name"), lab_name, action)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=5.0)) as client:
            resp = await _clab_request(client, agent, method, path)
            if resp.status_code >= 400:
                try:
                    error_payload: object = resp.json()
                except ValueError:
                    error_payload = resp.text
                logger.error(
                    "invoke_lab_action(clab-api) failed vm=%s lab=%s action=%s status=%s",
                    agent.get("name"), lab_name, action, resp.status_code,
                )
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error": _extract_error_text(error_payload),
                    "error_payload": error_payload,
                }
            logger.info(
                "invoke_lab_action(clab-api) success vm=%s lab=%s action=%s",
                agent.get("name"), lab_name, action,
            )
            return {"ok": True, "status_code": resp.status_code, "data": resp.json() if resp.content else {}}
    except httpx.TimeoutException:
        return {"ok": False, "status_code": 504, "error": f"Timeout clab-api {action} on {lab_name}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def _invoke_via_vm_agent(
    agent, lab_name, action, topology_file=None, router_names=None, mode=None,
) -> dict:
    """Original vm-agent action dispatch (unchanged behaviour)."""
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload: dict = {}
    if topology_file:
        payload["topology_file"] = topology_file
    if router_names:
        payload["router_names"] = router_names
    if mode:
        payload["mode"] = mode

    logger.info(
        "invoke_lab_action(vm-agent) vm=%s lab=%s action=%s mode=%s router_names=%s",
        agent.get("name"), lab_name, action, mode or "default", router_names or "all",
    )
    long_actions = {"reconfigure", "redeploy", "restart", "start", "stop", "set-default-config"}
    timeout = httpx.Timeout(900.0 if action in long_actions else 30.0, connect=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/{action}",
                headers=headers,
                json=payload,
            )
            if response.status_code >= 400:
                logger.error(
                    "invoke_lab_action(vm-agent) failed vm=%s lab=%s action=%s status=%s",
                    agent.get("name"), lab_name, action, response.status_code,
                )
                try:
                    error_payload: object = response.json()
                except ValueError:
                    error_payload = response.text
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": _extract_error_text(error_payload),
                    "error_payload": error_payload,
                }
            logger.info(
                "invoke_lab_action(vm-agent) success vm=%s lab=%s action=%s",
                agent.get("name"), lab_name, action,
            )
            return {"ok": True, "status_code": response.status_code, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.error(
            "invoke_lab_action(vm-agent) timeout vm=%s lab=%s action=%s",
            agent.get("name"), lab_name, action,
        )
        return {"ok": False, "status_code": 504, "error": f"Action timeout for {action} on lab {lab_name}"}
    except httpx.HTTPError as exc:
        logger.error(
            "invoke_lab_action(vm-agent) http_error vm=%s lab=%s action=%s error=%s",
            agent.get("name"), lab_name, action, exc,
        )
        return {"ok": False, "status_code": 502, "error": str(exc)}


# ── Downloads ──────────────────────────────────────────────────────────────────

_EXPORT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB guard


async def download_lab_export(agent, lab_name):
    """Download router config archive — always via vm-agent (SSH/SCP-based)."""
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    logger.info("download_lab_export start vm=%s lab=%s", agent.get("name"), lab_name)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=5.0)) as client:
            response = await client.get(f"{base}/labs/{lab_name}/export-config", headers=headers)
            if response.status_code >= 400:
                logger.error(
                    "download_lab_export failed vm=%s lab=%s status=%s",
                    agent.get("name"), lab_name, response.status_code,
                )
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            content = response.content
            if len(content) > _EXPORT_MAX_BYTES:
                logger.error(
                    "download_lab_export too large vm=%s lab=%s size=%d",
                    agent.get("name"), lab_name, len(content),
                )
                return {"ok": False, "status_code": 502, "error": "Export trop volumineux (100 MB max)"}
            logger.info("download_lab_export success vm=%s lab=%s", agent.get("name"), lab_name)
            return {
                "ok": True,
                "status_code": response.status_code,
                "content": content,
                "content_type": response.headers.get("content-type", "application/zip"),
                "content_disposition": response.headers.get("content-disposition", ""),
            }
    except httpx.TimeoutException:
        logger.error("download_lab_export timeout vm=%s lab=%s", agent.get("name"), lab_name)
        return {"ok": False, "status_code": 504, "error": "Timeout export: l'agent met trop de temps a repondre"}
    except httpx.HTTPError as exc:
        logger.error("download_lab_export http_error vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def download_lab_yaml(agent, lab_name):
    """Download topology YAML — uses clab-api when lab is owned by it, else vm-agent."""
    if _clab_available(agent) and _clab_owns_lab(agent, lab_name):
        return await _download_yaml_via_clab_api(agent, lab_name)
    return await _download_yaml_via_vm_agent(agent, lab_name)


async def _download_yaml_via_clab_api(agent: dict, lab_name: str) -> dict:
    logger.info("download_lab_yaml(clab-api) start vm=%s lab=%s", agent.get("name"), lab_name)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await _clab_request(client, agent, "GET", f"/api/v1/labs/{lab_name}/topology/yaml")
            if resp.status_code < 400:
                logger.info("download_lab_yaml(clab-api) success vm=%s lab=%s", agent.get("name"), lab_name)
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "content": resp.content,
                    "content_type": "application/x-yaml",
                    "content_disposition": f'attachment; filename="{lab_name}.yaml"',
                }
            logger.warning(
                "download_lab_yaml(clab-api) status=%s vm=%s lab=%s, fallback vm-agent",
                resp.status_code, agent.get("name"), lab_name,
            )
            return await _download_yaml_via_vm_agent(agent, lab_name)
    except httpx.TimeoutException:
        logger.error("download_lab_yaml(clab-api) timeout vm=%s lab=%s", agent.get("name"), lab_name)
        return {"ok": False, "status_code": 504, "error": "Export YAML timeout"}
    except httpx.HTTPError as exc:
        logger.error("download_lab_yaml(clab-api) http_error vm=%s lab=%s", agent.get("name"), lab_name)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def _download_yaml_via_vm_agent(agent: dict, lab_name: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("download_lab_yaml(vm-agent) start vm=%s lab=%s", agent.get("name"), lab_name)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{base}/labs/{lab_name}/export-yaml", headers=headers)
            if response.status_code >= 400:
                logger.error(
                    "download_lab_yaml(vm-agent) failed vm=%s lab=%s status=%s",
                    agent.get("name"), lab_name, response.status_code,
                )
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            logger.info("download_lab_yaml(vm-agent) success vm=%s lab=%s", agent.get("name"), lab_name)
            return {
                "ok": True,
                "status_code": response.status_code,
                "content": response.content,
                "content_type": response.headers.get("content-type", "application/x-yaml"),
                "content_disposition": response.headers.get("content-disposition", ""),
            }
    except httpx.TimeoutException:
        logger.error("download_lab_yaml(vm-agent) timeout vm=%s lab=%s", agent.get("name"), lab_name)
        return {"ok": False, "status_code": 504, "error": "Export YAML timeout"}
    except httpx.HTTPError as exc:
        logger.error("download_lab_yaml(vm-agent) http_error vm=%s lab=%s", agent.get("name"), lab_name)
        return {"ok": False, "status_code": 502, "error": str(exc)}


# ── vm-agent only functions (unchanged) ────────────────────────────────────────

async def fetch_reconfigure_job(agent, lab_name: str, job_id: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("fetch_reconfigure_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(f"{base}/labs/{lab_name}/reconfigure-job/{job_id}", headers=headers)
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("fetch_reconfigure_job timeout vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("fetch_reconfigure_job http_error vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def fetch_set_default_job(agent, lab_name: str, job_id: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("fetch_set_default_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(f"{base}/labs/{lab_name}/set-default-job/{job_id}", headers=headers)
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("fetch_set_default_job timeout vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("fetch_set_default_job http_error vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def cancel_reconfigure_job(agent, lab_name: str, job_id: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("cancel_reconfigure_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/reconfigure-job/{job_id}/cancel", headers=headers,
            )
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("cancel_reconfigure_job timeout vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("cancel_reconfigure_job http_error vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def cancel_set_default_job(agent, lab_name: str, job_id: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("cancel_set_default_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/set-default-job/{job_id}/cancel", headers=headers,
            )
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("cancel_set_default_job timeout vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("cancel_set_default_job http_error vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def cancel_config_diff_job(agent, lab_name: str, job_id: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("cancel_config_diff_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/config-diff-job/{job_id}/cancel", headers=headers,
            )
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("cancel_config_diff_job timeout vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("cancel_config_diff_job http_error vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def start_config_diff_job(agent, lab_name: str, router_names: list[str] | None = None) -> dict:
    """Start async config-diff job on vm-agent."""
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("start_config_diff_job vm=%s lab=%s router_names=%s", agent.get("name"), lab_name, router_names or "all")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/config-diff-async",
                headers=headers,
                json={"router_names": router_names},
            )
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("start_config_diff_job timeout vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("start_config_diff_job http_error vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def fetch_config_diff_job(agent, lab_name: str, job_id: str) -> dict:
    """Fetch async config-diff job state from vm-agent."""
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("fetch_config_diff_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(
                f"{base}/labs/{lab_name}/config-diff-job/{job_id}", headers=headers,
            )
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("fetch_config_diff_job timeout vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        logger.warning("fetch_config_diff_job http_error vm=%s job=%s error=%s", agent.get("name"), job_id, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def fetch_config_diff(agent, lab_name: str, router_names: list[str] | None = None) -> dict:
    """Fetch config diff (running vs base) from vm-agent."""
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    requested_count = len(router_names or [])
    request_timeout = max(120.0, min(600.0, 60.0 + (requested_count * 8.0)))
    logger.info("fetch_config_diff vm=%s lab=%s router_names=%s", agent.get("name"), lab_name, router_names or "all")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(request_timeout, connect=5.0)) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/config-diff",
                headers=headers,
                json={"router_names": router_names},
            )
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": response.text or f"HTTP {response.status_code} depuis vm-agent",
                }
            return {"ok": True, "data": response.json()}
    except httpx.TimeoutException as exc:
        logger.warning("fetch_config_diff timeout vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        msg = str(exc).strip() or (
            f"Timeout config-diff (> {request_timeout:.0f}s) "
            f"pour {requested_count or 'tous les'} routeur(s). "
            "Essayez un lot plus petit."
        )
        return {"ok": False, "status_code": 504, "error": msg}
    except httpx.HTTPError as exc:
        logger.warning("fetch_config_diff http_error vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {"ok": False, "status_code": 502, "error": str(exc) or "Erreur HTTP config-diff"}


async def fetch_agent_topology_inventory(agent) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    vm_id = str(agent.get("id") or "")

    logger.info("fetch_agent_topology_inventory start vm=%s base=%s", vm_id, base)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.get(f"{base}/labs/inventory", headers=headers)
            if response.status_code >= 400:
                logger.error(
                    "fetch_agent_topology_inventory failed vm=%s status=%s", vm_id, response.status_code,
                )
                return {
                    "ok": False,
                    "vm_id": vm_id,
                    "status_code": response.status_code,
                    "error": response.text or f"HTTP {response.status_code}",
                }
            data = response.json() if response.content else {}
            items = data.get("items", []) if isinstance(data, dict) else []
            return {
                "ok": True,
                "vm_id": vm_id,
                "status_code": response.status_code,
                "items": items if isinstance(items, list) else [],
            }
    except httpx.TimeoutException as exc:
        logger.error("fetch_agent_topology_inventory timeout vm=%s error=%s", vm_id, exc)
        return {"ok": False, "vm_id": vm_id, "status_code": 504, "error": "Inventory timeout"}
    except httpx.HTTPError as exc:
        logger.error("fetch_agent_topology_inventory http_error vm=%s error=%s", vm_id, exc)
        return {"ok": False, "vm_id": vm_id, "status_code": 502, "error": str(exc)}


async def submit_sandbox_yaml(
    agent,
    lab_name: str,
    yaml_text: str,
    apply: bool,
    node_positions: dict[str, dict[str, float]] | None = None,
) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    expected_management_subnet = str(agent.get("management_subnet") or "").strip() or None

    logger.info("submit_sandbox_yaml start vm=%s lab=%s apply=%s", agent.get("name"), lab_name, apply)
    try:
        validate_timeout = float(os.getenv("CENTRAL_SANDBOX_VALIDATE_TIMEOUT", "120"))
        apply_timeout = float(os.getenv("CENTRAL_SANDBOX_APPLY_TIMEOUT", "900"))
        request_timeout = apply_timeout if bool(apply) else validate_timeout

        async with httpx.AsyncClient(timeout=httpx.Timeout(request_timeout, connect=5.0)) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/sandbox-yaml",
                headers=headers,
                json={
                    "yaml_text": yaml_text,
                    "apply": bool(apply),
                    "expected_management_subnet": expected_management_subnet,
                    "node_positions": node_positions,
                },
            )
            if response.status_code >= 400:
                try:
                    data = response.json()
                    agent_detail = data.get("detail", data)
                except Exception:
                    agent_detail = {"error": response.text}
                return {"ok": False, "status_code": response.status_code, "error": agent_detail}
            return {"ok": True, "status_code": response.status_code, "data": response.json()}
    except httpx.TimeoutException:
        logger.warning(
            "submit_sandbox_yaml timeout vm=%s lab=%s apply=%s timeout=%ss",
            agent.get("name"), lab_name, bool(apply), request_timeout,
        )
        _action = "déploiement topologie" if bool(apply) else "validation"
        return {
            "ok": False,
            "status_code": 504,
            "error": f"Timeout {_action} (>{request_timeout:.0f}s) – opération peut être encore en cours sur le vm-agent",
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "error": f"Erreur réseau sandbox: {type(exc).__name__}"}


async def update_lab_topology_yaml(
    agent,
    lab_name: str,
    yaml_text: str,
    node_positions: dict[str, dict[str, float]] | None = None,
) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    expected_management_subnet = str(agent.get("management_subnet") or "").strip() or None

    logger.info("update_lab_topology_yaml start vm=%s lab=%s", agent.get("name"), lab_name)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            response = await client.put(
                f"{base}/labs/{lab_name}/topology-yaml",
                headers=headers,
                json={
                    "yaml_text": yaml_text,
                    "expected_management_subnet": expected_management_subnet,
                    "node_positions": node_positions,
                },
            )
            if response.status_code >= 400:
                try:
                    data = response.json()
                    agent_detail = data.get("detail", data)
                except Exception:
                    agent_detail = {"error": response.text}
                return {"ok": False, "status_code": response.status_code, "error": agent_detail}
            return {"ok": True, "status_code": response.status_code, "data": response.json()}
    except httpx.TimeoutException as exc:
        return {"ok": False, "status_code": 504, "error": str(exc)}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "error": str(exc)}


async def reset_sandbox_lab(agent, lab_name: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    reset_timeout = float(os.getenv("CENTRAL_SANDBOX_RESET_TIMEOUT", "900"))

    logger.info("reset_sandbox_lab start vm=%s lab=%s timeout=%ss", agent.get("name"), lab_name, reset_timeout)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(reset_timeout, connect=5.0)) as client:
            response = await client.post(f"{base}/labs/{lab_name}/sandbox-reset", headers=headers)
            if response.status_code >= 400:
                try:
                    data = response.json()
                    agent_detail = data.get("detail", data)
                except Exception:
                    agent_detail = {"error": response.text}
                return {"ok": False, "status_code": response.status_code, "error": agent_detail}
            return {"ok": True, "status_code": response.status_code, "data": response.json()}
    except httpx.TimeoutException:
        logger.warning("reset_sandbox_lab timeout vm=%s lab=%s", agent.get("name"), lab_name)
        return {
            "ok": False,
            "status_code": 504,
            "error": f"Remise à zéro sandbox timeout (>{reset_timeout:.0f}s) – vérifier l'état du lab sur le vm-agent",
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "error": f"Erreur réseau remise à zéro sandbox: {type(exc).__name__}"}


async def fetch_agent_docker_images(agent, client: httpx.AsyncClient | None = None) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    logger.info("fetch_agent_docker_images start vm=%s", agent.get("name"))
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20.0, limits=_default_limits()) as owned_client:
                return await fetch_agent_docker_images(agent, client=owned_client)

        response = await client.get(f"{base}/docker-images", headers=headers)
        if response.status_code >= 400:
            return {"ok": False, "status_code": response.status_code, "images": []}
        return {"ok": True, "images": response.json().get("images", [])}
    except httpx.TimeoutException:
        return {"ok": False, "status_code": 504, "images": []}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "images": [], "error": str(exc)}
