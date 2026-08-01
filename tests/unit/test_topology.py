"""Unit tests for Submodule 1.2 — Enterprise Topology Generator.

Run inside the mininet-net container:
    docker compose exec -T mininet-net bash -c \
      "cd /workspace && python3 -m pytest tests/unit/test_topology.py -v"
"""

import pathlib

import pytest

from network import devices
from network.config import ConfigError, load_topology_config
from network.enterprise_topology import EnterpriseTopology

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "network" / "config" / "enterprise.yaml"


def _write_config(tmp_path, text):
    path = tmp_path / "topology.yaml"
    path.write_text(text)
    return str(path)


GOOD_CONFIG = """
name: test-net
segments:
  - id: hq
    subnets:
      - { id: subnet-a, cidr: 10.0.1.0/24, gateway: 10.0.1.1 }
      - { id: subnet-b, cidr: 10.0.2.0/24, gateway: 10.0.2.1 }
devices:
  routers:
    - name: rtr
      datapath_id: "1"
      interfaces:
        - { port: 1, name: eth1, subnet: subnet-a, ip: 10.0.1.1/24 }
  switches:
    - { name: sw-a, datapath_id: "11" }
    - { name: sw-b, datapath_id: "12" }
  hosts:
    - { name: h1, subnet: subnet-a, ip: 10.0.1.10/24, gateway: 10.0.1.1 }
    - { name: h2, subnet: subnet-b, ip: 10.0.2.10/24, gateway: 10.0.2.1 }
  servers:
    - { name: srv-1, subnet: subnet-a, ip: 10.0.1.20/24, gateway: 10.0.1.1, services: [http] }
links:
  - { src: rtr, dst: sw-a, src_port: 1, dst_port: 1 }
  - { src: rtr, dst: sw-b, src_port: 2, dst_port: 1 }
  - { src: sw-a, dst: h1 }
  - { src: sw-a, dst: srv-1 }
  - { src: sw-b, dst: h2 }
"""


# --------------------------------------------------------------------------
# Config loading + validation
# --------------------------------------------------------------------------


def test_example_config_loads():
    config = load_topology_config(EXAMPLE_CONFIG)
    assert config.name == "enterprise-demo"
    assert len(config.segments) == 1
    assert len(config.devices) == 10  # 1 router + 2 switches + 5 hosts + 2 servers
    assert len(config.links) == 9


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_topology_config(tmp_path / "nope.yaml")


def test_duplicate_device_name_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("- { name: h2,", "- { name: h1,")
    with pytest.raises(ConfigError, match="duplicate device name"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_duplicate_dpid_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace('- { name: sw-b, datapath_id: "12" }', '- { name: sw-b, datapath_id: "11" }')
    with pytest.raises(ConfigError, match="duplicate datapath_id"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_ip_outside_subnet_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("ip: 10.0.1.10/24", "ip: 10.9.9.9/24")
    with pytest.raises(ConfigError, match="outside subnet"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_unknown_subnet_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("subnet: subnet-a", "subnet: does-not-exist")
    with pytest.raises(ConfigError, match="unknown subnet"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_invalid_cidr_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("10.0.1.0/24", "not-a-cidr")
    with pytest.raises(ConfigError, match="cidr"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_link_to_unknown_device_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("- { src: sw-b, dst: h2 }", "- { src: sw-b, dst: ghost }")
    with pytest.raises(ConfigError, match="unknown device"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_host_with_two_links_rejected(tmp_path):
    extra = "  - { src: sw-b, dst: srv-1 }\n"
    cfg = GOOD_CONFIG + extra
    with pytest.raises(ConfigError, match="exactly one link"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_unconnected_host_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("  - { src: sw-a, dst: h1 }\n", "")
    with pytest.raises(ConfigError, match="not connected"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_empty_host_subnet_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace("name: h2, subnet: subnet-b", "name: h2, subnet: subnet-z")
    with pytest.raises(ConfigError, match="unknown subnet"):
        load_topology_config(_write_config(tmp_path, cfg))


def test_switch_without_dpid_rejected(tmp_path):
    cfg = GOOD_CONFIG.replace('datapath_id: "11"', "datapath_id:")
    with pytest.raises(ConfigError, match="datapath_id"):
        load_topology_config(_write_config(tmp_path, cfg))


# --------------------------------------------------------------------------
# Topology graph construction
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def topology():
    return EnterpriseTopology(load_topology_config(_load_example()))


def _load_example():
    return load_topology_config(EXAMPLE_CONFIG)


def test_node_counts():
    topo = EnterpriseTopology(load_topology_config(EXAMPLE_CONFIG))
    assert len(topo.hosts()) == 7  # 5 hosts + 2 servers
    assert len(topo.switches()) == 3  # 1 router + 2 switches


def test_device_classes():
    topo = EnterpriseTopology(load_topology_config(EXAMPLE_CONFIG))
    assert topo.nodeInfo("rtr-core")["cls"] is devices.Router
    assert topo.nodeInfo("sw-a")["cls"] is devices.Switch
    assert topo.nodeInfo("h1")["cls"] is devices.Host
    assert topo.nodeInfo("srv-web-1")["cls"] is devices.Server


def test_datapath_ids_assigned():
    topo = EnterpriseTopology(load_topology_config(EXAMPLE_CONFIG))
    assert topo.nodeInfo("rtr-core")["dpid"] == "0000000000000001"
    assert topo.nodeInfo("sw-a")["dpid"] == "0000000000000011"


def test_host_params():
    topo = EnterpriseTopology(load_topology_config(EXAMPLE_CONFIG))
    params = topo.nodeInfo("h1")
    assert params["ip"] == "10.0.1.10/24"
    assert params["defaultRoute"] == "via 10.0.1.1"


def _peers(topo, name):
    return {peer for peer, _ in topo.ports[name].values()}


def test_links_exist():
    topo = EnterpriseTopology(load_topology_config(EXAMPLE_CONFIG))
    for src, dst in [
        ("rtr-core", "sw-a"),
        ("rtr-core", "sw-b"),
        ("sw-a", "h1"),
        ("sw-a", "srv-db-1"),
        ("sw-b", "h5"),
    ]:
        assert dst in _peers(topo, src), f"missing link {src}--{dst}"


def test_configured_ports_preserved():
    topo = EnterpriseTopology(load_topology_config(EXAMPLE_CONFIG))
    assert topo.ports["rtr-core"][1] == ("sw-a", 1)
    assert topo.ports["rtr-core"][2] == ("sw-b", 1)
    assert topo.ports["sw-a"][1] == ("rtr-core", 1)
