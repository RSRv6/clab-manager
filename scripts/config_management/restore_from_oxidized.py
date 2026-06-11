#!/usr/bin/env python3
import argparse
import getpass
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple


try:
    import paramiko
except Exception:
    print("[ERROR] Le module 'paramiko' est requis. Installe-le avec: pip install paramiko")
    sys.exit(1)


SUPPORTED_MODELS = {
    "ios": "ios",
    "iosxe": "ios",
    "iosxr": "iosxr",
    "junos": "junos",
}


@dataclass
class Node:
    name: str
    model: str
    ip: Optional[str]


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "oxidized-restore-script/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} sur {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Erreur réseau sur {url}: {exc}") from exc


def fetch_nodes(base_url: str) -> List[Node]:
    raw = http_get(f"{base_url.rstrip('/')}/nodes.json")
    data = json.loads(raw.decode("utf-8", errors="replace"))
    nodes: List[Node] = []
    for entry in data:
        name = str(entry.get("name", "")).strip()
        model = str(entry.get("model", "")).strip().lower()
        ip = entry.get("ip")
        if not name or not model:
            continue
        nodes.append(Node(name=name, model=model, ip=ip))
    return nodes


def fetch_config(base_url: str, node_name: str) -> str:
    encoded = urllib.parse.quote(node_name, safe="")
    raw = http_get(f"{base_url.rstrip('/')}/node/fetch/{encoded}")
    return raw.decode("utf-8", errors="replace")


def resolve_host(node: Node, prefer_ip: bool) -> str:
    if prefer_ip and node.ip:
        return str(node.ip)
    return node.name


def normalize_model(model: str) -> Optional[str]:
    m = model.lower()
    for key, family in SUPPORTED_MODELS.items():
        if m == key:
            return family
    return None


def credentials_for_node(node: Node, family: str, args: argparse.Namespace, generic_password: Optional[str]) -> Tuple[str, str]:
    if args.lab_credentials:
        upper = node.name.upper()
        if upper.startswith("CE"):
            return "admin", "admin"
        if upper.startswith("J-"):
            return "admin", "admin@123"
        if upper.startswith("C-"):
            return "clab", "clab@123"
        if family == "junos":
            return "admin", "admin@123"
        if family == "iosxr":
            return "clab", "clab@123"
        if family == "ios":
            return "admin", "admin"

    if not args.username:
        raise RuntimeError("Aucun username fourni (utilise --username ou --lab-credentials)")
    if generic_password is None:
        raise RuntimeError("Aucun password fourni (utilise --password ou laisse le prompt)")
    return args.username, generic_password


def sanitize_ios_like_config(config: str) -> List[str]:
    lines = config.splitlines()
    out: List[str] = []
    skip_prefix = (
        "Building configuration",
        "Current configuration",
        "! Last configuration",
        "end",
    )
    for line in lines:
        stripped = line.rstrip("\r")
        if not stripped:
            continue
        if stripped.startswith(skip_prefix):
            continue
        if stripped.strip() == "!":
            continue
        if stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def sanitize_iosxr_config(config: str) -> List[str]:
    lines = config.splitlines()
    out: List[str] = []
    skip_prefix = (
        "Building configuration",
        "Current configuration",
        "!!",
        "! %",
        "! ---",
        "% Invalid input",
    )
    for line in lines:
        stripped = line.rstrip("\r")
        plain = stripped.strip()
        if not stripped:
            continue
        if plain == "^":
            continue
        if plain == "end":
            continue
        if plain.startswith("0/RP0/CPU0"):
            continue
        if stripped.startswith(skip_prefix):
            continue
        out.append(stripped)
    return out


def sanitize_junos_set_config(config: str) -> List[str]:
    lines = config.splitlines()
    set_lines = [ln.strip() for ln in lines if ln.strip().startswith("set ")]
    if not set_lines:
        raise ValueError("Config JunOS non au format 'set ...'. Impossible d'appliquer automatiquement en mode sûr.")
    return set_lines


def sanitize_junos_hier_config(config: str) -> List[str]:
    out: List[str] = []
    for raw_line in config.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        if line.lstrip().startswith("##"):
            continue
        out.append(line)
    if not out:
        raise ValueError("Config JunOS hiérarchique vide")
    return out


def prepare_junos_payload(config: str) -> Tuple[str, List[str]]:
    lines = config.splitlines()
    set_lines = [ln.strip() for ln in lines if ln.strip().startswith("set ")]
    if set_lines:
        return "set", set_lines
    return "hier", sanitize_junos_hier_config(config)


def wait_for_prompt(chan: paramiko.Channel, pattern: str, timeout: int = 20) -> str:
    end_time = time.time() + timeout
    buf = ""
    rx = re.compile(pattern)
    while time.time() < end_time:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="ignore")
            buf += chunk
            if rx.search(buf):
                return buf
        else:
            time.sleep(0.1)
    raise TimeoutError(f"Timeout en attente du prompt regex={pattern}. Buffer:\n{buf[-800:]}")


def send_line(chan: paramiko.Channel, cmd: str) -> None:
    chan.send(cmd + "\n")


def apply_ios(ssh: paramiko.SSHClient, cfg_lines: List[str], save: bool) -> None:
    chan = ssh.invoke_shell()
    config_prompt = r"\([^)]*\)#\s*$"
    wait_for_prompt(chan, r"[>#]\s*$", timeout=20)
    send_line(chan, "terminal length 0")
    wait_for_prompt(chan, r"[>#]\s*$", timeout=10)
    send_line(chan, "configure terminal")
    wait_for_prompt(chan, config_prompt, timeout=10)
    for line in cfg_lines:
        send_line(chan, line)
        wait_for_prompt(chan, config_prompt, timeout=8)
    send_line(chan, "end")
    wait_for_prompt(chan, r"[>#]\s*$", timeout=10)
    if save:
        send_line(chan, "write memory")
        wait_for_prompt(chan, r"[>#]\s*$", timeout=30)
    chan.close()


def apply_iosxr(ssh: paramiko.SSHClient, cfg_lines: List[str]) -> None:
    chan = ssh.invoke_shell()
    config_prompt = r"\([^)]*\)#\s*$"
    wait_for_prompt(chan, r"[#>]\s*$", timeout=20)
    send_line(chan, "terminal length 0")
    wait_for_prompt(chan, r"[#>]\s*$", timeout=10)
    send_line(chan, "configure terminal")
    wait_for_prompt(chan, config_prompt, timeout=10)
    send_line(chan, "load merge terminal")
    time.sleep(0.5)
    while chan.recv_ready():
        chan.recv(65535)
    for line in cfg_lines:
        chan.send(line + "\n")
    chan.send("\x04")
    wait_for_prompt(chan, config_prompt, timeout=120)
    send_line(chan, "commit")
    commit_out = wait_for_prompt(chan, config_prompt, timeout=180)
    if re.search(r"(failed|error|aborted|cannot|invalid)", commit_out, flags=re.IGNORECASE):
        raise RuntimeError(f"Commit IOS-XR en erreur: {commit_out[-600:]}")
    send_line(chan, "end")
    wait_for_prompt(chan, r"[#>]\s*$", timeout=10)
    chan.close()


def apply_junos(ssh: paramiko.SSHClient, mode: str, payload_lines: List[str]) -> None:
    chan = ssh.invoke_shell()
    wait_for_prompt(chan, r"[>%] ?$", timeout=20)
    send_line(chan, "configure")
    wait_for_prompt(chan, r"# ?$", timeout=10)
    if mode == "set":
        send_line(chan, "load set terminal")
    else:
        send_line(chan, "load override terminal")
    wait_for_prompt(chan, r"\[Type .*\]", timeout=30)
    for line in payload_lines:
        chan.send(line + "\n")
    chan.send("\x04")  # Ctrl-D
    wait_for_prompt(chan, r"# ?$", timeout=180)
    send_line(chan, "commit and-quit")
    wait_for_prompt(chan, r"[>%] ?$", timeout=420)
    chan.close()


def connect_ssh(host: str, port: int, username: str, password: str, timeout: int) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        auth_timeout=timeout,
        banner_timeout=timeout,
    )
    return ssh


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restaure des configurations depuis Oxidized vers les équipements (IOS/IOS-XR/JunOS)."
    )
    parser.add_argument("--oxidized-url", default="http://127.0.0.1:9090", help="URL Oxidized (ex: http://127.0.0.1:9090)")
    parser.add_argument("--username", help="Username SSH des équipements (mode générique)")
    parser.add_argument("--password", help="Password SSH (si absent, prompt)")
    parser.add_argument("--lab-credentials", action="store_true", help="Utilise les credentials LAB: J-*=admin/admin@123, C-*=clab/clab@123, CE*=admin/admin")
    parser.add_argument("--port", type=int, default=22, help="Port SSH (défaut 22)")
    parser.add_argument("--include", nargs="*", default=[], help="Liste d'équipements à inclure (nom exact Oxidized)")
    parser.add_argument("--exclude", nargs="*", default=[], help="Liste d'équipements à exclure")
    parser.add_argument("--prefer-ip", action="store_true", help="Utiliser l'IP fournie par Oxidized au lieu du hostname")
    parser.add_argument("--apply", action="store_true", help="Appliquer réellement (sinon dry-run)")
    parser.add_argument("--save-ios", action="store_true", help="Sauvegarder la conf sur IOS/IOS-XE via write memory")
    parser.add_argument("--timeout", type=int, default=20, help="Timeout SSH (secondes)")

    args = parser.parse_args()

    generic_password: Optional[str] = args.password
    if args.apply and not args.lab_credentials:
        if not args.username:
            print("[ERROR] --username est requis sans --lab-credentials")
            return 2
        if generic_password is None:
            generic_password = getpass.getpass("Mot de passe SSH: ")

    try:
        nodes = fetch_nodes(args.oxidized_url)
    except Exception as exc:
        print(f"[ERROR] Impossible de lire les nœuds Oxidized: {exc}")
        return 2

    include_set = set(args.include)
    exclude_set = set(args.exclude)

    selected: List[Node] = []
    for node in nodes:
        if include_set and node.name not in include_set:
            continue
        if node.name in exclude_set:
            continue
        if normalize_model(node.model) is None:
            print(f"[SKIP] {node.name}: modèle non supporté ({node.model})")
            continue
        selected.append(node)

    if not selected:
        print("[INFO] Aucun équipement à traiter.")
        return 0

    print(f"[INFO] Équipements sélectionnés: {len(selected)}")
    print(f"[INFO] Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"[INFO] Auth: {'LAB-CREDENTIALS' if args.lab_credentials else 'GENERIC'}")

    ok = 0
    fail = 0

    for node in selected:
        family = normalize_model(node.model)
        host = resolve_host(node, args.prefer_ip)
        print(f"\n=== {node.name} ({node.model}) -> {host} ===")

        try:
            cfg = fetch_config(args.oxidized_url, node.name)
            if not cfg.strip():
                raise RuntimeError("configuration vide depuis Oxidized")
        except Exception as exc:
            print(f"[FAIL] Récupération config: {exc}")
            fail += 1
            continue

        try:
            junos_mode = "set"
            if family == "junos":
                junos_mode, payload = prepare_junos_payload(cfg)
            elif family == "iosxr":
                payload = sanitize_iosxr_config(cfg)
            else:
                payload = sanitize_ios_like_config(cfg)
            if not payload:
                raise RuntimeError("aucune ligne de configuration exploitable")
            if family == "junos":
                print(f"[INFO] Lignes prêtes: {len(payload)} (mode {junos_mode})")
            else:
                print(f"[INFO] Lignes prêtes: {len(payload)}")
        except Exception as exc:
            print(f"[FAIL] Préparation config: {exc}")
            fail += 1
            continue

        if not args.apply:
            print("[DRY-RUN] Aucune commande envoyée.")
            ok += 1
            continue

        ssh = None
        try:
            username, password = credentials_for_node(node, family or "", args, generic_password)
            print(f"[INFO] Login utilisé: {username}")
            ssh = connect_ssh(host=host, port=args.port, username=username, password=password, timeout=args.timeout)
            if family == "ios":
                apply_ios(ssh, payload, save=args.save_ios)
            elif family == "iosxr":
                apply_iosxr(ssh, payload)
            elif family == "junos":
                apply_junos(ssh, junos_mode, payload)
            else:
                raise RuntimeError(f"famille non gérée: {family}")
            print("[OK] Configuration appliquée")
            ok += 1
        except (socket.error, paramiko.SSHException, TimeoutError, RuntimeError, ValueError) as exc:
            print(f"[FAIL] Application: {exc}")
            fail += 1
        finally:
            if ssh is not None:
                ssh.close()

    print(f"\nRésultat: OK={ok} FAIL={fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
