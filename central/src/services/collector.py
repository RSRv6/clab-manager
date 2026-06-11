import logging
import httpx
import asyncio
import json
import os

logger = logging.getLogger(__name__)


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


def _agent_base_url(agent):
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

async def fetch_agent_state(agent, client: httpx.AsyncClient | None = None):
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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

        health_resp, res_resp, labs_resp = await asyncio.gather(
            client.get(f"{base}/health"),
            client.get(f"{base}/resources", headers=headers),
            client.get(f"{base}/labs", headers=headers),
        )
        health_resp.raise_for_status()
        res_resp.raise_for_status()
        labs_resp.raise_for_status()
        out["resources"] = res_resp.json()
        labs = labs_resp.json()
        out["labs"] = labs.get("labs", []) if isinstance(labs, dict) else []
        out["online"] = True
        logger.info("fetch_agent_state success vm=%s labs=%s", out["name"], len(out["labs"]))
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


async def invoke_lab_action(agent, lab_name, action, topology_file=None, router_names=None, mode=None):
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {}
    if topology_file:
        payload["topology_file"] = topology_file
    if router_names:
        payload["router_names"] = router_names
    if mode:
        payload["mode"] = mode

    logger.info(
        "invoke_lab_action start vm=%s lab=%s action=%s mode=%s router_names=%s",
        agent.get("name"),
        lab_name,
        action,
        mode or "default",
        router_names or "all",
    )
    long_actions = {"reconfigure", "redeploy", "restart", "start", "stop", "set-default-config"}
    timeout_seconds = 900.0 if action in long_actions else 30.0
    timeout = httpx.Timeout(timeout_seconds, connect=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base}/labs/{lab_name}/{action}",
                headers=headers,
                json=payload,
            )
            if response.status_code >= 400:
                logger.error(
                    "invoke_lab_action failed vm=%s lab=%s action=%s status=%s",
                    agent.get("name"),
                    lab_name,
                    action,
                    response.status_code,
                )
                error_payload: object
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = response.text

                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": _extract_error_text(error_payload),
                    "error_payload": error_payload,
                }
            logger.info("invoke_lab_action success vm=%s lab=%s action=%s", agent.get("name"), lab_name, action)
            return {
                "ok": True,
                "status_code": response.status_code,
                "data": response.json(),
            }
    except httpx.TimeoutException as exc:
        logger.error("invoke_lab_action timeout vm=%s lab=%s action=%s error=%s", agent.get("name"), lab_name, action, exc)
        return {
            "ok": False,
            "status_code": 504,
            "error": f"Action timeout for {action} on lab {lab_name}",
        }
    except httpx.HTTPError as exc:
        logger.error("invoke_lab_action http error vm=%s lab=%s action=%s error=%s", agent.get("name"), lab_name, action, exc)
        return {
            "ok": False,
            "status_code": 502,
            "error": str(exc),
        }


_EXPORT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB guard


async def download_lab_export(agent, lab_name):
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    logger.info("download_lab_export start vm=%s lab=%s", agent.get("name"), lab_name)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=5.0)) as client:
            response = await client.get(
                f"{base}/labs/{lab_name}/export-config",
                headers=headers,
            )

            if response.status_code >= 400:
                logger.error(
                    "download_lab_export failed vm=%s lab=%s status=%s",
                    agent.get("name"),
                    lab_name,
                    response.status_code,
                )
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": response.text,
                }

            content = response.content
            if len(content) > _EXPORT_MAX_BYTES:
                logger.error(
                    "download_lab_export response too large vm=%s lab=%s size=%d",
                    agent.get("name"),
                    lab_name,
                    len(content),
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
    except httpx.TimeoutException as exc:
        logger.error("download_lab_export timeout vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {
            "ok": False,
            "status_code": 504,
            "error": "Timeout export: l'agent met trop de temps a repondre",
        }
    except httpx.HTTPError as exc:
        logger.error("download_lab_export http_error vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {
            "ok": False,
            "status_code": 502,
            "error": str(exc),
        }


async def fetch_reconfigure_job(agent, lab_name: str, job_id: str) -> dict:
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info("fetch_reconfigure_job vm=%s lab=%s job=%s", agent.get("name"), lab_name, job_id)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(
                f"{base}/labs/{lab_name}/reconfigure-job/{job_id}",
                headers=headers,
            )
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
            response = await client.get(
                f"{base}/labs/{lab_name}/set-default-job/{job_id}",
                headers=headers,
            )
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
                f"{base}/labs/{lab_name}/reconfigure-job/{job_id}/cancel",
                headers=headers,
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
                f"{base}/labs/{lab_name}/set-default-job/{job_id}/cancel",
                headers=headers,
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
                f"{base}/labs/{lab_name}/config-diff-job/{job_id}/cancel",
                headers=headers,
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
                f"{base}/labs/{lab_name}/config-diff-job/{job_id}",
                headers=headers,
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
    """Fetch config diff (running vs base) from agent."""
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    requested_count = len(router_names or [])
    # Large labs are slow because the agent captures running config per router over SSH.
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
        msg = str(exc).strip() or "Erreur HTTP lors de la récupération du diff de configuration"
        return {"ok": False, "status_code": 502, "error": msg}


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
                    "fetch_agent_topology_inventory failed vm=%s status=%s",
                    vm_id,
                    response.status_code,
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


async def download_lab_yaml(agent, lab_name):
    base = _agent_base_url(agent)
    token = agent.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    logger.info("download_lab_yaml start vm=%s lab=%s", agent.get("name"), lab_name)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{base}/labs/{lab_name}/export-yaml",
                headers=headers,
            )

            if response.status_code >= 400:
                logger.error(
                    "download_lab_yaml failed vm=%s lab=%s status=%s",
                    agent.get("name"),
                    lab_name,
                    response.status_code,
                )
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": response.text,
                }

            logger.info("download_lab_yaml success vm=%s lab=%s", agent.get("name"), lab_name)
            return {
                "ok": True,
                "status_code": response.status_code,
                "content": response.content,
                "content_type": response.headers.get("content-type", "application/x-yaml"),
                "content_disposition": response.headers.get("content-disposition", ""),
            }
    except httpx.TimeoutException as exc:
        logger.error("download_lab_yaml timeout vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {"ok": False, "status_code": 504, "error": "Export YAML timeout"}
    except httpx.HTTPError as exc:
        logger.error("download_lab_yaml http_error vm=%s lab=%s error=%s", agent.get("name"), lab_name, exc)
        return {"ok": False, "status_code": 502, "error": str(exc)}


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
        # apply=True triggers destroy + redeploy and can legitimately run several minutes
        # on larger labs (especially multi-vendor images).
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
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": agent_detail,
                }
            return {
                "ok": True,
                "status_code": response.status_code,
                "data": response.json(),
            }
    except httpx.TimeoutException:
        logger.warning(
            "submit_sandbox_yaml timeout vm=%s lab=%s apply=%s timeout=%ss",
            agent.get("name"),
            lab_name,
            bool(apply),
            request_timeout,
        )
        _action = "d\u00e9ploiement topologie" if bool(apply) else "validation"
        return {"ok": False, "status_code": 504, "error": f"Timeout {_action} (>{request_timeout:.0f}s) \u2013 op\u00e9ration peut \u00eatre encore en cours sur le vm-agent"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "error": f"Erreur r\u00e9seau sandbox: {type(exc).__name__}"}


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
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": agent_detail,
                }
            return {
                "ok": True,
                "status_code": response.status_code,
                "data": response.json(),
            }
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
            response = await client.post(
                f"{base}/labs/{lab_name}/sandbox-reset",
                headers=headers,
            )
            if response.status_code >= 400:
                try:
                    data = response.json()
                    agent_detail = data.get("detail", data)
                except Exception:
                    agent_detail = {"error": response.text}
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": agent_detail,
                }
            return {
                "ok": True,
                "status_code": response.status_code,
                "data": response.json(),
            }
    except httpx.TimeoutException:
        logger.warning("reset_sandbox_lab timeout vm=%s lab=%s timeout=%ss", agent.get("name"), lab_name, reset_timeout)
        return {"ok": False, "status_code": 504, "error": f"Remise \u00e0 z\u00e9ro sandbox timeout (>{reset_timeout:.0f}s) \u2013 v\u00e9rifier l'\u00e9tat du lab sur le vm-agent"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 502, "error": f"Erreur r\u00e9seau remise \u00e0 z\u00e9ro sandbox: {type(exc).__name__}"}


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
