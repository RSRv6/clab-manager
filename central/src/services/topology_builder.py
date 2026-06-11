"""
topology_builder.py — Service de construction et validation de topologies CLAB

Génère un fichier YAML containerlab valide depuis une définition de topologie
fournie par l'interface visuelle.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Métadonnées des familles de routeurs
# ---------------------------------------------------------------------------

ROUTER_FAMILIES: dict[str, dict] = {
    "iosxr": {
        "label": "Cisco IOS-XR (xrd)",
        "clab_kind": "cisco_xrd",          # canonical ContainerLab kind
        "images": [
            {"id": "iosxr",  "image": "ios-xr/xrd-control-plane:24.4.1", "label": "XRd Control Plane 24.4"},
            {"id": "iosxr",  "image": "ios-xr/xrd-control-plane:7.9.2",  "label": "XRd Control Plane 7.9"},
        ],
        "interface_prefix": "Gi",
        "interface_pattern": "Gi0-0-0-{n}",
        "mgmt_interface": "MgmtEth0/RP0/CPU0/0",
        "credentials": {"username": "clab", "password": "clab@123"},
        "max_interfaces": 32,
    },
    "junos": {
        "label": "Juniper vMX (vmx)",
        "clab_kind": "juniper_vmx",         # canonical ContainerLab kind
        "images": [
            {"id": "junos", "image": "vrnetlab/vr-vmx:24.4R1.9",  "label": "vMX 24.4"},
            {"id": "junos", "image": "vrnetlab/vr-vmx:21.4R1.12", "label": "vMX 21.4"},
        ],
        "interface_prefix": "ge",
        "interface_pattern": "ge-0/0/{n}",
        "mgmt_interface": "em0",
        "credentials": {"username": "admin", "password": "admin@123"},
        "max_interfaces": 16,
    },
    "ios": {
        "label": "Cisco IOS (iol)",
        "clab_kind": "cisco_iol",           # canonical ContainerLab kind
        "images": [
            {"id": "ios", "image": "vrnetlab/cisco-iol:17.12.01",  "label": "IOL 17.12"},
            {"id": "ios", "image": "vrnetlab/cisco-iol:15.9.3M4",  "label": "IOL 15.9"},
        ],
        "interface_prefix": "Ethernet",
        "interface_pattern": "Ethernet{slot}/{n}",
        "mgmt_interface": "Ethernet0/0",
        "credentials": {"username": "admin", "password": "admin"},
        "max_interfaces": 16,
    },
    "crpd": {
        "label": "Juniper cRPD",
        "clab_kind": "juniper_crpd",        # canonical ContainerLab kind
        "images": [
            {"id": "crpd", "image": "crpd:23.4R1.9", "label": "cRPD 23.4"},
        ],
        "interface_prefix": "eth",
        "interface_pattern": "eth{n}",
        "mgmt_interface": "eth0",
        "credentials": {"username": "root", "password": "clab123"},
        "max_interfaces": 8,
    },
    "sros": {
        "label": "Nokia SR OS (vr-sros)",
        "clab_kind": "nokia_sros",          # canonical ContainerLab kind
        "images": [
            {"id": "sros", "image": "registry.srlinux.dev/pub/vr-sros:24.10.R1", "label": "SR OS 24.10"},
            {"id": "sros", "image": "registry.srlinux.dev/pub/vr-sros:23.10.R2", "label": "SR OS 23.10"},
            {"id": "sros", "image": "vrnetlab/vr-sros:24.10.R1", "label": "vr-sros 24.10 (vrnetlab)"},
        ],
        "interface_prefix": "toPort",
        "interface_pattern": "ethernet-1/{n}",
        "interface_start": 1,
        "mgmt_interface": "A/1",
        "credentials": {"username": "admin", "password": "admin"},
        "max_interfaces": 32,
    },
    "srl": {
        "label": "Nokia SR Linux",
        "clab_kind": "nokia_srlinux",       # canonical ContainerLab kind
        "images": [
            {"id": "srl", "image": "ghcr.io/nokia/srlinux:24.10.1", "label": "SR Linux 24.10.1"},
            {"id": "srl", "image": "ghcr.io/nokia/srlinux:23.10.1", "label": "SR Linux 23.10.1"},
        ],
        "interface_prefix": "ethernet",
        "interface_pattern": "ethernet-1/{n}",
        "interface_start": 1,
        "mgmt_interface": "mgmt0",
        "credentials": {"username": "admin", "password": "NokiaSrl1!"},
        "max_interfaces": 32,
    },
    "ceos": {
        "label": "Arista cEOS",
        "clab_kind": "arista_ceos",         # canonical ContainerLab kind
        "images": [
            {"id": "ceos", "image": "ceos:4.32.0F", "label": "cEOS 4.32.0F"},
            {"id": "ceos", "image": "ceos:4.28.3M", "label": "cEOS 4.28.3M"},
        ],
        "interface_prefix": "Ethernet",
        "interface_pattern": "Ethernet{n}",
        "mgmt_interface": "Management0",
        "credentials": {"username": "admin", "password": "admin"},
        "max_interfaces": 32,
    },
    "vjunos": {
        "label": "Juniper vJunos-switch",
        "clab_kind": "juniper_vjunos-switch",  # canonical ContainerLab kind
        "images": [
            {"id": "vjunos", "image": "vrnetlab/vr-vjunos-switch:23.2R1.14", "label": "vJunos-switch 23.2"},
        ],
        "interface_prefix": "et",
        "interface_pattern": "et-0/0/{n}",
        "mgmt_interface": "em0",
        "credentials": {"username": "admin", "password": "admin@123"},
        "max_interfaces": 12,
    },
    "linux": {
        "label": "Linux",
        "clab_kind": "linux",               # canonical ContainerLab kind
        "images": [
            {"id": "linux", "image": "alpine:latest",       "label": "Alpine Linux"},
            {"id": "linux", "image": "ubuntu:22.04",        "label": "Ubuntu 22.04"},
        ],
        "interface_prefix": "eth",
        "interface_pattern": "eth{n}",
        "mgmt_interface": "eth0",
        "credentials": {"username": "root", "password": ""},
        "max_interfaces": 8,
    },
}


FAMILY_ALIASES: dict[str, str] = {
    # Nokia SR Linux
    "nokia_srlinux": "srl",
    "srlinux": "srl",
    "sr-linux": "srl",
    # Nokia SR OS
    "nokia_sros": "sros",
    "vr-sros": "sros",
    # Arista cEOS
    "arista_ceos": "ceos",
    # Juniper vJunos
    "juniper_vjunos-switch": "vjunos",
    "juniper_vjunos-router": "vjunos",
}


def _normalize_family(family_id: str) -> str:
    key = str(family_id or "").strip().lower()
    if not key:
        return ""
    return FAMILY_ALIASES.get(key, key)


def get_all_families() -> list[dict]:
    """Retourne la liste des familles de routeurs avec leurs métadonnées."""
    result = []
    for family_id, meta in ROUTER_FAMILIES.items():
        result.append({
            "id": family_id,
            "label": meta["label"],
            "clab_kind": meta["clab_kind"],
            "images": meta["images"],
            "interface_pattern": meta["interface_pattern"],
            "interface_start": meta.get("interface_start", 0),
            "mgmt_interface": meta["mgmt_interface"],
            "max_interfaces": meta["max_interfaces"],
        })
    return result


def get_interfaces_for_family(family_id: str, count: int) -> list[str]:
    """Génère la liste d'interfaces pour une famille donnée."""
    normalized = _normalize_family(family_id)
    meta = ROUTER_FAMILIES.get(normalized)
    if not meta:
        return [f"eth{i}" for i in range(count)]

    pattern = meta["interface_pattern"]
    max_ifaces = meta["max_interfaces"]
    count = min(count, max_ifaces)

    interfaces = []
    start = meta.get("interface_start", 0)
    for i in range(count):
        if "{slot}" in pattern and "{n}" in pattern:
            # IOS: Ethernet{slot}/{n} — incrémente le slot après 4 ports
            slot = i // 4
            port = i % 4
            name = pattern.replace("{slot}", str(slot)).replace("{n}", str(port))
        else:
            name = pattern.replace("{n}", str(i + start))
        interfaces.append(name)

    return interfaces


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_lab_name(name: str) -> list[str]:
    errors = []
    if not name or not name.strip():
        errors.append("Le nom du lab est obligatoire.")
    elif not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]{0,62}$', name.strip()):
        errors.append(
            "Le nom du lab ne doit contenir que des lettres, chiffres, tirets et "
            "underscores, et commencer par une lettre (max 63 caractères)."
        )
    return errors


def _validate_mgmt(mgmt_network: str, mgmt_subnet: str) -> list[str]:
    errors = []
    if not mgmt_network or not mgmt_network.strip():
        errors.append("Le nom du réseau de management est obligatoire.")

    if not mgmt_subnet or not mgmt_subnet.strip():
        errors.append("Le subnet de management est obligatoire.")
    else:
        try:
            ipaddress.IPv4Network(mgmt_subnet.strip(), strict=False)
        except ValueError:
            errors.append(f"Subnet de management invalide : '{mgmt_subnet}'. Format attendu : ex. 172.20.20.0/24")

    return errors


def _validate_nodes(nodes: list[dict]) -> list[str]:
    errors = []
    if not nodes:
        errors.append("La topologie doit contenir au moins un nœud.")
        return errors

    seen_names: set[str] = set()
    for node in nodes:
        name = (node.get("name") or "").strip()
        if not name:
            errors.append("Tous les nœuds doivent avoir un nom.")
            continue
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]{0,30}$', name):
            errors.append(
                f"Nom de nœud invalide : '{name}'. Lettres, chiffres, tirets et "
                "underscores uniquement, commençant par une lettre."
            )
        if name in seen_names:
            errors.append(f"Nom de nœud en double : '{name}'.")
        seen_names.add(name)

        raw_family = node.get("family", "")
        family = _normalize_family(raw_family)
        if family:
            node["family"] = family
        if raw_family and family not in ROUTER_FAMILIES:
            errors.append(f"Famille de routeur inconnue : '{raw_family}' pour le nœud '{name}'.")

        image = (node.get("image") or "").strip()
        if not image:
            errors.append(f"Le nœud '{name}' n'a pas d'image assignée.")

    return errors


def _validate_links(links: list[dict], node_names: set[str]) -> list[str]:
    errors = []
    seen_endpoints: set[tuple] = set()

    for i, link in enumerate(links):
        src_node = (link.get("src_node") or "").strip()
        dst_node = (link.get("dst_node") or "").strip()
        src_iface = (link.get("src_iface") or "").strip()
        dst_iface = (link.get("dst_iface") or "").strip()

        if not src_node or not dst_node:
            errors.append(f"Lien {i+1} : les nœuds source et destination sont obligatoires.")
            continue

        if src_node not in node_names:
            errors.append(f"Lien {i+1} : nœud source inconnu '{src_node}'.")
        if dst_node not in node_names:
            errors.append(f"Lien {i+1} : nœud destination inconnu '{dst_node}'.")

        if src_node == dst_node:
            errors.append(f"Lien {i+1} : un nœud ne peut pas se connecter à lui-même ('{src_node}').")

        if src_iface and dst_iface:
            ep1 = (src_node, src_iface)
            ep2 = (dst_node, dst_iface)
            if ep1 in seen_endpoints:
                errors.append(
                    f"Lien {i+1} : interface '{src_iface}' du nœud '{src_node}' déjà utilisée."
                )
            if ep2 in seen_endpoints:
                errors.append(
                    f"Lien {i+1} : interface '{dst_iface}' du nœud '{dst_node}' déjà utilisée."
                )
            seen_endpoints.add(ep1)
            seen_endpoints.add(ep2)

    return errors


def validate_topology(topology: dict) -> dict:
    """
    Valide une définition de topologie.

    Retourne {"valid": bool, "errors": [...]}
    """
    errors: list[str] = []

    lab_name   = topology.get("lab_name", "")
    mgmt_net   = topology.get("mgmt_network", "")
    mgmt_sub   = topology.get("mgmt_subnet", "")
    nodes      = topology.get("nodes") or []
    links      = topology.get("links") or []

    errors += _validate_lab_name(lab_name)
    errors += _validate_mgmt(mgmt_net, mgmt_sub)
    errors += _validate_nodes(nodes)

    node_names = {(n.get("name") or "").strip() for n in nodes if n.get("name")}
    errors += _validate_links(links, node_names)

    return {"valid": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# Génération YAML CLAB
# ---------------------------------------------------------------------------

def _auto_assign_interfaces(
    node_name: str,
    family: str,
    link_index: int,
    used: dict[str, list[str]],
) -> str:
    """Trouve la prochaine interface libre pour un nœud selon sa famille."""
    meta = ROUTER_FAMILIES.get(family, ROUTER_FAMILIES["linux"])
    pattern = meta["interface_pattern"]
    max_n = meta["max_interfaces"]

    used_list = used.get(node_name, [])
    # Réserve la première interface (management)
    for i in range(max_n):
        if "{slot}" in pattern and "{n}" in pattern:
            slot = i // 4
            port = i % 4
            name = pattern.replace("{slot}", str(slot)).replace("{n}", str(port))
        else:
            name = pattern.replace("{n}", str(i))
        if name not in used_list:
            used_list.append(name)
            used[node_name] = used_list
            return name

    # Fallback
    fallback = f"eth{link_index}"
    used.setdefault(node_name, []).append(fallback)
    return fallback


class _FlowList(list):
    """List subclass that YAML serialises as flow-style (inline)."""


class _CLabDumper(yaml.Dumper):
    pass


_CLabDumper.add_representer(
    _FlowList,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)


def _insert_section_comments(
    yaml_text: str,
    node_names: list[str],
    imported_node_count: int,
    imported_link_count: int,
) -> str:
    """Insert a comment marker before the first new node and first new link."""
    COMMENT = "# --- Nouvelles additions ---"

    # Insert before first new node (nodes are indented 4 spaces under topology.nodes)
    if 0 < imported_node_count < len(node_names):
        first_new = node_names[imported_node_count]
        pat = re.compile(
            r'^( {4})(' + re.escape(first_new) + r'):([ \t]*)$',
            re.MULTILINE,
        )
        yaml_text = pat.sub(
            lambda m: m.group(1) + COMMENT + '\n' + m.group(1) + m.group(2) + ':' + m.group(3),
            yaml_text,
            count=1,
        )

    # Insert before the first *new* link item
    if imported_link_count > 0:
        link_pat = re.compile(r'^  - endpoints:', re.MULTILINE)
        matches = list(link_pat.finditer(yaml_text))
        if imported_link_count < len(matches):
            insert_pos = matches[imported_link_count].start()
            yaml_text = yaml_text[:insert_pos] + '  ' + COMMENT + '\n' + yaml_text[insert_pos:]

    return yaml_text


def generate_clab_yaml(topology: dict) -> str:
    """
    Convertit une définition de topologie visuelle en YAML containerlab valide.

    Le `topology` dict attendu :
    {
        "lab_name":      str,              # nom du lab
        "mgmt_network":  str,              # nom du réseau de management
        "mgmt_subnet":   str,              # CIDR ex. "172.20.20.0/24"
        "nodes": [
            {
                "name":    str,            # nom du nœud
                "family":  str,            # id famille (iosxr, junos, ios, ...)
                "image":   str,            # image Docker
                "mgmt_ip": str | None,     # IP de management (optionnel)
                "startup_config": str | None,  # fichier de config initiale (optionnel)
            }, ...
        ],
        "links": [
            {
                "src_node":   str,
                "src_iface":  str | None,  # auto-assigné si None
                "dst_node":   str,
                "dst_iface":  str | None,
            }, ...
        ],
    }
    """
    lab_name    = topology["lab_name"].strip()
    mgmt_net    = topology["mgmt_network"].strip()
    mgmt_sub    = topology["mgmt_subnet"].strip()
    nodes_def   = topology.get("nodes") or []
    links_def   = topology.get("links") or []

    # Construire le dict des nœuds
    nodes_out: dict[str, Any] = {}
    node_families: dict[str, str] = {}

    for node in nodes_def:
        name   = node["name"].strip()
        family = _normalize_family((node.get("family") or "linux").strip()) or "linux"
        image  = (node.get("image") or "").strip()
        mgmt   = (node.get("mgmt_ip") or "").strip()
        cfg    = (node.get("startup_config") or "").strip()
        # Use original ContainerLab kind if available (e.g. cisco_xrd, juniper_vmx)
        # otherwise fall back to the family's canonical clab_kind, never the internal id
        _fam_meta  = ROUTER_FAMILIES.get(family, {})
        _default_kind = _fam_meta.get("clab_kind", family)
        clab_kind = (node.get("original_kind") or _default_kind).strip()
        extra  = node.get("extra_params") or {}

        node_families[name] = family

        entry: dict[str, Any] = {"kind": clab_kind, "image": image}
        if mgmt:
            entry["mgmt-ipv4"] = mgmt
        if cfg:
            entry["startup-config"] = cfg
        # Merge extra params — never override the explicitly-set fields above
        _protected = {"kind", "image", "mgmt-ipv4", "mgmt_ipv4", "startup-config", "startup_config"}
        for k, v in extra.items():
            if k not in _protected:
                entry[k] = v

        nodes_out[name] = entry

    # Construire les liens avec auto-assignation des interfaces
    used_ifaces: dict[str, list[str]] = {}
    links_out: list[dict] = []

    for i, link in enumerate(links_def):
        src_node  = link["src_node"].strip()
        dst_node  = link["dst_node"].strip()
        src_iface = (link.get("src_iface") or "").strip()
        dst_iface = (link.get("dst_iface") or "").strip()

        src_fam = node_families.get(src_node, "linux")
        dst_fam = node_families.get(dst_node, "linux")

        if not src_iface:
            src_iface = _auto_assign_interfaces(src_node, src_fam, i, used_ifaces)
        else:
            used_ifaces.setdefault(src_node, []).append(src_iface)

        if not dst_iface:
            dst_iface = _auto_assign_interfaces(dst_node, dst_fam, i, used_ifaces)
        else:
            used_ifaces.setdefault(dst_node, []).append(dst_iface)

        links_out.append({
            "endpoints": _FlowList([
                f"{src_node}:{src_iface}",
                f"{dst_node}:{dst_iface}",
            ])
        })

    imported_node_count = int(topology.get("imported_node_count") or 0)
    imported_link_count = int(topology.get("imported_link_count") or 0)

    # Re-inject passthrough links (links to external endpoints e.g. "host:eth0") right after
    # the imported links so they stay in the "imported" section of the YAML.
    for plink in (topology.get("passthrough_links") or []):
        if isinstance(plink, dict) and isinstance(plink.get("endpoints"), list):
            eps = [str(ep) for ep in plink["endpoints"] if ep]
            if len(eps) >= 2:
                insert_at = min(imported_link_count, len(links_out))
                links_out.insert(insert_at, {"endpoints": _FlowList(eps)})
                imported_link_count += 1

    # Assembler la structure CLAB
    clab_doc: dict[str, Any] = {
        "name": lab_name,
        "mgmt": {
            "network": mgmt_net,
            "ipv4-subnet": mgmt_sub,
        },
        "topology": {
            "nodes": nodes_out,
        },
    }
    if links_out:
        clab_doc["topology"]["links"] = links_out  # type: ignore[index]

    yaml_text = yaml.dump(clab_doc, default_flow_style=False, allow_unicode=True, sort_keys=False,
                          Dumper=_CLabDumper)
    if imported_node_count or imported_link_count:
        yaml_text = _insert_section_comments(
            yaml_text, list(nodes_out.keys()), imported_node_count, imported_link_count
        )
    return yaml_text
