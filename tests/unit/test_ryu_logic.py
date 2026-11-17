"""Unit tests for the self-healing Ryu app's forwarding logic (sh_logic).

Pure Python: no Ryu needed. Uses the real topology/identity YAML files.
"""

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "controller" / "apps"))
import sh_logic as L  # noqa: E402

CFG = ROOT / "network" / "config"
RTR, SWA, SWB = 0x1, 0x11, 0x12


def _state(topology="enterprise.yaml"):
    topo = L.Topology(yaml.safe_load((CFG / topology).read_text()),
                      yaml.safe_load((CFG / "identities.yaml").read_text()))
    st = L.ForwardingState(topo)
    st.connected |= {RTR, SWA, SWB}
    return st


def _locate_all(st):
    # Mininet host ports on sw-a: 1 = uplink, then h1, h2, h3, srv-web-1, srv-db-1 (2..6)
    for i, name in enumerate(["h1", "h2", "h3", "srv-web-1", "srv-db-1"], start=2):
        st.learn(st.topo.hosts[name]["mac"], SWA, i)
    for i, name in enumerate(["h4", "h5"], start=2):
        st.learn(st.topo.hosts[name]["mac"], SWB, i)


def test_topology_facts():
    st = _state()
    assert st.topo.links == [(RTR, 1, SWA, 1), (RTR, 2, SWB, 1)]
    assert st.topo.hosts["h1"] == {"name": "h1", "ip": "10.0.1.10", "mac": "02:00:0a:00:01:0a",
                                   "subnet": "subnet-a", "gateway": "10.0.1.1"}
    assert st.topo.gateways["10.0.1.1"] == "02:fe:0a:00:01:01"


def test_redundant_topology_has_three_switch_links():
    st = _state("enterprise-redundant.yaml")
    assert (SWA, 10, SWB, 10) in st.topo.links and len(st.topo.links) == 3


def test_proxy_arp_answers_hosts_and_gateways_only():
    st = _state()
    assert st.arp_answer("10.0.2.11") == "02:00:0a:00:02:0b"
    assert st.arp_answer("10.0.2.1") == "02:fe:0a:00:02:01"
    assert st.arp_answer("10.9.9.9") is None


def test_learning_ignores_trunk_ports_and_gateway_macs():
    st = _state()
    assert not st.learn("02:00:0a:00:01:0a", SWA, 1)  # uplink to rtr-core
    assert not st.learn("02:fe:0a:00:01:01", SWA, 3)
    assert st.learn("02:00:0a:00:01:0a", SWA, 2)
    assert not st.learn("02:00:0a:00:01:0a", SWA, 2)  # unchanged


def test_plan_routes_inter_subnet_with_rewrite():
    st = _state()
    _locate_all(st)
    rules = st.plan("10.0.2.10")  # h4 on sw-b port 2
    by = {(r["dpid"], r["match"].get("eth_dst")): r for r in rules}
    assert by[(RTR, None)]["actions"] == [("output", 2)]
    assert by[(SWA, None)]["actions"] == [("output", 1)]
    assert by[(SWB, "02:00:0a:00:02:0a")]["actions"] == [("output", 2)]
    egress = [r for r in rules if r["dpid"] == SWB and r["priority"] == L.PRIO_EGRESS_ROUTED]
    assert len(egress) == 1 and "eth_dst" not in egress[0]["match"]
    assert egress[0]["actions"] == [
        ("set_eth_src", "02:fe:0a:00:02:01"), ("set_eth_dst", "02:00:0a:00:02:0a"), ("dec_ttl",), ("output", 2)]


def test_unlocated_destination_has_no_plan():
    st = _state()
    assert st.plan("10.0.2.10") == []


def test_link_down_removes_paths_and_up_restores():
    st = _state()
    _locate_all(st)
    assert st.set_port(RTR, 2, False) is True
    assert {r["dpid"] for r in st.plan("10.0.2.10")} == {SWB}
    assert st.set_port(SWA, 3, False) is False  # edge port: no path change
    assert st.set_port(RTR, 2, True) is True
    assert {r["dpid"] for r in st.plan("10.0.2.10")} == {RTR, SWA, SWB}


def test_redundant_prefers_direct_link_and_fails_over():
    st = _state("enterprise-redundant.yaml")
    _locate_all(st)
    assert st.route_summary("10.0.2.10")["paths"]["sw-a"] == ["sw-a", "sw-b"]
    st.set_port(SWA, 10, False)
    assert st.route_summary("10.0.2.10")["paths"]["sw-a"] == ["sw-a", "rtr-core", "sw-b"]


def test_tree_is_loop_free_on_redundant_topology():
    st = _state("enterprise-redundant.yaml")
    _locate_all(st)
    for host in st.topo.hosts.values():
        paths = st.route_summary(host["ip"])["paths"]
        assert set(paths) == {"rtr-core", "sw-a", "sw-b"}
        assert all(len(p) == len(set(p)) for p in paths.values())


def test_pinned_path_overrides_tree():
    st = _state("enterprise-redundant.yaml")
    _locate_all(st)
    assert st.validate_path("10.0.2.10", ["sw-a", "rtr-core", "sw-b"]) is None
    st.pinned["10.0.2.10"] = ["sw-a", "rtr-core", "sw-b"]
    summary = st.route_summary("10.0.2.10")
    assert summary["pinned_active"] and summary["paths"]["sw-a"] == ["sw-a", "rtr-core", "sw-b"]


@pytest.mark.parametrize("path,msg", [
    (["sw-a", "sw-b"], "down or does not exist"),       # no direct link in base topology
    (["sw-a", "rtr-core"], "must end at sw-b"),
    (["sw-a", "rtr-core", "sw-a", "sw-b"], "twice"),
    (["sw-x"], "switch names"),
])
def test_invalid_pins_rejected(path, msg):
    st = _state()
    _locate_all(st)
    assert msg in st.validate_path("10.0.2.10", path)


def test_pin_ignored_when_its_link_fails():
    st = _state("enterprise-redundant.yaml")
    _locate_all(st)
    st.pinned["10.0.2.10"] = ["sw-a", "rtr-core", "sw-b"]
    st.set_port(RTR, 2, False)
    summary = st.route_summary("10.0.2.10")
    assert not summary["pinned_active"] and summary["paths"]["sw-a"] == ["sw-a", "sw-b"]


def test_disconnected_switch_is_routed_around():
    st = _state("enterprise-redundant.yaml")
    _locate_all(st)
    st.connected.discard(RTR)
    assert st.route_summary("10.0.2.10")["paths"] == {"sw-a": ["sw-a", "sw-b"], "sw-b": ["sw-b"]}


def test_block_match_normalisation():
    assert L.normalise_block_match({"ipv4_src": "10.0.2.10", "tcp_dst": 22}) == {
        "ipv4_src": "10.0.2.10", "tcp_dst": 22, "ip_proto": 6, "eth_type": 0x0800}
    assert L.normalise_block_match({"eth_src": "02:00:0a:00:02:0a"}) == {"eth_src": "02:00:0a:00:02:0a"}
    for bad in ({}, {"in_port": 1}, {"ipv4_src": "nonsense"}, {"tcp_dst": 1, "udp_dst": 2},
                {"tcp_dst": 1, "ip_proto": 17}):
        with pytest.raises(ValueError):
            L.normalise_block_match(bad)


def test_block_rules_go_to_acl_table_on_every_switch():
    st = _state()
    block = st.add_block({"ipv4_src": "10.0.2.10"}, "test")
    rules = st.block_rules(block)
    assert {r["dpid"] for r in rules} == {RTR, SWA, SWB}
    assert all(r["table"] == L.ACL_TABLE and r["actions"] == [] and r["cookie"] == L.COOKIE_BLOCK + 1
               for r in rules)
    with pytest.raises(ValueError):
        st.add_block({"ipv4_src": "10.0.2.10"}, switch="sw-x")


def test_covers_guards_resubmission():
    st = _state()
    _locate_all(st)
    rules = st.plan("10.0.2.10")
    assert L.covers(rules, RTR, "anything")
    assert L.covers(rules, SWB, "02:fe:0a:00:01:01")  # sent to subnet-a's gateway
    assert not L.covers(rules, 0x99, "02:00:00:00:00:99")  # unknown switch


def test_probe_mac_round_trip():
    assert L.probe_mac(70000) == "02:fd:00:01:11:70"
    assert L.probe_seq(L.probe_mac(70000)) == 70000
    assert L.probe_seq("02:00:0a:00:01:0a") is None


def test_monitor_ip_is_free_and_access_links_known():
    st = _state()
    assert st.topo.monitor_ip("subnet-a") == "10.0.1.254"
    assert (SWB, "h4") in st.topo.access and len(st.topo.access) == 7


def test_probe_book_loss_and_latency():
    book = L.ProbeBook(window_s=5.0, timeout_s=1.0)
    key = ("rtr-core-sw-a", "1>17")
    for seq in range(10):
        book.sent(key, seq, 100.0 + seq * 0.2)
        if seq % 2 == 0:
            assert book.received(key, seq, 100.0 + seq * 0.2 + 0.05, 50.0 + seq)
    s = book.summary([key], now=103.0)
    assert s["probes"] == 10 and s["loss_pct"] == 50.0 and s["latency_ms"] == 54.0
    # undecided probes (younger than the timeout) are not counted
    book.sent(key, 99, 102.5)
    assert book.summary([key], now=103.0)["probes"] == 10


def test_probe_book_rejects_late_duplicate_and_unknown():
    book = L.ProbeBook(timeout_s=1.0)
    book.sent(("l", "rt"), 1, 10.0)
    assert not book.received(("l", "rt"), 1, 11.5, 5.0)   # too late
    assert not book.received(("l", "rt"), 2, 10.1, 5.0)   # never sent
    book.sent(("l", "rt"), 3, 10.0)
    assert book.received(("l", "rt"), 3, 10.1, -2.0)
    assert not book.received(("l", "rt"), 3, 10.2, 5.0)  # duplicate
    assert book.summary([("l", "rt")], 12.0)["latency_ms"] == 0.0  # clamped


def test_probe_book_prunes_old_probes():
    book = L.ProbeBook(window_s=5.0, timeout_s=1.0)
    book.sent(("l", "rt"), 1, 0.0)
    book.prune(100.0)
    assert book.summary([("l", "rt")], 3.0)["probes"] == 0
