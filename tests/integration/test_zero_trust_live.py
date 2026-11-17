"""Zero Trust on the real network (live P5).

Requires a running stack (mininet-net + ryu-controller with the
self-healing app):
    make net-test

Services run on the servers, every device runs its normal role traffic,
and the Zero Trust engine is fed with real flows (ingress-port device,
claimed addresses, packet counts from the switches) and real logins. Its
rules are enforced per flow by the switches. Each attack is then run for
real from a host and must be detected for the attacker.
"""

import pathlib
import time
from collections import defaultdict

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = ROOT / "network" / "config"
STEP = 2.0
WARMUP_STEPS = 22  # volume detectors learn ~20 steps of normal traffic


class Lab:
    def __init__(self):
        from network.config import load_topology_config
        from network.control import NetworkController
        from network.live import LiveSecurityFeed, LiveTraffic, RyuClient, attach_live_enforcer
        from network.security.engine import ZeroTrustEngine
        from network.security.identity import IdentityRegistry
        from network.security.policy import ZeroTrustPolicy

        self.client = RyuClient()
        self.client.set_topology("enterprise.yaml")
        self.client.request("DELETE", "/sh/zt")
        topology = load_topology_config(str(CFG / "enterprise.yaml"))
        self.registry = IdentityRegistry.from_config(topology, str(CFG / "identities.yaml"))
        self.ctrl = NetworkController(config_path=str(CFG / "enterprise.yaml"))
        self.ctrl.start_network()
        deadline = time.time() + 45
        while time.time() < deadline and not all(s.connected() for s in self.ctrl._net.switches):
            time.sleep(0.5)
        for host in self.ctrl._net.hosts:
            host.cmd(f"ping -c1 -W1 {self.registry.get(host.name).gateway}")
        self.engine = ZeroTrustEngine(self.registry, ZeroTrustPolicy.from_file(str(CFG / "zero_trust_policy.yaml")))
        self.enforcer = attach_live_enforcer(self.engine, self.client)
        self.traffic = LiveTraffic(self.ctrl, self.registry, str(CFG))
        self.traffic.start()
        self.feed = LiveSecurityFeed(self.client, self.registry, self.traffic.auth_logs)
        self.anomalies = defaultdict(set)   # subject -> kinds
        self.flows = 0
        self.denied = defaultdict(int)

    def steps(self, n):
        for _ in range(n):
            ts = time.time()
            flows, auth = self.feed.step(ts)
            result = self.engine.step(ts, flows, auth)
            self.flows += len(flows)
            for flow, decision, _ in result.decided:
                if not decision.allowed:
                    self.denied[flow.src] += 1
            for a in result.anomalies:
                self.anomalies[a.subject].add(a.kind)
            time.sleep(max(0.0, STEP - (time.time() - ts)))

    def tcp(self, src, dst, port):
        ip = self.registry.get(dst).ip
        out = self.ctrl._net.get(src).cmd(f"timeout 2 bash -c 'echo GET / > /dev/tcp/{ip}/{port}' && echo CONNECTED")
        return "CONNECTED" in out

    def close(self):
        from mininet.clean import cleanup

        self.traffic.stop()
        try:
            self.client.request("DELETE", "/sh/zt")
            self.ctrl.stop_network()
        finally:
            cleanup()


@pytest.fixture(scope="module")
def lab():
    lab = Lab()
    lab.steps(WARMUP_STEPS)
    yield lab
    lab.close()


def test_normal_traffic_is_seen_and_quiet(lab):
    assert lab.enforcer.pushes >= 1 and lab.enforcer.last_error is None
    assert lab.flows > 20, "the switches reported almost no flows"
    noisy = {s: k & {"fingerprint_mismatch", "port_scan", "auth_failures", "flood"}
             for s, k in lab.anomalies.items()}
    assert not any(noisy.values()), f"false alarms on normal traffic: {noisy}"
    assert not lab.engine.quarantined()


def test_policy_enforced_by_switches(lab):
    assert lab.tcp("h2", "srv-web-1", 443), "employee -> web:443 must be allowed"
    assert not lab.tcp("h4", "srv-db-1", 5432), "contractor -> db:5432 must be dropped"
    assert not lab.tcp("h2", "srv-web-1", 22), "employee ssh must be dropped"
    assert lab.tcp("h1", "srv-db-1", 22), "admin ssh must be allowed"


def test_manual_block_enforced_and_lifted(lab):
    lab.engine.add_block("h2")
    try:
        assert not lab.tcp("h2", "srv-web-1", 443)
        assert lab.tcp("h3", "srv-web-1", 443)
    finally:
        lab.engine.remove_block("h2")
    assert lab.tcp("h2", "srv-web-1", 443)


@pytest.mark.parametrize("kind,attacker,expected", [
    ("port_scan", "h5", {"port_scan"}),
    ("brute_force", "h3", {"auth_failures"}),
    ("spoofing", "h4", {"fingerprint_mismatch"}),
    ("lateral_movement", "h2", {"policy_violation"}),
    ("flood", "h1", {"volume_anomaly", "flood"}),
])
def test_attack_detected(lab, kind, attacker, expected):
    lab.anomalies.clear()
    lab.traffic.launch_attack(kind, attacker, duration_s=24, pps=1000)
    lab.steps(12)
    seen = lab.anomalies.get(attacker, set())
    assert seen & expected, f"{kind} from {attacker}: detected {seen}; all: {dict(lab.anomalies)}"
    lab.steps(3)  # let it finish
