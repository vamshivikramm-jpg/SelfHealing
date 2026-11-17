"""Generated topologies for scalability and baseline experiments (Module 9).

``campus(n_switches, hosts_per_switch)`` builds an enterprise-style
topology: one core router linked to every access switch, plus a ring
between the access switches (so traffic has several alternative paths),
one subnet per switch, and the two application servers (``srv-web-1``,
``srv-db-1``) on the first switch so the Zero Trust and decision policies
apply unchanged.

``write_config_dir`` writes a self-contained config directory (topology,
identities, Zero Trust and decision policies) that every evaluation can
load. These topologies are SIMULATION-ONLY: like the redundant topology,
their loops would need loop-aware forwarding in Ryu on a real network.
"""

from __future__ import annotations

import os
import shutil
from typing import Dict, Tuple

import yaml

CONFIG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config"))


def _mac(ip: str) -> str:
    a, b, c, d = (int(x) for x in ip.split("."))
    return f"02:00:{a:02x}:{b:02x}:{c:02x}:{d:02x}"


def campus(n_switches: int, hosts_per_switch: int, ring: bool = True) -> Tuple[dict, dict]:
    """Return ``(topology_yaml, identities_yaml)`` dictionaries."""
    if n_switches < 2 or not 1 <= hosts_per_switch <= 200:
        raise ValueError("need >= 2 switches and 1-200 hosts per switch")
    if n_switches > 200:
        raise ValueError("at most 200 switches (one /24 subnet each)")
    subnets, interfaces, switches, hosts, servers, links = [], [], [], [], [], []
    identities: Dict[str, dict] = {}
    for k in range(1, n_switches + 1):
        third = k
        subnet = f"subnet-{k}"
        gw = f"10.{third // 256}.{third % 256}.1"
        subnets.append({"id": subnet, "cidr": f"10.{third // 256}.{third % 256}.0/24", "gateway": gw})
        interfaces.append({"port": k, "name": f"eth{k}", "subnet": subnet, "ip": f"{gw}/24"})
        sw = f"sw-{k}"
        switches.append({"name": sw, "role": "access", "datapath_id": f"{0x100 + k:016x}"})
        links.append({"src": "rtr-core", "dst": sw, "src_port": k, "dst_port": 1})
        for i in range(1, hosts_per_switch + 1):
            ip = f"10.{third // 256}.{third % 256}.{10 + i}"
            name = f"h{k}-{i}"
            hosts.append({"name": name, "subnet": subnet, "ip": f"{ip}/24", "gateway": gw})
            links.append({"src": sw, "dst": name})
            role = "admin" if (k, i) == (1, 1) else ("contractor" if k == n_switches else "employee")
            identities[name] = {"role": role, "owner": f"acct-{name}", "mac": _mac(ip),
                                "base_trust": {"admin": 0.9, "employee": 0.8, "contractor": 0.6}[role],
                                "managed": role != "contractor", "patched": True}
    for name, ip_last, role, services in (("srv-web-1", 250, "web_server", ["http", "https"]),
                                          ("srv-db-1", 251, "db_server", ["postgres"])):
        ip = f"10.0.1.{ip_last}"
        servers.append({"name": name, "subnet": "subnet-1", "ip": f"{ip}/24", "gateway": "10.0.1.1",
                        "services": services})
        links.append({"src": "sw-1", "dst": name})
        identities[name] = {"role": role, "owner": f"svc-{name}", "mac": _mac(ip), "base_trust": 0.9,
                            "managed": True, "patched": True}
    if ring:
        for k in range(1, n_switches + 1):
            nxt = k % n_switches + 1
            if n_switches == 2 and k == 2:
                break
            links.append({"src": f"sw-{k}", "dst": f"sw-{nxt}", "src_port": 100 + nxt, "dst_port": 100 + k})
    topology = {
        "name": f"campus-{n_switches}x{hosts_per_switch}",
        "version": "sim",
        "segments": [{"id": "campus", "label": "Generated campus (simulation only)", "subnets": subnets}],
        "devices": {
            "routers": [{"name": "rtr-core", "role": "core", "datapath_id": f"{1:016x}", "interfaces": interfaces}],
            "switches": switches, "hosts": hosts, "servers": servers,
        },
        "links": links,
    }
    return topology, {"devices": identities}


def write_config_dir(path: str, n_switches: int, hosts_per_switch: int, ring: bool = True) -> str:
    """Write topology + identities + copies of the policies into ``path``.

    The topology is written as ``topology.yaml``. Returns ``path``.
    """
    os.makedirs(path, exist_ok=True)
    topology, identities = campus(n_switches, hosts_per_switch, ring)
    with open(os.path.join(path, "topology.yaml"), "w") as fh:
        fh.write("# GENERATED, SIMULATION ONLY (loops need loop-aware forwarding on a real network)\n")
        yaml.safe_dump(topology, fh, sort_keys=False)
    with open(os.path.join(path, "identities.yaml"), "w") as fh:
        yaml.safe_dump(identities, fh, sort_keys=False)
    for name in ("zero_trust_policy.yaml", "decision_policy.yaml"):
        shutil.copy(os.path.join(CONFIG_DIR, name), os.path.join(path, name))
    return path
