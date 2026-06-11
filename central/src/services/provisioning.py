from __future__ import annotations

import getpass
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

_JOBS: dict[str, "ProvisionJob"] = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 50
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass
class ProvisionJob:
    job_id: str
    vm_id: str
    status: str = "pending"  # pending | running | success | error
    lines: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None


def get_job(job_id: str) -> Optional[ProvisionJob]:
    return _JOBS.get(job_id)


def list_jobs(vm_id: Optional[str] = None) -> list[dict]:
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    if vm_id:
        jobs = [j for j in jobs if j.vm_id == vm_id]
    return [job_to_dict(j) for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)]


def job_to_dict(job: ProvisionJob) -> dict:
    return _job_to_dict(job)


def _job_to_dict(job: ProvisionJob) -> dict:
    return {
        "job_id": job.job_id,
        "vm_id": job.vm_id,
        "status": job.status,
        "lines": list(job.lines[-400:]),
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def detect_default_repo_url() -> str:
    """Build an SSH-accessible git remote URL usable from remote agent hosts."""
    try:
        app_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../.."))
        result = subprocess.run(
            ["git", "-C", app_root, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        url = result.stdout.strip()
        if not url:
            return ""
        # Already an SSH or HTTPS URL → use as-is
        if not url.startswith("/"):
            return url
        # Local bare repo path → construct ssh:// URL reachable from agent hosts
        username = getpass.getuser()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("1.1.1.1", 80))
                local_ip = s.getsockname()[0]
        except Exception:
            local_ip = socket.gethostname()
        return f"ssh://{username}@{local_ip}{url}"
    except Exception:
        return ""


def _start_ssh_agent() -> tuple[dict, Optional[int]]:
    """Start a temporary ssh-agent, add the default key, and return (extra_env, agent_pid)."""
    # If a usable agent is already running in this process, use it
    existing_sock = os.environ.get("SSH_AUTH_SOCK", "")
    if existing_sock and os.path.exists(existing_sock):
        return {"SSH_AUTH_SOCK": existing_sock}, None

    try:
        result = subprocess.run(
            ["ssh-agent", "-s"], capture_output=True, text=True, timeout=5
        )
        extra_env: dict = {}
        agent_pid: Optional[int] = None
        for line in result.stdout.splitlines():
            m = re.match(r"^(SSH_[A-Z_]+)=([^;]+);", line)
            if m:
                extra_env[m.group(1)] = m.group(2)
                if m.group(1) == "SSH_AGENT_PID":
                    try:
                        agent_pid = int(m.group(2))
                    except ValueError:
                        pass
        if extra_env.get("SSH_AUTH_SOCK"):
            merged = {**os.environ, **extra_env}
            subprocess.run(["ssh-add"], env=merged, capture_output=True, timeout=10)
        return extra_env, agent_pid
    except Exception:
        return {}, None


def _stop_ssh_agent(agent_pid: Optional[int]) -> None:
    if agent_pid:
        try:
            subprocess.run(["kill", str(agent_pid)], capture_output=True, timeout=3)
        except Exception:
            pass


def _validate_repo_url(repo_url: str) -> str:
    value = str(repo_url or "").strip()
    if not value:
        raise ValueError("Le dépôt git est obligatoire")
    if any(char in value for char in ("\n", "\r", "\t")):
        raise ValueError("Le dépôt git contient des caractères invalides")
    if value.startswith("git@"):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"ssh", "https"}:
        raise ValueError("Le dépôt git doit utiliser ssh://, https:// ou git@host:repo")
    if not parsed.netloc:
        raise ValueError("Le dépôt git est invalide")
    return value


def _validate_branch(branch: str) -> str:
    value = str(branch or "").strip()
    if not value or not _BRANCH_RE.fullmatch(value) or value.startswith("/") or ".." in value:
        raise ValueError("La branche git est invalide")
    return value


def _validate_app_dir(app_dir: str) -> str:
    value = str(app_dir or "").strip()
    if not value:
        raise ValueError("Le répertoire applicatif est obligatoire")
    if not value.startswith("/"):
        raise ValueError("Le répertoire applicatif doit être absolu")
    if any(char in value for char in ("\n", "\r", "\t")):
        raise ValueError("Le répertoire applicatif est invalide")
    return value


def _validate_ssh_user(ssh_user: str) -> str:
    value = str(ssh_user or "").strip()
    if not _SSH_USER_RE.fullmatch(value):
        raise ValueError("L'utilisateur SSH est invalide")
    return value


def start_provision_job(
    vm: dict,
    ssh_user: str,
    ssh_password: Optional[str],
    sudo_password: Optional[str],
    repo_ssh_password: Optional[str],
    app_dir: str,
    branch: str,
    repo_url: str,
) -> str:
    ssh_user = _validate_ssh_user(ssh_user)
    app_dir = _validate_app_dir(app_dir)
    branch = _validate_branch(branch)
    repo_url = _validate_repo_url(repo_url)

    job_id = uuid.uuid4().hex[:12]
    job = ProvisionJob(job_id=job_id, vm_id=str(vm.get("id", "")))

    with _JOBS_LOCK:
        active_for_vm = any(
            existing.vm_id == job.vm_id and existing.status in {"pending", "running"}
            for existing in _JOBS.values()
        )
        if active_for_vm:
            raise RuntimeError(f"Un provisionnement est déjà en cours pour la VM {job.vm_id}")
        _JOBS[job_id] = job
        # Evict oldest job if limit exceeded
        if len(_JOBS) > _MAX_JOBS:
            oldest = next(iter(_JOBS))
            del _JOBS[oldest]

    t = threading.Thread(
        target=_run_provision,
        args=(job, vm, ssh_user, ssh_password, sudo_password, repo_ssh_password, app_dir, branch, repo_url),
        daemon=True,
    )
    t.start()
    return job_id


def _run_provision(
    job: ProvisionJob,
    vm: dict,
    ssh_user: str,
    ssh_password: Optional[str],
    sudo_password: Optional[str],
    repo_ssh_password: Optional[str],
    app_dir: str,
    branch: str,
    repo_url: str,
) -> None:
    job.status = "running"
    hosts_path: Optional[str] = None
    agent_pid: Optional[int] = None

    try:
        vm_id = str(vm.get("id", ""))
        vm_name = str(vm.get("name", vm_id))
        vm_ip = str(vm.get("ip", ""))
        vm_token = str(vm.get("token", ""))
        vm_port = str(vm.get("port", 8081))

        # Temp hosts file in extended format: id,name,ip,token,port,ssh_user
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="vlm_hosts_", delete=False
        ) as hf:
            hosts_path = hf.name
            hf.write(f"{vm_id},{vm_name},{vm_ip},{vm_token},{vm_port},{ssh_user}\n")

        script_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "deployment", "scripts")
        )
        deploy_script = os.path.join(script_dir, "deploy-over-ssh.sh")

        cmd = [
            "bash",
            deploy_script,
            "--role", "agent",
            "--hosts-file", hosts_path,
            "--ssh-user", ssh_user,
            "--repo-url", repo_url,
            "--branch", branch,
            "--app-dir", app_dir,
            "--app-user", ssh_user,
            "--port", vm_port,
            "--sync-central-config", "false",
            "--sync-mode", "force",
        ]

        # Passwords are passed via environment variables to avoid exposure
        # in /proc/{pid}/cmdline and `ps aux` output.
        safe_cmd = list(cmd)

        # Start a temporary ssh-agent and load the default key so that
        # SSH agent forwarding (-A) can carry the central's identity to
        # the agent VM for the git clone step.
        agent_env, agent_pid = _start_ssh_agent()
        proc_env = {**os.environ, **agent_env}
        # Passwords via env vars — never exposed in process listing.
        if ssh_password:
            proc_env["VLM_SSH_PASSWORD"] = ssh_password
        if sudo_password:
            proc_env["VLM_SUDO_PASSWORD"] = sudo_password
        if repo_ssh_password:
            proc_env["VLM_REPO_SSH_PASSWORD"] = repo_ssh_password

        job.lines.append(f"[vlm] Provisionnement VM {vm_name!r} ({vm_id}) → {vm_ip}:{vm_port}")
        job.lines.append(f"[vlm] App dir : {app_dir}  |  branch : {branch}")
        job.lines.append(f"[vlm] Dépôt  : {repo_url}")
        job.lines.append(f"[vlm] Commande : {' '.join(shlex.quote(a) for a in safe_cmd)}")
        job.lines.append("")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=proc_env,
        )

        for line in proc.stdout:
            job.lines.append(line.rstrip("\n"))

        proc.wait()
        job.lines.append("")

        if proc.returncode == 0:
            job.status = "success"
            job.lines.append("[vlm] ✓ Provisionnement terminé avec succès.")
        else:
            job.status = "error"
            job.lines.append(f"[vlm] ✗ Provisionnement échoué (code retour {proc.returncode}).")

    except Exception as exc:
        job.status = "error"
        job.lines.append(f"[vlm] Exception : {exc}")

    finally:
        job.finished_at = datetime.now(timezone.utc).isoformat()
        _stop_ssh_agent(agent_pid)
        if hosts_path:
            try:
                os.unlink(hosts_path)
            except Exception:
                pass
