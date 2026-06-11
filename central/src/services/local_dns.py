from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import threading
from typing import Any

from src.config import get_settings
from src.services.reservations import attach_reservations
from src.services import state_snapshot


logger = logging.getLogger(__name__)

_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_HOST_ALIAS_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_LOCK = threading.RLock()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _enabled() -> bool:
    raw = os.getenv("CENTRAL_LOCAL_DNS_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _domain() -> str:
    return str(os.getenv("CENTRAL_LOCAL_DNS_DOMAIN", "vlm.local") or "vlm.local").strip().strip(".").lower()


def _hosts_file_path() -> Path:
    configured = os.getenv("CENTRAL_LOCAL_DNS_HOSTS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "local_dns.hosts"


def _reload_cmd() -> list[str]:
    raw = os.getenv("CENTRAL_LOCAL_DNS_RELOAD_CMD", "").strip()
    if not raw:
        return []
    return shlex.split(raw)


def _extract_ipv4(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    candidate = value.split("/", 1)[0].strip()
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if parsed.version != 4:
        return None
    return str(parsed)


def _slug(value: str) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", str(value or "").strip().lower())
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        return "item"
    return base[:63]


def _valid_label(value: str) -> bool:
    return bool(_LABEL_RE.match(str(value or "").strip()))


def _valid_host_alias(value: str) -> bool:
    return bool(_HOST_ALIAS_RE.match(str(value or "").strip()))


def _candidate_names(vm_name: str, lab_name: str, router_name: str) -> list[str]:
    out: list[str] = []
    raw_router = str(router_name or "").strip()
    raw_lab = str(lab_name or "").strip()
    if not raw_router:
        return out

    raw_clab_name = raw_router if raw_router.lower().startswith("clab-") else (f"clab-{raw_lab}-{raw_router}" if raw_lab else "")
    if raw_clab_name and _valid_host_alias(raw_clab_name):
        out.append(raw_clab_name)
        lower = raw_clab_name.lower()
        if lower != raw_clab_name:
            out.append(lower)

    # Keep the containerlab-reported name as first-class alias.
    if _valid_host_alias(raw_router):
        out.append(raw_router)
        lower = raw_router.lower()
        if lower != raw_router:
            out.append(lower)

    vm_slug = _slug(vm_name)
    lab_slug = _slug(lab_name)
    router_slug = _slug(raw_router)
    out.append(f"{router_slug}-{lab_slug}-{vm_slug}")

    domain = _domain()
    if domain:
        fqdn_aliases = []
        for name in out:
            if _valid_host_alias(name):
                fqdn_aliases.append(f"{name}.{domain}")
        out.extend(fqdn_aliases)

    deduped: list[str] = []
    seen: set[str] = set()
    for name in out:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def _render_hosts(items_with_reservations: list[dict[str, Any]]) -> str:
    rows: dict[str, set[str]] = {}

    for vm in items_with_reservations:
        vm_name = str(vm.get("name") or vm.get("id") or "vm")
        for lab in vm.get("labs") or []:
            lab_name = str(lab.get("name") or "lab")
            for router in lab.get("routers") or []:
                ip = _extract_ipv4(router.get("mgmt_ipv4"))
                if not ip:
                    continue
                names = _candidate_names(vm_name=vm_name, lab_name=lab_name, router_name=str(router.get("name") or ""))
                if not names:
                    continue
                rows.setdefault(ip, set()).update(names)

    header = [
        "# Managed by VLM central local DNS",
        "# This file is generated automatically; do not edit manually.",
    ]
    lines = list(header)
    for ip in sorted(rows.keys(), key=lambda value: tuple(int(x) for x in value.split("."))):
        aliases = sorted(rows[ip], key=lambda name: name.lower())
        if not aliases:
            continue
        lines.append(f"{ip} {' '.join(aliases)}")
    lines.append("")
    return "\n".join(lines)


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None

    if current == content:
        try:
            path.chmod(0o644)
        except OSError:
            pass
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        tmp_path.chmod(0o644)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return True


def _reload_if_configured(changed: bool) -> None:
    if not changed:
        return
    cmd = _reload_cmd()
    if not cmd:
        return
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "DNS reload command failed")


def reconcile_from_items(items_with_reservations: list[dict[str, Any]]) -> None:
    if not _enabled():
        return

    path = _hosts_file_path()
    content = _render_hosts(items_with_reservations)
    changed = _write_if_changed(path, content)
    _reload_if_configured(changed)
    logger.info("local_dns reconciled hosts_file=%s changed=%s", path, changed)


def reconcile_now() -> None:
    if not _enabled():
        return

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(reconcile_now_async())
        return
    raise RuntimeError("reconcile_now cannot be called from async context; use reconcile_now_async")


async def reconcile_now_async() -> None:
    if not _enabled():
        return

    state = state_snapshot.get_snapshot_copy()
    if not state:
        logger.info("local_dns skipped reconcile: no snapshot available yet")
        return

    with _LOCK:
        with_reservations = attach_reservations(state)
        reconcile_from_items(with_reservations)


def _loop(interval_seconds: int) -> None:
    while not _STOP.is_set():
        try:
            reconcile_now()
        except Exception as exc:
            logger.warning("local_dns reconcile loop failed: %s", exc)
        _STOP.wait(interval_seconds)


def start_reconciler(interval_seconds: int = 20) -> None:
    global _THREAD
    if not _enabled():
        return
    if _THREAD is not None and _THREAD.is_alive():
        return

    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop,
        args=(max(10, int(interval_seconds)),),
        daemon=True,
        name="local-dns-reconciler",
    )
    _THREAD.start()


def stop_reconciler() -> None:
    global _THREAD
    _STOP.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.5)
    _THREAD = None
