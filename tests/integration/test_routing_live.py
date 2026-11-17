"""Live tests for the self-healing Ryu app (live P2).

Requires a running stack (mininet-net + ryu-controller with
apps/selfhealing_app.py):
    make net-test

Checks inter-subnet routing through the virtual gateways, gateway ping,
blocks installed through the REST API, and loop-safe forwarding on the
redundant topology (sw-a <-> sw-b plus both uplinks): failover when a link
goes down and pinned paths.
"""

import itertools
import json
import os
import pathlib
import re
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = ROOT / "network" / "config"
API = os.environ.get("RYU_API", "http://ryu-controller:8080")
H4 = "10.0.2.10"


def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def loss(net, src, dst_ip, count=2):
    out = net.get(src).cmd(f"ping -c {count} -W 1 -i 0.3 {dst_ip}")
    m = re.search(r"(\d+)% packet loss", out)
    return int(m.group(1)) if m else 100


def reachable(net, src, dst_ip, tries=8):
    for _ in range(tries):
        if loss(net, src, dst_ip) < 100:
            return True
        time.sleep(0.5)
    return False


def _start(topology_file):
    from network.control import NetworkController

    status, _ = api("PUT", "/sh/config", {"topology": topology_file})
    assert status == 200
    ctrl = NetworkController(config_path=str(CFG / topology_file))
    ctrl.start_network()
    deadline = time.time() + 45
    while time.time() < deadline:
        if all(s.connected() for s in ctrl._net.switches):
            break
        time.sleep(0.5)
    deadline = time.time() + 20
    while time.time() < deadline and len(api("GET", "/sh/health")[1]["switches_connected"]) < 3:
        time.sleep(0.5)
    return ctrl


def _stop(ctrl):
    from mininet.clean import cleanup

    try:
        ctrl.stop_network()
    except Exception:  # noqa: BLE001 - already stopped
        pass
    finally:
        cleanup()


@pytest.fixture(scope="module")
def base():
    ctrl = _start("enterprise.yaml")
    yield ctrl
    _stop(ctrl)


def test_api_health(base):
    status, body = api("GET", "/sh/health")
    assert status == 200 and body["topology"] == "enterprise.yaml"
    assert body["switches_connected"] == ["rtr-core", "sw-a", "sw-b"]


def test_inter_subnet_routing(base):
    assert reachable(base._net, "h1", H4), "h1 (subnet-a) -> h4 (subnet-b)"
    assert reachable(base._net, "h5", "10.0.1.20"), "h5 (subnet-b) -> srv-web-1 (subnet-a)"


def test_every_pair_reaches_each_other(base):
    names = ["h1", "h2", "h3", "h4", "h5", "srv-web-1", "srv-db-1"]
    ips = {n: base._net.get(n).IP() for n in names}
    for n in names:  # locate everyone first
        reachable(base._net, n, ips["h1" if n != "h1" else "h2"])
    failed = [f"{a}->{b}" for a, b in itertools.permutations(names, 2) if loss(base._net, a, ips[b], 1) == 100]
    assert not failed, failed


def test_gateway_answers_ping(base):
    assert reachable(base._net, "h1", "10.0.1.1")
    assert reachable(base._net, "h4", "10.0.2.1")


def test_hosts_resolve_gateway_to_virtual_mac(base):
    base._net.get("h1").cmd("ping -c1 -W1 10.0.1.1")
    assert "02:fe:0a:00:01:01" in base._net.get("h1").cmd("ip neigh show 10.0.1.1")


def test_topology_view(base):
    _, topo = api("GET", "/sh/topology")
    assert all(link["live"] for link in topo["links"])
    assert topo["hosts"]["h1"] == {"switch": "sw-a", "port": 2}


def test_block_and_unblock(base):
    assert reachable(base._net, "h4", "10.0.1.10")
    status, block = api("POST", "/sh/blocks", {"match": {"ipv4_src": H4}, "reason": "live test"})
    assert status == 201
    try:
        time.sleep(0.5)
        assert loss(base._net, "h4", "10.0.1.10") == 100
        assert reachable(base._net, "h5", "10.0.1.10"), "only h4 should be blocked"
    finally:
        assert api("DELETE", f"/sh/blocks/{block['id']}")[0] == 200
    assert reachable(base._net, "h4", "10.0.1.10")


def test_invalid_requests_rejected(base):
    assert api("POST", "/sh/blocks", {"match": {"in_port": 1}})[0] == 400
    assert api("PUT", f"/sh/routes/{H4}", {"path": ["sw-a", "sw-b"]})[0] == 400  # no such link here
    assert api("DELETE", "/sh/blocks/9999")[0] == 404


def test_port_stats_polled(base):
    time.sleep(2.5)
    _, stats = api("GET", "/sh/stats/ports")
    assert set(stats) == {"rtr-core", "sw-a", "sw-b"}
    assert stats["sw-a"]["ports"]["1"]["tx_packets"] > 0


@pytest.fixture(scope="module")
def redundant(base):
    _stop(base)  # module order: base tests first, then the looped topology
    ctrl = _start("enterprise-redundant.yaml")
    yield ctrl
    _stop(ctrl)
    api("PUT", "/sh/config", {"topology": "enterprise.yaml"})


def _flow_packets(switch, dst_ip):
    _, flows = api("GET", "/sh/stats/flows")
    return sum(f["packets"] for f in flows.get(switch, {}).get("flows", [])
               if f["match"].get("ipv4_dst") == dst_ip)


def test_redundant_loop_is_safe(redundant):
    assert reachable(redundant._net, "h1", H4)
    assert reachable(redundant._net, "h4", "10.0.1.20")
    _, routes = api("GET", "/sh/routes")
    assert routes[H4]["paths"]["sw-a"] == ["sw-a", "sw-b"]


def test_redundant_failover(redundant):
    redundant.disconnect_link("sw-a", "sw-b")
    try:
        time.sleep(1)
        assert reachable(redundant._net, "h1", H4), "no failover via rtr-core"
        _, routes = api("GET", "/sh/routes")
        assert routes[H4]["paths"]["sw-a"] == ["sw-a", "rtr-core", "sw-b"]
    finally:
        redundant.connect_link("sw-a", "sw-b")
    time.sleep(1)
    assert api("GET", "/sh/routes")[1][H4]["paths"]["sw-a"] == ["sw-a", "sw-b"]


def test_pinned_path_carries_traffic(redundant):
    status, summary = api("PUT", f"/sh/routes/{H4}", {"path": ["sw-a", "rtr-core", "sw-b"]})
    assert status == 200 and summary["pinned_active"], summary
    try:
        time.sleep(2.5)
        before = _flow_packets("rtr-core", H4)
        assert loss(redundant._net, "h1", H4, count=5) < 100
        time.sleep(2.5)
        assert _flow_packets("rtr-core", H4) >= before + 4, "pinned path not used"
    finally:
        api("DELETE", f"/sh/routes/{H4}")
