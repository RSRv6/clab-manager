from __future__ import annotations

import logging
import subprocess
from typing import Any

from src.audit.collector import get_lab_details

logger = logging.getLogger(__name__)


def _run_command(cmd: list[str], timeout: int = 120) -> dict[str, Any]:
    logger.info("run_command start cmd=%s", " ".join(cmd))
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("run_command timeout after %ss cmd=%s", timeout, " ".join(cmd))
        return {
            "ok": False,
            "returncode": 124,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "command": " ".join(cmd),
        }
    if process.returncode == 0:
        logger.info("run_command success cmd=%s", " ".join(cmd))
    else:
        logger.error(
            "run_command failed rc=%s cmd=%s stderr=%s",
            process.returncode,
            " ".join(cmd),
            process.stderr.strip(),
        )
    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
        "command": " ".join(cmd),
    }


def start_lab(lab_name: str, topology_file: str | None = None, reconfigure: bool = False) -> dict[str, Any]:
    logger.info("start_lab lab=%s topology=%s reconfigure=%s", lab_name, topology_file, reconfigure)
    topology = topology_file
    if not topology:
        details = get_lab_details(lab_name)
        topology = details.get("topology_file") if details else None

    if not topology:
        return {
            "ok": False,
            "error": "topology_file is required when lab is not currently inspectable",
        }

    cmd = ["containerlab", "deploy", "-t", topology]
    if reconfigure:
        cmd.append("--reconfigure")
    return _run_command(cmd, timeout=600)


def stop_lab(lab_name: str, cleanup: bool = True) -> dict[str, Any]:
    logger.info("stop_lab lab=%s cleanup=%s", lab_name, cleanup)
    cmd = ["containerlab", "destroy", "--name", lab_name, "-y"]
    if cleanup:
        cmd.append("--cleanup")
    return _run_command(cmd)


def stop_lab_by_topology(topology_file: str, cleanup: bool = True) -> dict[str, Any]:
    logger.info("stop_lab_by_topology topology=%s cleanup=%s", topology_file, cleanup)
    cmd = ["containerlab", "destroy", "-t", topology_file, "-y"]
    if cleanup:
        cmd.append("--cleanup")
    return _run_command(cmd)


def restart_lab(lab_name: str, topology_file: str | None = None) -> dict[str, Any]:
    logger.info("restart_lab lab=%s topology=%s", lab_name, topology_file)
    stop_result = stop_lab(lab_name, cleanup=False)
    if not stop_result.get("ok"):
        return {**stop_result, "stage": "stop"}
    start_result = start_lab(lab_name, topology_file=topology_file)
    return {
        "ok": start_result.get("ok"),
        "stop": stop_result,
        "start": start_result,
    }


def redeploy_lab(lab_name: str, topology_file: str | None = None) -> dict[str, Any]:
    logger.info("redeploy_lab lab=%s topology=%s", lab_name, topology_file)
    stop_result = stop_lab(lab_name, cleanup=False)
    if not stop_result.get("ok"):
        return {**stop_result, "stage": "stop"}
    start_result = start_lab(lab_name, topology_file=topology_file)
    return {
        "ok": start_result.get("ok"),
        "stop": stop_result,
        "start": start_result,
    }