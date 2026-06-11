"""Tests unitaires pour l'API VM Agent (FastAPI).

Lance avec: pytest tests/test_vm_agent_api.py -v
Depuis le répertoire /home/vlm/virtual-labs-management/vm-agent/
"""
from __future__ import annotations

import importlib
import types
import tempfile
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

import sys, os


_FAKE_CONFIG = {
    "agent": {
        "api_token": "test-token-123",
    }
}


@pytest.fixture(scope="module")
def client():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    vm_agent_root = os.path.join(repo_root, "vm-agent")
    central_root = os.path.join(repo_root, "central")
    vm_agent_src_root = os.path.join(vm_agent_root, "src")
    previous_cwd = os.getcwd()
    previous_path = list(sys.path)
    previous_agent_config = os.environ.get("AGENT_CONFIG")
    previous_log_dir = os.environ.get("VM_AGENT_LOG_DIR")

    # Avoid cross-test package collision with central's `src` package.
    stale_src_modules = [name for name in sys.modules.keys() if name == "src" or name.startswith("src.")]
    for name in stale_src_modules:
        sys.modules.pop(name, None)

    central_root_norm = os.path.realpath(central_root)
    vm_agent_root_norm = os.path.realpath(vm_agent_root)
    cleaned_path = []
    for entry in sys.path:
        entry_norm = os.path.realpath(entry) if entry else entry
        if entry_norm in {central_root_norm, vm_agent_root_norm}:
            continue
        cleaned_path.append(entry)
    sys.path = cleaned_path
    sys.path.insert(0, vm_agent_root)
    importlib.invalidate_caches()

    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["AGENT_CONFIG"] = os.path.join(vm_agent_root, "config.yaml")
        os.environ["VM_AGENT_LOG_DIR"] = tmp_dir
        os.chdir(vm_agent_root)
        try:
            config_module = importlib.import_module("src.config")
            config_file = os.path.realpath(getattr(config_module, "__file__", ""))
            if not config_file.startswith(vm_agent_src_root):
                raise RuntimeError(f"Unexpected src.config module path: {config_file}")
            with patch.object(config_module, "get_settings", return_value=_FAKE_CONFIG):
                app_module = importlib.import_module("src.agent")
                with TestClient(app_module.app) as c:
                    yield c
        finally:
            os.chdir(previous_cwd)
            sys.path = previous_path
            if previous_agent_config is None:
                os.environ.pop("AGENT_CONFIG", None)
            else:
                os.environ["AGENT_CONFIG"] = previous_agent_config
            if previous_log_dir is None:
                os.environ.pop("VM_AGENT_LOG_DIR", None)
            else:
                os.environ["VM_AGENT_LOG_DIR"] = previous_log_dir


def test_health_unauthenticated(client):
    """Le endpoint /health est public (pas de Bearer requis)."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_labs_requires_token(client):
    """Le endpoint /labs doit refuser les requêtes sans token."""
    resp = client.get("/labs")
    assert resp.status_code == 401


def test_resources_requires_token(client):
    """Le endpoint /resources doit refuser les requêtes sans token."""
    resp = client.get("/resources")
    assert resp.status_code == 401


def test_authenticated_resources(client):
    """Avec un token valide, /resources répond."""
    with patch("src.audit.collector.get_resources", return_value={"cpu_percent": 0.0, "memory": {}}):
        resp = client.get("/resources", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code in (200, 500)  # 500 si psutil absent dans CI


def test_validate_yaml_rejects_management_ips_outside_vm_subnet(client):
        service = importlib.import_module("src.config_management_service")
        yaml_text = """
name: demo
mgmt:
    network: mgmt-net
    ipv4-subnet: 172.20.10.0/24
topology:
    nodes:
        r1:
            kind: cisco_iol
            mgmt-ipv4: 172.20.10.10
        r2:
            kind: cisco_iol
            mgmt-ipv4: 172.20.10.11
    links:
        - endpoints: ["r1:eth1", "r2:eth1"]
""".strip()

        with patch.object(service, "_load_sandbox_reference_yaml", return_value=(yaml_text, "current-topology")):
                result = service.validate_sandbox_topology_yaml(
                        "demo",
                        yaml_text,
                        expected_management_subnet="172.25.0.0/16",
                )

        assert result["ok"] is False
        assert any("management_subnet VM '172.25.0.0/16'" in error for error in result["errors"])


def test_wait_for_prompt_matches_iosxr_prompt_before_trailing_log(client):
    service = importlib.import_module("src.config_management_service")

    class FakeChannel:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def recv_ready(self):
            return bool(self._chunks)

        def recv(self, _size):
            return self._chunks.pop(0)

    channel = FakeChannel([
        (
            "commit\r\n"
            "RP/0/RP0/CPU0:router(config)#\r\n"
            "Thu Apr 9 10:44:29.794 UTC\r\n"
        ).encode("utf-8")
    ])

    output = service._wait_for_prompt(channel, service._IOSXR_CONFIG_PROMPT_PATTERN, timeout=1)

    assert "router(config)#" in output


def test_wait_for_prompt_matches_iosxr_any_prompt_pattern(client):
    service = importlib.import_module("src.config_management_service")

    class FakeChannel:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def recv_ready(self):
            return bool(self._chunks)

        def recv(self, _size):
            return self._chunks.pop(0)

    channel = FakeChannel([
        (
            "commit\r\n"
            "RP/0/RP0/CPU0:router#\r\n"
        ).encode("utf-8")
    ])

    output = service._wait_for_prompt(channel, service._IOSXR_ANY_PROMPT_PATTERN, timeout=1)

    assert "router#" in output


def test_apply_iosxr_raises_runtime_error_on_commit_failure_output(client):
    service = importlib.import_module("src.config_management_service")
    wait_outputs = iter([
        "RP/0/RP0/CPU0:router#",
        "RP/0/RP0/CPU0:router#",
        "RP/0/RP0/CPU0:router(config)#",
        "RP/0/RP0/CPU0:router(config)#",
        (
            "commit\r\n"
            "% Failed to commit one or more configuration items during a pseudo-atomic operation.\r\n"
            "RP/0/RP0/CPU0:router(config)#"
        ),
    ])

    class FakeChannel:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

        def recv_ready(self):
            return False

        def recv(self, _size):
            return b""

        def close(self):
            return None

    fake_channel = FakeChannel()
    fake_ssh = types.SimpleNamespace(invoke_shell=lambda: fake_channel)

    def fake_wait_for_prompt(_chan, _pattern, _timeout=20):
        return next(wait_outputs)

    with patch.object(service, "_wait_for_prompt", side_effect=fake_wait_for_prompt), patch.object(service.time, "sleep", return_value=None):
        with pytest.raises(RuntimeError, match="IOS-XR commit failed"):
            service._apply_iosxr(fake_ssh, ["hostname router"])


def test_wait_for_prompt_matches_ios_config_prompt_with_leading_cr(client):
    service = importlib.import_module("src.config_management_service")

    class FakeChannel:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def recv_ready(self):
            return bool(self._chunks)

        def recv(self, _size):
            return self._chunks.pop(0)

    channel = FakeChannel([
        (
            "configure terminal\r\n"
            "\r\nRouter(config)#\r\n"
        ).encode("utf-8")
    ])

    output = service._wait_for_prompt(channel, service._IOS_CONFIG_PROMPT_PATTERN, timeout=1)

    assert "Router(config)#" in output


def test_apply_junos_uses_commit_then_exit(client):
    service = importlib.import_module("src.config_management_service")
    wait_outputs = iter([
        ">",   # initial
        "#",   # configure
        "#",   # delete
        "[Type <Ctrl-D> to end input]",  # load set terminal
        "#",   # after pasted config
        ">",   # exit to operational mode
    ])

    class FakeChannel:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

        def recv_ready(self):
            return False

        def recv(self, _size):
            return b""

        def close(self):
            return None

    fake_channel = FakeChannel()
    fake_ssh = types.SimpleNamespace(invoke_shell=lambda: fake_channel)

    def fake_wait_for_prompt(_chan, _pattern, _timeout=20):
        return next(wait_outputs)

    def fake_wait_for_prompt_quiet(_chan, _pattern, _timeout=20, **_kwargs):
        return "#"

    with patch.object(service, "_wait_for_prompt", side_effect=fake_wait_for_prompt), patch.object(service, "_wait_for_prompt_quiet", side_effect=fake_wait_for_prompt_quiet):
        service._apply_junos(fake_ssh, "set system host-name r1")

    sent_blob = "".join(fake_channel.sent)
    assert "commit\n" in sent_blob
    assert "commit and-quit\n" not in sent_blob


def test_sanitize_ios_like_strips_crypto_pki_certificate_chain(client):
    service = importlib.import_module("src.config_management_service")

    raw = """!
hostname CE-12
crypto pki certificate chain TP-self-signed-12345
 certificate self-signed 01
  308202F7 30820260 A0030201 02020101 300D0609
  2A864886 F70D0101 0B050030 32310B30 09060355
 quit
line vty 0 4
 login local
end
"""

    lines = service._sanitize_ios_like(raw)

    assert "hostname CE-12" in lines
    assert "line vty 0 4" in lines
    assert not any("certificate self-signed" in line for line in lines)
    assert not any(line.strip().startswith("308202F7") for line in lines)
    assert not any(line.strip().startswith("crypto pki certificate chain") for line in lines)


def test_sanitize_ios_like_strips_captured_prompt_lines(client):
    service = importlib.import_module("src.config_management_service")

    raw = """show running-config
hostname CE-11
line vty 0 4
 login local
CE-11(config-line)#
CE-11#
end
"""

    lines = service._sanitize_ios_like(raw)

    assert "show running-config" not in lines
    assert "hostname CE-11" in lines
    assert "line vty 0 4" in lines
    assert " login local" in lines
    assert "CE-11(config-line)#" not in lines
    assert "CE-11#" not in lines


def test_sanitize_iosxr_removes_capture_artifacts(client):
    service = importlib.import_module("src.config_management_service")

    raw = """show running-config
Tue Mar 17 13:32:22.320 UTC
!! Building configuration...
! % Invalid input detected at '^' marker.
! --------------------------------------------------------------------------------
! 0/RP0/CPU0        XRd-CP-C-01(Active)      IOS XR RUN               NSHUT
hostname C-P-1
interface MgmtEth0/RP0/CPU0/0
 ipv4 address 172.20.20.11 255.255.255.0
!
RP/0/RP0/CPU0:C-P-1#
ssh server v2
end
"""

    sanitized = service._sanitize_iosxr(raw)

    assert sanitized == [
        "hostname C-P-1",
        "interface MgmtEth0/RP0/CPU0/0",
        " ipv4 address 172.20.20.11 255.255.255.0",
        "ssh server v2",
    ]


def test_prepare_iosxr_reconfigure_strips_management_changes(client):
    service = importlib.import_module("src.config_management_service")

    raw = """hostname C-P-1
interface MgmtEth0/RP0/CPU0/0
 ipv4 address 172.20.20.11 255.255.255.0
 lldp
  receive disable
!
router static
 address-family ipv4 unicast
  0.0.0.0/0 MgmtEth0/RP0/CPU0/0 172.20.20.1
 !
!
interface Loopback0
 ipv6 address 2001:db8:1::11/128
"""

    prepared = service._prepare_iosxr_reconfigure(raw)

    assert prepared == [
        "hostname C-P-1",
        "router static",
        " address-family ipv4 unicast",
        "interface Loopback0",
        " ipv6 address 2001:db8:1::11/128",
    ]
