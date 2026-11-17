"""Integration tests for Submodule 1.2 — end-to-end connectivity.

Requires a running stack (mininet-net + ryu-controller):
    make net-test

Verifies: switches connect to the Ryu controller, hosts/servers in the
same subnet reach each other (L2 learning switch), and learning flows
are actually installed on the data plane.
"""

import itertools
import pathlib
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"
PING_COUNT = 2
PING_WAIT = 1


def _wait_connected(net, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(s.connected() for s in net.switches):
            return
        time.sleep(0.5)
    names = [s.name for s in net.switches if not s.connected()]
    pytest.fail(f"switches not connected to controller: {names}")


def _ping(host, dst_ip):
    out = host.cmd(f"ping -c {PING_COUNT} -W {PING_WAIT} {dst_ip}")
    return "0% packet loss" in out


@pytest.fixture(scope="module")
def net():
    from mininet.clean import cleanup

    from network.network_builder import NetworkBuilder

    builder = NetworkBuilder(config_path=str(CONFIG))
    network = builder.build()
    network.start()
    _wait_connected(network)
    yield network
    try:
        network.stop()
    finally:
        cleanup()


@pytest.fixture(scope="module")
def hosts_by_subnet(net):
    # the topology config lives on the builder/YAML, not on the Mininet object
    from network.config import load_topology_config

    mapping = {}
    for dev in load_topology_config(CONFIG).host_devices:
        mapping.setdefault(dev.subnet, []).append(dev)
    return mapping


def test_all_switches_connected_to_controller(net):
    assert all(s.connected() for s in net.switches)


def test_expected_node_counts(net):
    assert len(net.hosts) == 7
    assert len(net.switches) == 3


def test_same_subnet_host_ping(net, hosts_by_subnet):
    h1 = net.get("h1")
    h2 = net.get("h2")
    assert _ping(h1, "10.0.1.11"), "h1 -> h2 ping failed"


def test_host_to_server_ping(net):
    h1 = net.get("h1")
    assert _ping(h1, "10.0.1.20"), "h1 -> srv-web-1 ping failed"
    assert _ping(h1, "10.0.1.21"), "h1 -> srv-db-1 ping failed"


def test_all_pairs_reach_each_other(net, hosts_by_subnet):
    failures = []
    for subnet, devs in hosts_by_subnet.items():
        for a, b in itertools.combinations(devs, 2):
            if a.subnet != b.subnet:
                continue
            src = net.get(a.name)
            if not _ping(src, b.ip.split("/")[0]):
                failures.append(f"{a.name} -> {b.name} ({subnet})")
    assert not failures, f"unreachable pairs: {failures}"


def test_learning_flows_installed_after_traffic(net):
    h1 = net.get("h1")
    _ping(h1, "10.0.1.11")
    # the switches speak OpenFlow 1.3 only; ovs-ofctl defaults to 1.0
    flows = net.get("sw-a").cmd("ovs-ofctl -O OpenFlow13 dump-flows sw-a")
    assert "output:" in flows, "no forwarding flows installed on sw-a"
