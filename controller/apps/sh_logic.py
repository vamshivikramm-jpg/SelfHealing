"""Forwarding logic for the self-healing Ryu app (pure Python, no Ryu).

Kept apart from the Ryu glue (``selfhealing_app.py``) so it can be unit
tested anywhere. It knows the topology from the same YAML files Mininet is
built from:

* switch-to-switch links and their port numbers (from the topology file;
  missing port numbers are filled the way Mininet's ``Topo.addPort`` does);
* every host's IP, MAC (identities.yaml), subnet and gateway.

Forwarding model (one distributed router):

* ARP is answered by the controller (proxy ARP): a host's IP resolves to
  its configured MAC, a gateway IP to a virtual gateway MAC. Nothing is
  ever flooded across switch-to-switch links, so loops are harmless.
* IPv4 is forwarded on destination IP. For each destination host the
  controller computes a shortest-path tree over the *live* links towards
  the host's switch, so every switch's next hop is consistent and loop
  free. At the last switch, routed packets (sent to the gateway MAC) get
  their MACs rewritten and TTL decremented (any packet not already
  addressed to the host's MAC); same-subnet packets go as is.
* An operator/self-healing engine may pin a path for a destination; the
  pinned switches use it, all others follow the tree into it.
"""

from __future__ import annotations

import ipaddress
import statistics
from collections import deque
from typing import Dict, Iterable, List, Optional, Tuple

ARP = 0x0806
IPV4 = 0x0800
PROBE_ETHERTYPE = 0x88B5      # IEEE "local experimental": controller link probes
PROBE_MAC_PREFIX = "02:fd:"   # source MAC of probes; ARP probe sequence lives in it

# table 0 = access control (blocks), table 1 = forwarding
ACL_TABLE = 0
FWD_TABLE = 1
PRIO_BLOCK = 1000
PRIO_EGRESS = 120          # last switch, packet already addressed to the host
PRIO_EGRESS_ROUTED = 110   # last switch, packet addressed to a gateway: rewrite MACs
PRIO_TRANSIT = 100
PRIO_ARP = 200
PRIO_PROBE = 300
# table 0 (Zero Trust watch): microflow rules decided per flow
PRIO_MICROFLOW = 500
PRIO_TRUNK_BYPASS = 20     # traffic arriving from another switch was checked at its ingress
PRIO_WATCH = 10            # first IPv4 packet of a new flow from a host port -> controller
COOKIE_WATCH = 0x5700000000    # + flow id (microflow rules)
COOKIE_WATCH_MASK = 0xFF00000000
COOKIE_BASE = 0x5000          # table-miss / ARP / drop rules
COOKIE_FWD = 0x5100           # per-destination forwarding rules
COOKIE_BLOCK = 0x5B000000     # + block id

BLOCK_FIELDS = ("eth_src", "eth_dst", "ipv4_src", "ipv4_dst", "ip_proto", "tcp_dst", "udp_dst")


def gateway_mac(gateway_ip: str) -> str:
    """Virtual MAC of a subnet gateway, derived from its IP."""
    a, b, c, d = (int(x) for x in gateway_ip.split("."))
    return f"02:fe:{a:02x}:{b:02x}:{c:02x}:{d:02x}"


def _dpid(value) -> int:
    return int(str(value), 16)


def _strip_prefix(ip: str) -> str:
    return ip.split("/")[0]


class Topology:
    """Static facts from the topology + identities YAML dictionaries."""

    def __init__(self, topo: dict, identities: Optional[dict] = None):
        devices = topo.get("devices") or {}
        idents = (identities or {}).get("devices") or {}
        self.name = topo.get("name", "topology")
        self.dpid_of: Dict[str, int] = {}
        for group in ("routers", "switches"):
            for dev in devices.get(group) or []:
                self.dpid_of[dev["name"]] = _dpid(dev["datapath_id"])
        self.name_of: Dict[int, str] = {v: k for k, v in self.dpid_of.items()}

        self.subnets: Dict[str, dict] = {}
        for seg in topo.get("segments") or []:
            for sn in seg.get("subnets") or []:
                self.subnets[sn["id"]] = {
                    "network": ipaddress.ip_network(sn["cidr"], strict=False),
                    "gateway": sn["gateway"],
                    "gateway_mac": gateway_mac(sn["gateway"]),
                }
        self.gateways: Dict[str, str] = {s["gateway"]: s["gateway_mac"] for s in self.subnets.values()}

        self.hosts: Dict[str, dict] = {}
        for group in ("hosts", "servers"):
            for dev in devices.get(group) or []:
                ip = _strip_prefix(dev["ip"])
                mac = (idents.get(dev["name"]) or {}).get("mac")
                self.hosts[dev["name"]] = {"name": dev["name"], "ip": ip, "mac": mac,
                                           "subnet": dev.get("subnet"), "gateway": dev.get("gateway")}
        self.host_by_ip = {h["ip"]: h for h in self.hosts.values()}
        self.host_by_mac = {h["mac"]: h for h in self.hosts.values() if h["mac"]}

        # links: replay Mininet's Topo.addPort numbering for missing ports
        ports: Dict[str, int] = {}
        self.edge_port: Dict[Tuple[int, int], str] = {}  # (dpid, port) -> device wired there
        self.links: List[Tuple[int, int, int, int]] = []  # (dpid_a, port_a, dpid_b, port_b)
        for link in topo.get("links") or []:
            src, dst = link["src"], link["dst"]
            sport = link.get("src_port") or self._next_port(ports, src)
            dport = link.get("dst_port") or self._next_port(ports, dst)
            ports[src] = ports.get(src, 0) + 1
            ports[dst] = ports.get(dst, 0) + 1
            if src in self.dpid_of and dst in self.dpid_of:
                self.links.append((self.dpid_of[src], int(sport), self.dpid_of[dst], int(dport)))
            for sw, port, other in ((src, sport, dst), (dst, dport, src)):
                if sw in self.dpid_of and other not in self.dpid_of:
                    self.edge_port[(self.dpid_of[sw], int(port))] = other
        # switch <-> host links: (switch dpid, host name)
        self.access: List[Tuple[int, str]] = []
        for link in topo.get("links") or []:
            for sw, other in ((link["src"], link["dst"]), (link["dst"], link["src"])):
                if sw in self.dpid_of and other in self.hosts:
                    self.access.append((self.dpid_of[sw], other))
        self.trunk_ports: Dict[int, set] = {d: set() for d in self.name_of}
        self.peer: Dict[Tuple[int, int], Tuple[int, int]] = {}
        for a, pa, b, pb in self.links:
            self.trunk_ports[a].add(pa)
            self.trunk_ports[b].add(pb)
            self.peer[(a, pa)] = (b, pb)
            self.peer[(b, pb)] = (a, pa)

    def _next_port(self, ports: Dict[str, int], node: str) -> int:
        base = 1 if node in self.dpid_of else 0
        return ports.get(node, 0) + base

    def subnet_of_ip(self, ip: str) -> Optional[dict]:
        addr = ipaddress.ip_address(ip)
        for sn in self.subnets.values():
            if addr in sn["network"]:
                return sn
        return None

    def is_trunk(self, dpid: int, port: int) -> bool:
        return port in self.trunk_ports.get(dpid, ())

    def monitor_ip(self, subnet_id: str) -> Optional[str]:
        """An unused address in the subnet that ARP probes come from (the
        highest free one), so probes never touch a host's gateway entry."""
        sn = self.subnets.get(subnet_id)
        if sn is None:
            return None
        used = set(self.host_by_ip) | set(self.gateways)
        for addr in reversed(list(sn["network"].hosts())[-16:]):
            if str(addr) not in used:
                return str(addr)
        return None


def link_name(a: str, b: str) -> str:
    """Canonical link name, as NetworkController/NetworkState use."""
    return "-".join(sorted((a, b)))


def probe_mac(seq: int) -> str:
    seq &= 0xFFFFFFFF
    return PROBE_MAC_PREFIX + ":".join(f"{(seq >> s) & 0xFF:02x}" for s in (24, 16, 8, 0))


def probe_seq(mac: str) -> Optional[int]:
    if not mac.startswith(PROBE_MAC_PREFIX):
        return None
    return int(mac[len(PROBE_MAC_PREFIX):].replace(":", ""), 16)


class ProbeBook:
    """Sent/received probes per (link, direction) and their summaries.

    A probe counts as lost if no answer arrived within ``timeout_s``.
    Summaries use probes sent in the last ``window_s`` seconds that are
    already decided (older than the timeout).
    """

    def __init__(self, window_s: float = 5.0, timeout_s: float = 1.0):
        self.window_s = window_s
        self.timeout_s = timeout_s
        self._probes: Dict[tuple, Dict[int, list]] = {}  # key -> seq -> [t_sent, one_way_ms|None]

    def sent(self, key: tuple, seq: int, t: float) -> None:
        self._probes.setdefault(key, {})[seq] = [t, None]

    def sent_at(self, key: tuple, seq: int) -> Optional[float]:
        entry = self._probes.get(key, {}).get(seq)
        return entry[0] if entry else None

    def received(self, key: tuple, seq: int, now: float, one_way_ms: float) -> bool:
        entry = self._probes.get(key, {}).get(seq)
        if entry is None or now - entry[0] > self.timeout_s or entry[1] is not None:
            return False
        entry[1] = max(0.0, one_way_ms)
        return True

    def prune(self, now: float) -> None:
        horizon = now - self.window_s - self.timeout_s - 1.0
        for probes in self._probes.values():
            for seq in [s for s, e in probes.items() if e[0] < horizon]:
                del probes[seq]

    def summary(self, keys: Iterable[tuple], now: float) -> dict:
        decided = [e for k in keys for e in self._probes.get(k, {}).values()
                   if now - self.window_s - self.timeout_s <= e[0] <= now - self.timeout_s]
        got = [e[1] for e in decided if e[1] is not None]
        sent = len(decided)
        return {"probes": sent,
                "loss_pct": None if not sent else round(100.0 * (sent - len(got)) / sent, 2),
                "latency_ms": round(statistics.median(got), 3) if got else None}


class ForwardingState:
    """Live state: connected switches, port liveness, host locations,
    pinned paths and blocks. Produces abstract flow rules."""

    def __init__(self, topology: Topology):
        self.topo = topology
        self.connected: set = set()
        self.port_down: set = set()             # {(dpid, port)}
        self.host_loc: Dict[str, Tuple[int, int]] = {}  # mac -> (dpid, port)
        self.pinned: Dict[str, List[str]] = {}  # dst ip -> [switch names]
        self.blocks: Dict[int, dict] = {}
        self._next_block = 1

    # -- liveness ------------------------------------------------------
    def link_live(self, a: int, pa: int, b: int, pb: int) -> bool:
        return (a in self.connected and b in self.connected
                and (a, pa) not in self.port_down and (b, pb) not in self.port_down)

    def live_links(self) -> List[Tuple[int, int, int, int]]:
        return [l for l in self.topo.links if self.link_live(*l)]

    def set_port(self, dpid: int, port: int, live: bool) -> bool:
        """Record a port's state. Returns True if a switch-to-switch link
        changed (paths must be recomputed)."""
        key = (dpid, port)
        before = key in self.port_down
        if live:
            self.port_down.discard(key)
        else:
            self.port_down.add(key)
        return before != (not live) and self.topo.is_trunk(dpid, port)

    # -- hosts ---------------------------------------------------------
    def learn(self, mac: str, dpid: int, port: int) -> bool:
        """Learn a host location from a packet seen on an edge port.
        Returns True if it is new or moved."""
        if self.topo.is_trunk(dpid, port) or mac in self.topo.gateways.values():
            return False
        owner = self.topo.host_by_mac.get(mac)
        wired = self.topo.edge_port.get((dpid, port))
        if owner is not None and wired is not None and owner["name"] != wired:
            return False  # port security: a MAC claimed from someone else's port
        old = self.host_loc.get(mac)
        self.host_loc[mac] = (dpid, port)
        return old != (dpid, port)

    def edge_ports(self, dpid: int, all_ports: Iterable[int]) -> List[int]:
        return sorted(p for p in all_ports
                      if not self.topo.is_trunk(dpid, p) and (dpid, p) not in self.port_down)

    def arp_answer(self, target_ip: str) -> Optional[str]:
        """MAC to answer an ARP request for ``target_ip`` with (or None)."""
        if target_ip in self.topo.gateways:
            return self.topo.gateways[target_ip]
        host = self.topo.host_by_ip.get(target_ip)
        return host["mac"] if host else None

    # -- paths ---------------------------------------------------------
    def tree(self, dst_dpid: int) -> Dict[int, int]:
        """Next-hop out port for every switch that can reach ``dst_dpid``
        over live links (BFS, ties broken by switch name)."""
        adj: Dict[int, List[Tuple[int, int, int]]] = {}
        for a, pa, b, pb in self.live_links():
            adj.setdefault(a, []).append((b, pa, pb))
            adj.setdefault(b, []).append((a, pb, pa))
        nxt: Dict[int, int] = {}
        if dst_dpid not in self.connected:
            return nxt
        seen = {dst_dpid}
        queue = deque([dst_dpid])
        while queue:
            v = queue.popleft()
            for u, port_v, port_u in sorted(adj.get(v, []), key=lambda t: (self.topo.name_of[t[0]], t[2])):
                if u not in seen:
                    seen.add(u)
                    nxt[u] = port_u  # u sends towards v out of port_u
                    queue.append(u)
        return nxt

    def path_ports(self, names: List[str]) -> Optional[List[int]]:
        """Out ports along a switch path, or None if a hop is not a live link."""
        out = []
        for x, y in zip(names, names[1:]):
            a, b = self.topo.dpid_of.get(x), self.topo.dpid_of.get(y)
            port = next((pa for (s, pa, d, pb) in self.live_links() if s == a and d == b), None)
            if port is None:
                port = next((pb for (s, pa, d, pb) in self.live_links() if s == b and d == a), None)
            if port is None:
                return None
            out.append(port)
        return out

    def validate_path(self, dst_ip: str, names: List[str]) -> Optional[str]:
        """Error message for an invalid pinned path, else None."""
        host = self.topo.host_by_ip.get(dst_ip)
        if host is None:
            return f"unknown destination {dst_ip}"
        if not names or any(n not in self.topo.dpid_of for n in names):
            return "path must be a list of switch names"
        if len(set(names)) != len(names):
            return "path visits a switch twice"
        loc = self.host_loc.get(host["mac"])
        if loc is not None and self.topo.dpid_of[names[-1]] != loc[0]:
            return f"path must end at {self.topo.name_of[loc[0]]}, where {host['name']} is attached"
        if self.path_ports(names) is None:
            return "path uses a link that is down or does not exist"
        return None

    def next_hops(self, dst_ip: str) -> Tuple[Optional[int], Dict[int, int], bool]:
        """(destination switch, next hops, pinned path used?)."""
        host = self.topo.host_by_ip[dst_ip]
        loc = self.host_loc.get(host["mac"])
        if loc is None:
            return None, {}, False
        nxt = self.tree(loc[0])
        pin = self.pinned.get(dst_ip)
        used = False
        if pin and self.topo.dpid_of.get(pin[-1]) == loc[0]:
            ports = self.path_ports(pin)
            if ports is not None:
                for name, port in zip(pin, ports):
                    nxt[self.topo.dpid_of[name]] = port
                used = True
        return loc[0], nxt, used

    def plan(self, dst_ip: str) -> List[dict]:
        """Abstract flow rules delivering IPv4 to ``dst_ip`` from every
        switch that can reach it. Empty if the host is not located yet."""
        host = self.topo.host_by_ip.get(dst_ip)
        if host is None or not host["mac"]:
            return []
        dst_dpid, nxt, _ = self.next_hops(dst_ip)
        if dst_dpid is None:
            return []
        port = self.host_loc[host["mac"]][1]
        gw_mac = self.topo.subnets[host["subnet"]]["gateway_mac"] if host["subnet"] in self.topo.subnets else None
        rules = [_rule(dst_dpid, PRIO_EGRESS, {"eth_type": IPV4, "ipv4_dst": dst_ip, "eth_dst": host["mac"]},
                       [("output", port)])]
        if gw_mac:
            # routed: the sender addressed its own subnet's gateway MAC
            rules.append(_rule(dst_dpid, PRIO_EGRESS_ROUTED, {"eth_type": IPV4, "ipv4_dst": dst_ip},
                               [("set_eth_src", gw_mac), ("set_eth_dst", host["mac"]), ("dec_ttl",),
                                ("output", port)]))
        for dpid, out in sorted(nxt.items()):
            if dpid != dst_dpid:
                rules.append(_rule(dpid, PRIO_TRANSIT, {"eth_type": IPV4, "ipv4_dst": dst_ip}, [("output", out)]))
        return rules

    def route_summary(self, dst_ip: str) -> dict:
        """Switch path from every located host's switch to ``dst_ip``."""
        host = self.topo.host_by_ip[dst_ip]
        dst_dpid, nxt, pinned = self.next_hops(dst_ip)
        out = {"dst": host["name"], "ip": dst_ip, "located": dst_dpid is not None,
               "pinned": self.pinned.get(dst_ip), "pinned_active": pinned, "paths": {}}
        if dst_dpid is None:
            return out
        for name, dpid in sorted(self.topo.dpid_of.items()):
            path, cur = [name], dpid
            while cur != dst_dpid and cur in nxt and len(path) <= len(self.topo.dpid_of):
                cur = self.topo.peer.get((cur, nxt[cur]), (None, None))[0]
                if cur is None:
                    break
                path.append(self.topo.name_of[cur])
            if cur == dst_dpid:
                out["paths"][name] = path
        return out

    # -- blocks --------------------------------------------------------
    def add_block(self, match: dict, reason: str = "", switch: Optional[str] = None) -> dict:
        clean = normalise_block_match(match)
        if switch is not None and switch not in self.topo.dpid_of:
            raise ValueError(f"unknown switch {switch!r}")
        block = {"id": self._next_block, "match": clean, "reason": reason, "switch": switch}
        self.blocks[block["id"]] = block
        self._next_block += 1
        return block

    def block_rules(self, block: dict) -> List[dict]:
        targets = [self.topo.dpid_of[block["switch"]]] if block["switch"] else sorted(self.connected)
        return [dict(_rule(d, PRIO_BLOCK, block["match"], []), table=ACL_TABLE,
                     cookie=COOKIE_BLOCK + block["id"]) for d in targets]


class ZtTable:
    """Zero Trust rules pushed by the runtime (``FlowRule`` dicts from
    ``network.security.enforcement.compile_rules``), evaluated per new flow
    exactly like ``SimulatedEnforcer``: table 0 (identity binding) then
    table 1 (access), first match by priority wins.

    Claims: ``src`` (device wired to the ingress port), ``src_ip``,
    ``src_mac``, ``dst`` (device name, or ``gateway``), ``port``. Flows
    without a destination port (ICMP) are not subject to the role rules
    (monitoring pings), only to identity binding and quarantine: in table 1
    they see just the per-device rules (quarantine drop / gateway allow).
    """

    def __init__(self, rules: Optional[List[dict]] = None):
        self.rules: List[dict] = []
        self.version = 0
        if rules is not None:
            self.set(rules)

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    def set(self, rules: List[dict]) -> bool:
        clean = []
        for r in rules:
            if r.get("action") not in ("allow", "drop") or int(r.get("table", -1)) not in (0, 1):
                raise ValueError(f"bad rule {r!r}")
            clean.append({"table": int(r["table"]), "priority": int(r["priority"]), "action": r["action"],
                          "match": dict(r.get("match") or {}), "reason": r.get("reason", "")})
        clean.sort(key=lambda r: (r["table"], -r["priority"], sorted(r["match"].items())))
        changed = clean != self.rules
        if changed:
            self.rules = clean
            self.version += 1
        return changed

    def admit(self, claims: dict) -> Tuple[bool, str]:
        if not self.rules:
            return True, "zero trust off"
        icmp = claims.get("port") is None
        reason = ""
        for table in (0, 1):
            candidates = [r for r in self.rules if r["table"] == table]
            if icmp and table == 1:
                candidates = [r for r in candidates if r["match"] and "port" not in r["match"]]
            rule = next((r for r in candidates if all(claims.get(k) == v for k, v in r["match"].items())), None)
            if rule is None:
                if icmp and table == 1:
                    return True, reason + " (monitoring ping)"
                return False, f"no rule in table {table}"
            if rule["action"] == "drop":
                return False, rule["reason"]
            reason = rule["reason"]
        return True, reason


def flow_claims(topo: "Topology", dpid: int, in_port: int, src_mac: str, src_ip: str, dst_ip: str,
                dst_port: Optional[int]) -> dict:
    dst = "gateway" if dst_ip in topo.gateways else (topo.host_by_ip.get(dst_ip) or {}).get("name", dst_ip)
    return {"src": topo.edge_port.get((dpid, in_port), "unknown"), "src_ip": src_ip,
            "src_mac": src_mac.lower(), "dst": dst, "port": dst_port}


def microflow_match(in_port: int, src_mac: str, src_ip: str, dst_ip: str, proto: int,
                    dst_port: Optional[int]) -> dict:
    match = {"in_port": in_port, "eth_src": src_mac, "eth_type": IPV4, "ipv4_src": src_ip,
             "ipv4_dst": dst_ip, "ip_proto": proto}
    if proto == 6 and dst_port is not None:
        match["tcp_dst"] = dst_port
    elif proto == 17 and dst_port is not None:
        match["udp_dst"] = dst_port
    return match


class FlowLog:
    """Flows seen at host ports, with cumulative counters from the switch."""

    def __init__(self, keep_s: float = 120.0, max_flows: int = 5000):
        self.keep_s = keep_s
        self.max_flows = max_flows
        self.flows: Dict[int, dict] = {}
        self._next = 1

    def add(self, now: float, **fields) -> dict:
        flow = dict(fields, id=self._next, first_seen=now, last_seen=now, packets=1,
                    bytes=fields.get("bytes", 64))
        self.flows[flow["id"]] = flow
        self._next += 1
        if len(self.flows) > self.max_flows:
            for fid in sorted(self.flows)[: len(self.flows) - self.max_flows]:
                del self.flows[fid]
        return flow

    def update(self, flow_id: int, packets: int, bytes_: int, now: float) -> None:
        flow = self.flows.get(flow_id)
        if flow is None:
            return
        if packets > flow["packets"] or bytes_ > flow["bytes"]:
            flow["last_seen"] = now
        flow["packets"] = max(flow["packets"], packets)
        flow["bytes"] = max(flow["bytes"], bytes_)

    def prune(self, now: float) -> None:
        for fid in [f for f, v in self.flows.items() if now - v["last_seen"] > self.keep_s]:
            del self.flows[fid]

    def since(self, after_id: int = 0, active_s: Optional[float] = None, now: Optional[float] = None) -> list:
        rows = [f for f in self.flows.values()
                if f["id"] > after_id or (active_s is not None and now - f["last_seen"] <= active_s)]
        return sorted(rows, key=lambda f: f["id"])


def normalise_block_match(match: dict) -> dict:
    """Validate a block's match and add the prerequisite fields OpenFlow
    needs (eth_type for IP fields, ip_proto for ports)."""
    if not match:
        raise ValueError("a block needs at least one match field")
    bad = set(match) - set(BLOCK_FIELDS)
    if bad:
        raise ValueError(f"unsupported match fields: {sorted(bad)} (allowed: {list(BLOCK_FIELDS)})")
    out = dict(match)
    for key in ("ipv4_src", "ipv4_dst"):
        if key in out:
            ipaddress.ip_network(out[key], strict=False)
    if "tcp_dst" in out and "udp_dst" in out:
        raise ValueError("use tcp_dst or udp_dst, not both")
    if "tcp_dst" in out:
        out.setdefault("ip_proto", 6)
        if out["ip_proto"] != 6:
            raise ValueError("tcp_dst needs ip_proto 6")
    if "udp_dst" in out:
        out.setdefault("ip_proto", 17)
        if out["ip_proto"] != 17:
            raise ValueError("udp_dst needs ip_proto 17")
    if any(k in out for k in ("ipv4_src", "ipv4_dst", "ip_proto")):
        out["eth_type"] = IPV4
    for k in ("ip_proto", "tcp_dst", "udp_dst"):
        if k in out:
            out[k] = int(out[k])
    return out


def covers(rules: List[dict], dpid: int, eth_dst: str) -> bool:
    """Would a packet with ``eth_dst`` match one of ``rules`` on ``dpid``?"""
    return any(r["dpid"] == dpid and r["match"].get("eth_dst", eth_dst) == eth_dst for r in rules)


def _rule(dpid: int, priority: int, match: dict, actions: list) -> dict:
    return {"dpid": dpid, "table": FWD_TABLE, "priority": priority, "match": match,
            "actions": actions, "cookie": COOKIE_FWD}
