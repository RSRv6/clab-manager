#!/usr/bin/env python3
"""
VLM Integration Test Suite
Teste toutes les fonctionnalités VLM contre le système en production.

Usage:
    python3 tests/vlm_integration_test.py [options]

Options:
    --url     URL du central VLM  (défaut: http://198.18.21.99)
    --user    Utilisateur VLM     (défaut: env VLM_USER ou 'ramarouc')
    --pass    Mot de passe VLM    (défaut: env VLM_PASSWORD)
    --vm      ID de VM à tester   (défaut: vm1, peut répéter)
    --lab     Nom de lab à tester (défaut: auto-découverte)
    --node    Nœud pour config-diff (défaut: premier nœud IOL/XRd trouvé)
    --skip    Tests à ignorer     (ex: --skip lifecycle --skip reconfigure)
    --only    N'exécuter que ces tests
    -v        Verbose
"""
from __future__ import annotations

import argparse
import re
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# ── Couleurs terminal ──────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
GREY   = "\033[90m"

PASS = f"{GREEN}✓ PASS{RESET}"
FAIL = f"{RED}✗ FAIL{RESET}"
SKIP = f"{YELLOW}– SKIP{RESET}"
WARN = f"{YELLOW}⚠ WARN{RESET}"


# ── Résultat d'un test ─────────────────────────────────────────────────────────
@dataclass
class Result:
    name: str
    status: str          # "pass" | "fail" | "skip" | "warn"
    message: str = ""
    detail: str = ""
    duration: float = 0.0

    @property
    def icon(self) -> str:
        return {"pass": PASS, "fail": FAIL, "skip": SKIP, "warn": WARN}[self.status]


# ── Runner ─────────────────────────────────────────────────────────────────────
class VLMTestRunner:
    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        self.results: list[Result] = []
        self.session_cookies: dict = {}
        self.vm_tokens: dict[str, str] = {}   # vm_id → token (vm-agent)
        self.clab_tokens: dict[str, str] = {}  # vm_id → JWT (clab-api)
        self.state: dict = {}                  # dernière réponse /api/state
        self._verbose = cfg.verbose

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get(self, path: str, base: str | None = None, timeout: int = 15, **kw) -> httpx.Response:
        url = (base or self.cfg.url).rstrip("/") + path
        return httpx.get(url, cookies=self.session_cookies, timeout=timeout, **kw)

    def _post(self, path: str, base: str | None = None, **kw) -> httpx.Response:
        url = (base or self.cfg.url).rstrip("/") + path
        return httpx.post(url, cookies=self.session_cookies, timeout=60, **kw)

    def _vm_agent_get(self, vm_id: str, path: str, vm_ip: str, port: int = 8081) -> httpx.Response:
        token = self.vm_tokens.get(vm_id, "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.get(f"http://{vm_ip}:{port}{path}", headers=headers, timeout=10)

    def _vm_agent_post(self, vm_id: str, path: str, vm_ip: str, port: int = 8081, **kw) -> httpx.Response:
        token = self.vm_tokens.get(vm_id, "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.post(f"http://{vm_ip}:{port}{path}", headers=headers, timeout=60, **kw)

    def _record(self, result: Result) -> Result:
        self.results.append(result)
        dur = f"{GREY}({result.duration:.1f}s){RESET}" if result.duration > 0.1 else ""
        msg = f"  {result.message}" if result.message else ""
        print(f"  {result.icon}  {result.name}{msg} {dur}")
        if self._verbose and result.detail:
            for line in result.detail.splitlines():
                print(f"      {GREY}{line}{RESET}")
        return result

    def _run(self, name: str, fn) -> Result:
        if self._should_skip(name):
            return self._record(Result(name, "skip", "skipped by --skip"))
        t0 = time.monotonic()
        try:
            r = fn()
            r.duration = time.monotonic() - t0
            return self._record(r)
        except Exception as exc:
            return self._record(Result(name, "fail", str(exc), duration=time.monotonic() - t0))

    # Tests always executed regardless of --only (prerequisites for state)
    _PREREQS = {"central_health", "central_auth", "central_state"}

    def _should_skip(self, name: str) -> bool:
        if name in self._PREREQS:
            return False   # prérequis toujours exécutés
        name_l = name.lower()
        if self.cfg.only and not any(o.lower() in name_l for o in self.cfg.only):
            return True
        return any(s.lower() in name_l for s in self.cfg.skip)

    def _section(self, title: str):
        print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

    def _vm_info(self) -> list[dict]:
        """Retourne les VMs configurées depuis /api/admin/vms."""
        try:
            r = self._get("/api/admin/vms")
            if r.status_code == 200:
                d = r.json()
                return d.get("items", [])
        except Exception:
            pass
        return []

    # ── TESTS ─────────────────────────────────────────────────────────────────

    # 1. Health du Central
    def t_central_health(self) -> Result:
        r = httpx.get(f"{self.cfg.url}/health", timeout=5)
        if r.status_code == 200 and r.json().get("status") == "ok":
            return Result("central_health", "pass", f"HTTP {r.status_code}")
        return Result("central_health", "fail", f"HTTP {r.status_code}: {r.text[:80]}")

    # 2. Authentification VLM
    def t_central_auth(self) -> Result:
        r = httpx.post(
            f"{self.cfg.url}/api/auth/login",
            json={"username": self.cfg.user, "password": self.cfg.password},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("ok"):
            self.session_cookies = dict(r.cookies)
            me = r.json().get("user", {})
            return Result("central_auth", "pass", f"Connecté en tant que {me.get('username')} ({me.get('role')})")
        return Result("central_auth", "fail", f"HTTP {r.status_code}: {r.text[:120]}")

    # 3. API /api/state
    def t_central_state(self) -> Result:
        r = self._get("/api/state")
        if r.status_code != 200:
            return Result("central_state", "fail", f"HTTP {r.status_code}")
        self.state = r.json()
        agents = self.state.get("items", self.state.get("items", []))
        online = [a for a in agents if a.get("online")]
        return Result(
            "central_state", "pass" if online else "warn",
            f"{len(online)}/{len(agents)} VMs en ligne",
            detail="\n".join(f"{a['name']}: online={a.get('online')} labs={len(a.get('labs',[]))}" for a in agents)
        )

    # 4. Santé des vm-agents
    def t_vm_agents_health(self) -> Result:
        vms = self._vm_info()
        if not vms:
            return Result("vm_agents_health", "skip", "aucune VM configurée")
        results = []
        for vm in vms:
            vm_id = vm.get("id", "")
            ip = self._ip_from_agent(vm)
            port = int(vm.get("port", 8081))
            token = vm.get("token", "")
            if token:
                self.vm_tokens[vm_id] = token
            try:
                r = httpx.get(f"http://{ip}:{port}/health",
                              headers={"Authorization": f"Bearer {token}"} if token else {},
                              timeout=4)
                status = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
            except Exception as e:
                status = f"ERR: {e}"
            results.append(f"{vm.get('name','?')} ({ip}:{port}): {status}")
        failed = [r for r in results if not r.endswith(": ok")]
        return Result(
            "vm_agents_health",
            "pass" if not failed else ("warn" if len(failed) < len(results) else "fail"),
            f"{len(results)-len(failed)}/{len(results)} agents OK",
            detail="\n".join(results),
        )

    # 5. Santé des clab-api-servers
    def t_clab_api_health(self) -> Result:
        vms = [a for a in self.state.get("items", []) if a.get("online")]
        clab_vms = []
        for vm_id in self.cfg.vms:
            for agent in self.state.get("items", []):
                if agent.get("id") == vm_id:
                    clab_vms.append(agent)
        if not clab_vms:
            return Result("clab_api_health", "skip", "aucune VM clab-api configurée")

        results = []
        for vm in clab_vms:
            vm_id = vm.get("id", "")
            ip = self._ip_from_agent(vm)
            try:
                r = httpx.get(f"http://{ip}:8080/health", timeout=4)
                status = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
            except Exception as e:
                status = f"ERR: {e}"
            results.append(f"{vm.get('name','?')} ({ip}:8080): {status}")

        failed = [r for r in results if not r.endswith(": ok")]
        return Result(
            "clab_api_health",
            "pass" if not failed else "fail",
            f"{len(results)-len(failed)}/{len(results)} clab-api OK",
            detail="\n".join(results),
        )

    # 6. Auth clab-api JWT
    def t_clab_api_auth(self) -> Result:
        if not self.cfg.clab_user or not self.cfg.clab_password:
            return Result("clab_api_auth", "skip", "pas de credentials clab-api (--clab-user/--clab-pass)")
        results = []
        for vm_id in self.cfg.vms:
            agent = next((a for a in self.state.get("items", []) if a.get("id") == vm_id), None)
            if not agent:
                continue
            ip = self._ip_from_agent(agent)
            try:
                r = httpx.post(f"http://{ip}:8080/login",
                               json={"username": self.cfg.clab_user, "password": self.cfg.clab_password},
                               timeout=10)
                if r.status_code == 200 and r.json().get("token"):
                    self.clab_tokens[vm_id] = r.json()["token"]
                    results.append(f"{vm_id}: token OK")
                else:
                    results.append(f"{vm_id}: FAIL HTTP {r.status_code}")
            except Exception as e:
                results.append(f"{vm_id}: ERR {e}")
        failed = [r for r in results if "FAIL" in r or "ERR" in r]
        return Result(
            "clab_api_auth",
            "pass" if not failed else "fail",
            f"{len(results)-len(failed)}/{len(results)} tokens obtenus",
            detail="\n".join(results),
        )

    # 7. Découverte des labs (node_count > 0)
    def t_labs_discovery(self) -> Result:
        agents = self.state.get("items", self.state.get("items", []))
        labs_found = []
        issues = []
        for agent in agents:
            if not agent.get("online"):
                continue
            for lab in agent.get("labs", []):
                nc = lab.get("node_count", 0)
                labs_found.append(f"{agent['name']}/{lab['name']}: {nc} nœuds")
                if nc == 0:
                    issues.append(f"node_count=0 sur {agent['name']}/{lab['name']}")
        if not labs_found:
            return Result("labs_discovery", "warn", "aucun lab trouvé")
        return Result(
            "labs_discovery",
            "pass" if not issues else "fail",
            f"{len(labs_found)} lab(s) trouvé(s)" + (f", {len(issues)} sans nœuds" if issues else ""),
            detail="\n".join(labs_found + (issues if issues else [])),
        )

    # 8. Ressources CPU/RAM/Disk non nulles
    def t_resources(self) -> Result:
        results = []
        issues = []
        for agent in self.state.get("items", []):
            if not agent.get("online"):
                continue
            res = agent.get("resources", {})
            cpu = res.get("cpu_percent")
            mem = (res.get("memory") or {}).get("total", 0)
            disk = (res.get("disk") or {}).get("total", 0)
            status = "ok" if cpu is not None and mem > 0 and disk > 0 else "missing"
            results.append(f"{agent['name']}: cpu={cpu}% mem={mem//1024//1024//1024}GB disk={disk//1024//1024//1024}GB [{status}]")
            if status != "ok":
                issues.append(agent['name'])
        return Result(
            "resources",
            "pass" if not issues else "warn",
            f"{len(results)-len(issues)}/{len(results)} VMs avec métriques",
            detail="\n".join(results),
        )

    # 9. Positions de graphe présentes
    def t_graph_positions(self) -> Result:
        results = []
        missing = []
        for agent in self.state.get("items", []):
            for lab in agent.get("labs", []):
                nodes = (lab.get("graph") or {}).get("nodes", [])
                positioned = [n for n in nodes if n.get("x") is not None]
                lab_name = f"{agent.get('name','?')}/{lab['name']}"
                results.append(f"{lab_name}: {len(positioned)}/{len(nodes)} nœuds positionnés")
                if nodes and not positioned:
                    missing.append(lab_name)
        return Result(
            "graph_positions",
            "pass" if not missing else "warn",
            f"{len(results)-len(missing)}/{len(results)} labs avec positions",
            detail="\n".join(results),
        )

    # 10. SSH TCP vers les nœuds IOL (port 22)
    def t_ssh_connectivity(self) -> Result:
        targets = []
        for agent in self.state.get("items", []):
            for lab in agent.get("labs", []):
                for router in lab.get("routers", []):
                    kind = router.get("kind", "")
                    if "iol" in kind or "ios" in kind:
                        ip = (router.get("mgmt_ipv4") or "").split("/")[0]
                        if ip:
                            targets.append((agent.get("name","?"), lab["name"], router.get("name","?"), ip))
        if not targets:
            return Result("ssh_connectivity", "skip", "aucun nœud IOL trouvé")

        results = []
        failed = []
        for vm_name, lab_name, node, ip in targets[:10]:  # max 10 nodes
            try:
                sock = socket.create_connection((ip, 22), timeout=3)
                sock.close()
                results.append(f"{node} ({ip}): OPEN")
            except Exception:
                results.append(f"{node} ({ip}): CLOSED")
                failed.append(node)

        checked = len(results)
        return Result(
            "ssh_connectivity",
            "pass" if not failed else ("warn" if len(failed) < checked else "fail"),
            f"{checked-len(failed)}/{checked} nœuds SSH accessibles",
            detail="\n".join(results),
        )

    # 11. Config-diff
    def t_config_diff(self) -> Result:
        vm_id, lab_name, node_name = self._pick_lab_and_node()
        if not vm_id:
            return Result("config_diff", "skip", "aucun lab/nœud adapté trouvé")

        r = self._post(
            f"/api/vms/{vm_id}/labs/{lab_name}/config-diff",
            json={"router_names": [node_name]},
        )
        if r.status_code != 200:
            return Result("config_diff", "fail", f"HTTP {r.status_code}: {r.text[:120]}")
        d = r.json()
        if not d.get("ok"):
            return Result("config_diff", "fail", str(d)[:120])
        results_list = d.get("results", [])
        matched = [x for x in results_list if x.get("ok")]
        return Result(
            "config_diff", "pass",
            f"vm={vm_id} lab={lab_name} node={node_name} — match={results_list[0].get('diff',{}).get('match','?') if results_list else '?'}",
        )

    # 12. Export-config
    def t_export_config(self) -> Result:
        vm_id, lab_name, _ = self._pick_lab_and_node()
        if not vm_id:
            return Result("export_config", "skip", "aucun lab trouvé")

        r = self._get(f"/api/vms/{vm_id}/labs/{lab_name}/export-config", timeout=120)
        if r.status_code != 200:
            return Result("export_config", "fail", f"HTTP {r.status_code}: {r.text[:120]}")
        size = len(r.content)
        ctype = r.headers.get("content-type", "")
        is_zip = "zip" in ctype or size > 100
        return Result(
            "export_config",
            "pass" if is_zip else "warn",
            f"vm={vm_id} lab={lab_name} — {size} bytes ({ctype.split('/')[1] if '/' in ctype else 'unknown'})",
        )

    # 13. Sandbox YAML
    def t_sandbox_yaml(self) -> Result:
        vm_id, lab_name, _ = self._pick_lab_and_node()
        if not vm_id:
            return Result("sandbox_yaml", "skip", "aucun lab trouvé")

        r = self._get(f"/api/vms/{vm_id}/labs/{lab_name}/sandbox-yaml")
        if r.status_code == 403:
            # Admin fallback : topology-yaml via admin endpoint
            r2 = self._get(f"/api/admin/vms/{vm_id}/labs/{lab_name}/topology-yaml")
            if r2.status_code == 200:
                d2 = r2.json()
                yaml_text = d2.get("yaml", d2.get("yaml_text", ""))
                if yaml_text:
                    return Result("sandbox_yaml", "pass",
                                  f"vm={vm_id} lab={lab_name} — {len(yaml_text)} chars YAML (via admin)")
            return Result("sandbox_yaml", "warn",
                          f"HTTP 403 — lab non assigné à l'utilisateur de test")
        if r.status_code != 200:
            return Result("sandbox_yaml", "fail", f"HTTP {r.status_code}: {r.text[:80]}")
        d = r.json()
        yaml_text = d.get("yaml_text", "")
        return Result(
            "sandbox_yaml", "pass" if yaml_text else "fail",
            f"vm={vm_id} lab={lab_name} — {len(yaml_text)} chars YAML",
        )

    # 13b. Set-default-config (sauvegarde config courante)
    def t_set_default_config(self) -> Result:
        vm_id, lab_name, node_name = self._pick_lab_and_node()
        if not vm_id:
            return Result("set_default_config", "skip", "aucun lab/nœud adapté")

        r = self._post(
            f"/api/vms/{vm_id}/labs/{lab_name}/set-default-config",
            json={"router_names": [node_name]},
        )
        if r.status_code != 200:
            return Result("set_default_config", "fail", f"HTTP {r.status_code}: {r.text[:120]}")
        d = r.json()

        # Peut être asynchrone (job_id) ou synchrone (results)
        job_id = d.get("job_id")
        if job_id:
            for _ in range(18):  # max 90s
                time.sleep(5)
                jr = self._get(f"/api/vms/{vm_id}/labs/{lab_name}/set-default-job/{job_id}")
                if jr.status_code != 200:
                    break
                jd = jr.json()
                if jd.get("status") == "completed":
                    d = jd
                    break

        success = d.get("success_count", 0)
        total   = d.get("total", 0)
        results_list = d.get("results", [])
        ok_list = [x for x in results_list if x.get("ok")]
        return Result(
            "set_default_config",
            "pass" if (success > 0 or ok_list) else "warn",
            f"vm={vm_id} lab={lab_name} node={node_name} — {success or len(ok_list)}/{total or len(results_list)} OK"
            + (f" (job {job_id[:8]})" if job_id else ""),
        )

    # 14. Topology-builder metadata
    def t_topology_builder_metadata(self) -> Result:
        r = self._get("/api/topology-builder/metadata")
        if r.status_code != 200:
            return Result("topology_builder_metadata", "fail", f"HTTP {r.status_code}")
        d = r.json()
        families = d.get("families", [])
        vms = d.get("vms", [])
        return Result(
            "topology_builder_metadata", "pass",
            f"{len(families)} familles, {len(vms)} VMs disponibles",
        )

    # 15. Reconfigure (optionnel — peut modifier les configs)
    def t_reconfigure(self) -> Result:
        vm_id, lab_name, node_name = self._pick_lab_and_node()
        if not vm_id:
            return Result("reconfigure", "skip", "aucun lab/nœud adapté")

        # Lancer le job de reconfigure
        r = self._post(
            f"/api/vms/{vm_id}/labs/{lab_name}/reconfigure",
            json={"router_names": [node_name]},
        )
        if r.status_code != 200:
            return Result("reconfigure", "fail", f"HTTP {r.status_code}: {r.text[:120]}")
        d = r.json()

        # Reconfigure asynchrone : polling du job jusqu'à completion
        job_id = d.get("job_id")
        if job_id:
            for _ in range(30):   # max 150s
                time.sleep(5)
                jr = self._get(f"/api/vms/{vm_id}/labs/{lab_name}/reconfigure-job/{job_id}")
                if jr.status_code != 200:
                    break
                jd = jr.json()
                if jd.get("status") == "completed":
                    d = jd
                    break

        results_list = d.get("results", [])
        ok_list = [x for x in results_list if x.get("ok")]
        success = d.get("success_count", len(ok_list))
        total   = d.get("total", len(results_list))
        return Result(
            "reconfigure",
            "pass" if (ok_list or success > 0) else "warn",
            f"vm={vm_id} lab={lab_name} node={node_name} — {success}/{total} OK"
            + (f" (job {job_id[:8]})" if job_id else ""),
            detail=str(results_list[0]) if results_list else str(d.get("error", "")),
        )

    # 16. Export YAML topologie
    def t_export_yaml(self) -> Result:
        vm_id, lab_name, _ = self._pick_lab_and_node()
        if not vm_id:
            return Result("export_yaml", "skip", "aucun lab trouvé")

        r = self._get(f"/api/vms/{vm_id}/labs/{lab_name}/export-yaml")
        if r.status_code != 200:
            return Result("export_yaml", "fail", f"HTTP {r.status_code}")
        return Result("export_yaml", "pass", f"vm={vm_id} lab={lab_name} — {len(r.content)} bytes")

    # 17. Inventaire topologies (admin)
    def t_topology_inventory(self) -> Result:
        r = self._get("/api/admin/topologies/inventory")
        if r.status_code != 200:
            return Result("topology_inventory", "fail", f"HTTP {r.status_code}")
        d = r.json()
        items = d.get("items", [])
        total_topos = sum(len(i.get("topologies", [])) for i in items)
        return Result(
            "topology_inventory", "pass",
            f"{len(items)} VMs, {total_topos} topologies détectées",
        )

    # ── Helper : choisir un lab et un nœud de test ────────────────────────────

    @staticmethod
    def _ip_from_agent(agent: dict) -> str:
        """Extrait l'IP depuis base_url ou ip."""
        ip = agent.get("ip", "")
        if not ip:
            base = agent.get("base_url", "")
            m = re.match(r"https?://([\d\.]+)", base)
            if m:
                ip = m.group(1)
        return ip

    def _pick_lab_and_node(self) -> tuple[str, str, str]:
        """Retourne (vm_id, lab_name, node_name) depuis le state ou les args."""
        for agent in self.state.get("items", []):
            if not agent.get("online"):
                continue
            vm_id = agent.get("id", "")
            if self.cfg.vms and vm_id not in self.cfg.vms:
                continue
            for lab in agent.get("labs", []):
                lab_name = lab.get("name", "")
                if self.cfg.lab and lab_name != self.cfg.lab:
                    continue
                for router in lab.get("routers", []):
                    kind = router.get("kind", "")
                    node_name = router.get("name", "")
                    if self.cfg.node:
                        node_name = self.cfg.node
                    if node_name:
                        return vm_id, lab_name, node_name
        return "", "", ""

    # ── EXÉCUTION ─────────────────────────────────────────────────────────────
    def run(self):
        print(f"\n{BOLD}VLM Integration Test Suite{RESET}")
        print(f"Central : {self.cfg.url}")
        print(f"User    : {self.cfg.user}")
        if self.cfg.vms:
            print(f"VMs     : {', '.join(self.cfg.vms)}")
        print()

        # Groupe 1 : Infrastructure
        self._section("1. Infrastructure")
        self._run("central_health", self.t_central_health)
        self._run("central_auth", self.t_central_auth)
        self._run("central_state", self.t_central_state)
        self._run("vm_agents_health", self.t_vm_agents_health)
        self._run("clab_api_health", self.t_clab_api_health)
        self._run("clab_api_auth", self.t_clab_api_auth)

        # Groupe 2 : Collecte de données
        self._section("2. Collecte de données")
        self._run("labs_discovery", self.t_labs_discovery)
        self._run("resources", self.t_resources)
        self._run("graph_positions", self.t_graph_positions)

        # Groupe 3 : Connectivité réseau
        self._section("3. Connectivité réseau")
        self._run("ssh_connectivity", self.t_ssh_connectivity)

        # Groupe 4 : Fonctionnalités config management
        self._section("4. Config management")
        self._run("config_diff", self.t_config_diff)
        self._run("export_config", self.t_export_config)
        self._run("export_yaml", self.t_export_yaml)
        self._run("sandbox_yaml", self.t_sandbox_yaml)
        self._run("set_default_config", self.t_set_default_config)

        # Groupe 5 : Topology builder
        self._section("5. Topology builder")
        self._run("topology_builder_metadata", self.t_topology_builder_metadata)
        self._run("topology_inventory", self.t_topology_inventory)

        # Groupe 6 : Actions lab (optionnel — modifie les configs)
        self._section("6. Actions lab (--skip reconfigure pour ignorer)")
        self._run("reconfigure", self.t_reconfigure)

        # ── Résumé ────────────────────────────────────────────────────────────
        self._print_summary()

    def _print_summary(self):
        passed  = [r for r in self.results if r.status == "pass"]
        failed  = [r for r in self.results if r.status == "fail"]
        warned  = [r for r in self.results if r.status == "warn"]
        skipped = [r for r in self.results if r.status == "skip"]
        total   = len(passed) + len(failed) + len(warned)

        print(f"\n{BOLD}── Résumé {'─' * 47}{RESET}")
        print(f"  {GREEN}{len(passed)} PASS{RESET}  "
              f"{RED}{len(failed)} FAIL{RESET}  "
              f"{YELLOW}{len(warned)} WARN{RESET}  "
              f"{GREY}{len(skipped)} SKIP{RESET}  "
              f"sur {total} tests exécutés")

        if failed:
            print(f"\n{RED}Tests en échec :{RESET}")
            for r in failed:
                print(f"  {FAIL}  {r.name}: {r.message}")

        if warned:
            print(f"\n{YELLOW}Avertissements :{RESET}")
            for r in warned:
                print(f"  {WARN}  {r.name}: {r.message}")

        # Code de sortie
        sys.exit(1 if failed else 0)


# ── CLI ────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VLM Integration Test Suite")
    p.add_argument("--url",         default=os.getenv("VLM_URL", "http://198.18.21.99"))
    p.add_argument("--user",        default=os.getenv("VLM_USER", "ramarouc"))
    p.add_argument("--password",    default=os.getenv("VLM_PASSWORD", ""))
    p.add_argument("--vm",          dest="vms", action="append", default=[],
                   metavar="VM_ID", help="VM à tester (répétable, défaut: toutes)")
    p.add_argument("--lab",         default="", help="Lab spécifique à tester")
    p.add_argument("--node",        default="", help="Nœud pour config-diff/reconfigure")
    p.add_argument("--clab-user",   default=os.getenv("CLAB_USER", "vlm-central"))
    p.add_argument("--clab-pass",   dest="clab_password",
                   default=os.getenv("CLAB_PASSWORD", "vlm-C3ntr@l-2026"))
    p.add_argument("--skip",        nargs="+", default=[], metavar="TEST",
                   help="Noms de tests à ignorer")
    p.add_argument("--only",        nargs="+", default=[], metavar="TEST",
                   help="N'exécuter que ces tests")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.password:
        print(f"{RED}Mot de passe requis : --password ou VLM_PASSWORD{RESET}")
        sys.exit(2)
    VLMTestRunner(args).run()
