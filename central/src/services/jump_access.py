from __future__ import annotations

import ipaddress
import logging
import os
import pwd
import shlex
import subprocess
import threading
from typing import Any

from src.services.auth import list_users
from src.services import local_dns
from src.services import state_snapshot
from src.services.reservations import attach_reservations

logger = logging.getLogger(__name__)

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()


def _enabled() -> bool:
    raw = os.getenv("CENTRAL_JUMP_FILTER_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _nft_cmd() -> list[str]:
    configured = os.getenv("CENTRAL_NFT_COMMAND", "nft").strip()
    return shlex.split(configured) if configured else ["nft"]


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


def _active_non_admin_users() -> list[str]:
    users = list_users()
    out: list[str] = []
    for user in users:
        if not bool(user.get("active", True)):
            continue
        if str(user.get("role") or "").lower() == "admin":
            continue
        username = str(user.get("username") or "").strip().lower()
        if username:
            out.append(username)
    return sorted(set(out))


def _uid_by_username(usernames: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for username in usernames:
        try:
            out[username] = int(pwd.getpwnam(username).pw_uid)
        except KeyError:
            # Local account may not exist yet; skip until sync creates it.
            continue
    return out


def _gather_router_ips(items: list[dict[str, Any]]) -> set[str]:
    router_ips: set[str] = set()
    for vm in items:
        for lab in vm.get("labs") or []:
            for router in lab.get("routers") or []:
                ip = _extract_ipv4(router.get("mgmt_ipv4"))
                if ip:
                    router_ips.add(ip)
    return router_ips


def _gather_allowed_ips_by_user(items: list[dict[str, Any]]) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for vm in items:
        for lab in vm.get("labs") or []:
            reservation = lab.get("reservation") or {}
            owner = str(reservation.get("owner_username") or "").strip().lower()
            if not owner:
                continue

            for router in lab.get("routers") or []:
                ip = _extract_ipv4(router.get("mgmt_ipv4"))
                if not ip:
                    continue
                allowed.setdefault(owner, set()).add(ip)
    return allowed


def _format_set_elements(ips: set[str]) -> str:
    if not ips:
        return ""
    return " elements = { " + ", ".join(sorted(ips)) + " }"


def _render_rules(router_ips: set[str], user_uid: dict[str, int], allowed_by_user: dict[str, set[str]]) -> str:
    lines: list[str] = [
        "table inet vlm_jump {",
        f"  set routers_all {{ type ipv4_addr; flags interval;{_format_set_elements(router_ips)} }}",
    ]

    for username, uid in sorted(user_uid.items(), key=lambda item: item[1]):
        allowed = allowed_by_user.get(username, set())
        lines.append(f"  set user_{uid} {{ type ipv4_addr; flags interval;{_format_set_elements(allowed)} }}")

    lines.extend(
        [
            "  chain output {",
            "    type filter hook output priority 0; policy accept;",
            "    ct state established,related accept",
            "    meta skuid 0 accept",
        ]
    )

    for username, uid in sorted(user_uid.items(), key=lambda item: item[1]):
        lines.append(f"    meta skuid {uid} ip daddr @user_{uid} accept")
        lines.append(f"    meta skuid {uid} ip daddr @routers_all reject with icmpx type admin-prohibited")

    lines.extend(["  }", "}"])
    return "\n".join(lines) + "\n"


def _apply_rules(content: str) -> None:
    # Replace only the dedicated table used by VLM jump filtering.
    cmd = _nft_cmd()
    subprocess.run([*cmd, "delete", "table", "inet", "vlm_jump"], capture_output=True, text=True, check=False)

    result = subprocess.run([*cmd, "-f", "-"], input=content, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "nft apply failed").strip())


def reconcile_from_items(items_with_reservations: list[dict[str, Any]]) -> None:
    if not _enabled():
        return

    users = _active_non_admin_users()
    uid_map = _uid_by_username(users)
    router_ips = _gather_router_ips(items_with_reservations)
    allowed_by_user = _gather_allowed_ips_by_user(items_with_reservations)

    rendered = _render_rules(router_ips=router_ips, user_uid=uid_map, allowed_by_user=allowed_by_user)
    _apply_rules(rendered)
    try:
        local_dns.reconcile_from_items(items_with_reservations)
    except Exception as exc:
        logger.warning("local_dns reconcile failed: %s", exc)
    logger.info(
        "jump_access reconciled users=%s routers=%s reserved_users=%s",
        len(uid_map),
        len(router_ips),
        len([u for u, ips in allowed_by_user.items() if ips]),
    )


def reconcile_now() -> None:
    if not _enabled():
        return

    with _LOCK:
        state = state_snapshot.get_snapshot_copy()
        if not state:
            logger.info("jump_access skipped reconcile: no snapshot available yet")
            return
        with_reservations = attach_reservations(state)
        reconcile_from_items(with_reservations)


def _loop(interval_seconds: int) -> None:
    while not _STOP.is_set():
        try:
            reconcile_now()
        except Exception as exc:
            logger.warning("jump_access reconcile loop failed: %s", exc)
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
        name="jump-access-reconciler",
    )
    _THREAD.start()


def stop_reconciler() -> None:
    global _THREAD
    _STOP.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.5)
    _THREAD = None
