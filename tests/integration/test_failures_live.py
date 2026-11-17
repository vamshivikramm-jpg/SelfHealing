"""Live failure control on the real Mininet network (live P1).

Requires a running stack (mininet-net + ryu-controller):
    make net-test

Every failure the self-healing loop can inject must change real traffic:
added latency shows in ping round-trip times, packet loss drops pings,
bandwidth limits install a tc rate, and links/switches/hosts going down
stop traffic until they are restored. Uses h1 <-> h2 (same subnet, both
on sw-a), so the path is h1 - sw-a - h2.
"""

import pathlib
import re
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"
H2_IP = "10.0.1.11"


def _wait_connected(net, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(s.connected() for s in net.switches):
            return
        time.sleep(0.5)
    pytest.fail("switches not connected to controller")


def _ping(net, count=3, wait=1):
    out = net.get("h1").cmd(f"ping -c {count} -W {wait} -i 0.2 {H2_IP}")
    m = re.search(r"(\d+)% packet loss", out)
    loss = int(m.group(1)) if m else 100
    rtt = re.search(r"= [\d.]+/([\d.]+)/", out)
    return loss, (float(rtt.group(1)) if rtt else None)


def _reachable(net, tries=10):
    for _ in range(tries):
        loss, _ = _ping(net, count=2)
        if loss < 100:
            return True
        time.sleep(1)
    return False


@pytest.fixture(scope="module")
def ctrl():
    from mininet.clean import cleanup

    from network.control import NetworkController

    controller = NetworkController(config_path=str(CONFIG))
    controller.start_network()
    _wait_connected(controller._net)
    assert _reachable(controller._net), "baseline h1 -> h2 ping failed"
    yield controller
    try:
        controller.stop_network()
    finally:
        cleanup()


def test_identity_macs_are_assigned(ctrl):
    assert ctrl._net.get("h1").MAC() == "02:00:0a:00:01:0a"


def test_latency_raises_round_trip_time(ctrl):
    _, base = _ping(ctrl._net)
    ctrl.set_link_latency("sw-a", "h2", 50)
    try:
        loss, rtt = _ping(ctrl._net)
        assert loss < 100 and rtt is not None
        assert rtt >= 90, f"expected ~100 ms RTT (50 ms each way), got {rtt} (baseline {base})"
    finally:
        ctrl.clear_link_latency("sw-a", "h2")
    _, after = _ping(ctrl._net)
    assert after is not None and after < 20, f"latency not cleared: {after} ms"


def test_full_packet_loss_stops_traffic(ctrl):
    ctrl.set_link_packet_loss("sw-a", "h2", 100)
    try:
        loss, _ = _ping(ctrl._net)
        assert loss == 100
    finally:
        ctrl.clear_link_packet_loss("sw-a", "h2")
    assert _reachable(ctrl._net)


def test_bandwidth_limit_installs_tc_rate(ctrl):
    ctrl.set_link_bandwidth("sw-a", "h2", 5)
    try:
        out = ctrl._net.get("h2").cmd("tc qdisc show dev h2-eth0; tc class show dev h2-eth0")
        assert "htb" in out and "5Mbit" in out, out
    finally:
        ctrl.clear_link_bandwidth("sw-a", "h2")
    out = ctrl._net.get("h2").cmd("tc class show dev h2-eth0")
    assert "5Mbit" not in out


def test_link_down_and_up(ctrl):
    ctrl.disconnect_link("sw-a", "h2")
    assert _ping(ctrl._net, count=2)[0] == 100
    ctrl.connect_link("sw-a", "h2")
    assert _reachable(ctrl._net)


def test_switch_fail_and_recover(ctrl):
    ctrl.disable_node("sw-a")
    assert _ping(ctrl._net, count=2)[0] == 100
    ctrl.enable_node("sw-a")
    _wait_connected(ctrl._net)
    assert _reachable(ctrl._net, tries=20), "traffic did not recover after switch restart"


def test_host_disable_and_enable(ctrl):
    ctrl.disable_node("h2")
    assert _ping(ctrl._net, count=2)[0] == 100
    ctrl.enable_node("h2")
    assert _reachable(ctrl._net)
