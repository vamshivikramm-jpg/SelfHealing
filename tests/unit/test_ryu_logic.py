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
