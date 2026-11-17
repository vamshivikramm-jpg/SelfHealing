"""Unit tests for live Zero Trust (P5): controller-side rule evaluation,
flow bookkeeping, the runtime feed/enforcer and the host programs.

The rules come from the real policy via ``compile_rules``, so the Ryu
evaluation (``sh_logic.ZtTable``) is checked against the same decisions
the simulated enforcer makes. No Ryu or Mininet needed.
"""

import json
import pathlib
import socket
import struct
import sys

import pytest
import yaml

from network.config import load_topology_config
from network.live.hostapps import _checksum, spoofed_syn
from network.live.security import LiveEnforcer, LiveSecurityFeed, attach_live_enforcer, rule_to_json
from network.security.enforcement import SimulatedEnforcer, compile_rules
from network.security.engine import ZeroTrustEngine
from network.security.identity import IdentityRegistry
from network.security.policy import ZeroTrustPolicy
from network.security.records import FlowRecord

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = ROOT / "network" / "config"
sys.path.insert(0, str(ROOT / "controller" / "apps"))
import sh_logic as L  # noqa: E402

SWA, SWB = 0x11, 0x12


@pytest.fixture(scope="module")
def world():
    topology = load_topology_config(str(CFG / "enterprise.yaml"))
    registry = IdentityRegistry.from_config(topology, str(CFG / "identities.yaml"))
    policy = ZeroTrustPolicy.from_file(str(CFG / "zero_trust_policy.yaml"))
    topo = L.Topology(yaml.safe_load((CFG / "enterprise.yaml").read_text()),
                      yaml.safe_load((CFG / "identities.yaml").read_text()))
    return topology, registry, policy, topo


def _rules(registry, policy, quarantined=()):
    trust = {i.name: i.effective_base_trust() for i in registry.all()}
    return compile_rules(policy, registry, trust, quarantined)


def _claims(topo, registry, src, dst, port, claim=None):
    me = registry.get(claim or src)
    port_of = {v: k for k, v in topo.edge_port.items()}
    dpid, in_port = port_of[src]
    dst_ip = registry.get(dst).ip if dst != "gateway" else registry.get(src).gateway
    return L.flow_claims(topo, dpid, in_port, me.mac, me.ip, dst_ip, port)


CASES = [
    ("h2", "srv-web-1", 443, None),    # employee -> web: allowed
    ("h4", "srv-web-1", 443, None),    # contractor -> web: allowed
    ("h4", "srv-db-1", 5432, None),    # contractor -> db: no rule
    ("h2", "srv-web-1", 22, None),     # employee ssh: no rule
    ("h1", "srv-db-1", 22, None),      # admin ssh: allowed
    ("srv-web-1", "srv-db-1", 5432, None),
    ("h4", "srv-db-1", 22, "h1"),      # h4 claims h1's identity: spoofing
]


@pytest.mark.parametrize("src,dst,port,claim", CASES)
def test_ryu_decision_matches_simulated_enforcer(world, src, dst, port, claim):
    _, registry, policy, topo = world
    rules = _rules(registry, policy)
    table = L.ZtTable([rule_to_json(r) for r in rules])
    claims = _claims(topo, registry, src, dst, port, claim)
    allowed, _ = table.admit(claims)
    sim = SimulatedEnforcer()
    sim.apply(rules)
    me = registry.get(claim or src)
    expected, _ = sim.admit(FlowRecord(0, src, me.ip, me.mac, dst, port))
    assert allowed == expected


def test_monitoring_ping_allowed_but_not_when_quarantined(world):
    _, registry, policy, topo = world
    table = L.ZtTable([rule_to_json(r) for r in _rules(registry, policy, quarantined=["h4"])])
    assert table.admit(_claims(topo, registry, "h2", "srv-web-1", None))[0]
    assert not table.admit(_claims(topo, registry, "h4", "srv-web-1", None))[0]
    assert table.admit(_claims(topo, registry, "h4", "gateway", None))[0]  # gateway stays reachable
    assert not table.admit(_claims(topo, registry, "h4", "srv-web-1", 443))[0]


def test_spoofed_ping_denied(world):
    _, registry, policy, topo = world
    table = L.ZtTable([rule_to_json(r) for r in _rules(registry, policy)])
    assert not table.admit(_claims(topo, registry, "h4", "srv-web-1", None, claim="h1"))[0]


def test_zt_off_allows_everything_and_set_reports_changes(world):
    _, registry, policy, _ = world
    table = L.ZtTable()
    assert table.admit({"src": "unknown"}) == (True, "zero trust off")
    rules = [rule_to_json(r) for r in _rules(registry, policy)]
    assert table.set(rules) and table.version == 1
    assert not table.set(list(reversed(rules))) and table.version == 1  # same set, other order
    with pytest.raises(ValueError):
        table.set([{"table": 2, "priority": 1, "action": "allow"}])


def test_port_security_refuses_foreign_mac(world):
    _, registry, _, topo = world
    st = L.ForwardingState(topo)
    port_of = {v: k for k, v in topo.edge_port.items()}
    assert not st.learn(registry.get("h1").mac, *port_of["h4"])  # h1's MAC on h4's port
    assert st.learn(registry.get("h4").mac, *port_of["h4"])


def test_microflow_match_fields():
    m = L.microflow_match(3, "02:00:0a:00:01:0b", "10.0.1.11", "10.0.1.20", 6, 443)
    assert m["tcp_dst"] == 443 and m["in_port"] == 3 and m["eth_type"] == L.IPV4
    assert "udp_dst" not in L.microflow_match(3, "a", "b", "c", 17, None)
    assert L.microflow_match(3, "a", "b", "c", 17, 80)["udp_dst"] == 80


def test_flow_log_counts_and_prunes():
    log = L.FlowLog(keep_s=10)
    a = log.add(100.0, src="h1", dst="srv-web-1")
    b = log.add(101.0, src="h2", dst="srv-web-1")
    log.update(a["id"], 50, 5000, 105.0)
    log.update(a["id"], 40, 4000, 106.0)  # counters never go backwards
    assert log.flows[a["id"]]["packets"] == 50 and log.flows[a["id"]]["last_seen"] == 105.0
    assert [f["id"] for f in log.since(after_id=a["id"])] == [b["id"]]
    assert {f["id"] for f in log.since(after_id=b["id"], active_s=3, now=107.0)} == {a["id"]}
    log.prune(112.0)
    assert set(log.flows) == {a["id"]}


class FakeClient:
    def __init__(self):
        self.pushed = []
        self.flows = []

    def request(self, method, path, body=None):
        if method == "PUT" and path == "/sh/zt":
            self.pushed.append(body["rules"])
            return {"changed": True}
        if method == "GET" and path.startswith("/sh/flows"):
            return {"flows": self.flows}
        raise AssertionError(path)


def test_live_enforcer_pushes_only_on_change(world):
    _, registry, policy, _ = world
    client = FakeClient()
    engine = ZeroTrustEngine(registry, policy)
    enforcer = attach_live_enforcer(engine, client)
    assert isinstance(engine.enforcer, LiveEnforcer) and len(client.pushed) == 1
    engine._apply_rules()
    assert len(client.pushed) == 1
    engine.add_block("h4")
    assert len(client.pushed) == 2 and enforcer.pushes == 2
    assert any(r["match"] == {"src": "h4"} and r["action"] == "drop" for r in client.pushed[-1])


def test_feed_turns_counters_into_step_records(world, tmp_path):
    _, registry, _, _ = world
    client = FakeClient()
    log = tmp_path / "auth.log"
    feed = LiveSecurityFeed(client, registry, {"srv-web-1": str(log)})
    flow = {"id": 1, "src": "h2", "src_ip": "10.0.1.11", "src_mac": "02:00:0a:00:01:0b", "dst": "srv-web-1",
            "dst_port": 443, "proto": 6, "packets": 10, "bytes": 8000}
    ping = dict(flow, id=2, dst_port=None, proto=1)
    client.flows = [flow, ping]
    log.write_text(json.dumps({"ts": 1, "src_ip": "10.0.2.10", "port": 443, "account": "root",
                               "success": False}) + "\n")
    flows, auth = feed.step(1.0)
    assert [(f.src, f.dst_port, f.packets, f.bytes) for f in flows] == [("h2", 443, 10, 8000)]
    assert [(a.device, a.target, a.account, a.success) for a in auth] == [("h4", "srv-web-1", "root", False)]
    client.flows = [dict(flow, packets=10)]
    assert feed.step(2.0) == ([], [])  # nothing new: no packets, no new log lines
    client.flows = [dict(flow, packets=25, bytes=20000)]
    flows, _ = feed.step(3.0)
    assert flows[0].packets == 15 and feed.skipped == 1


def test_spoofed_syn_is_a_valid_frame():
    frame = spoofed_syn("02:00:0a:00:01:0a", "02:fe:0a:00:02:01", "10.0.1.10", "10.0.1.21", 40000, 22)
    assert frame[6:12] == bytes.fromhex("02000a00010a") and frame[12:14] == b"\x08\x00"
    ip = frame[14:34]
    assert _checksum(ip) == 0  # header checksum verifies
    assert socket.inet_ntoa(ip[12:16]) == "10.0.1.10" and socket.inet_ntoa(ip[16:20]) == "10.0.1.21"
    tcp = frame[34:]
    pseudo = ip[12:20] + struct.pack("!BBH", 0, 6, len(tcp))
    assert _checksum(pseudo + tcp) == 0 and struct.unpack("!H", tcp[2:4])[0] == 22 and tcp[13] == 0x02
