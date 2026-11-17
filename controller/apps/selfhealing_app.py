"""Self-healing Ryu app: loop-safe routing, proxy ARP, blocks, stats, REST.

Replaces the Module 1 L2 learning switch. The forwarding decisions live in
``sh_logic`` (pure Python, unit tested); this file only talks OpenFlow 1.3
and serves the REST API on ``--wsapi-port`` (default 8080):

  GET    /sh/health                    app status, loaded topology
  GET    /sh/topology                  switches, links (live?), located hosts
  PUT    /sh/config                    {"topology": "file.yaml"} load another topology
  GET    /sh/routes                    per destination: path from every switch
  PUT    /sh/routes/{dst_ip}           {"path": ["sw-b", "rtr-core", "sw-a"]} pin a path
  DELETE /sh/routes/{dst_ip}           unpin
  GET    /sh/blocks                    active blocks
  POST   /sh/blocks                    {"match": {...}, "reason": "...", "switch": opt.}
  DELETE /sh/blocks/{id}               remove a block
  GET    /sh/stats/ports               latest port counters (polled every STATS_INTERVAL s)
  GET    /sh/stats/flows               latest flow counters

Tables: 0 = access control (blocks; default: go to table 1),
1 = forwarding (ARP -> controller, per-destination IPv4 rules, miss ->
controller). Nothing is flooded over switch-to-switch links.
"""

import json
import os
import sys
import time

import yaml
from ryu.app.wsgi import ControllerBase, Response, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, icmp, ipv4, packet
from ryu.ofproto import ofproto_v1_3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sh_logic as L  # noqa: E402

APP_KEY = "selfhealing_app"
CONFIG_DIR = os.environ.get("SH_CONFIG_DIR", "/opt/controller/config")
TOPOLOGY_FILE = os.environ.get("SH_TOPOLOGY", "enterprise.yaml")
STATS_INTERVAL = float(os.environ.get("SH_STATS_INTERVAL", "2"))
FLOW_IDLE_TIMEOUT = 0  # rules are removed explicitly when paths change


def load_topology(name):
    name = os.path.basename(name)  # no paths outside the config dir
    with open(os.path.join(CONFIG_DIR, name)) as fh:
        topo = yaml.safe_load(fh)
    ident_path = os.path.join(CONFIG_DIR, "identities.yaml")
    identities = None
    if os.path.isfile(ident_path):
        with open(ident_path) as fh:
            identities = yaml.safe_load(fh)
    return L.Topology(topo, identities), name


class SelfHealingApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        topo, self.topology_file = load_topology(TOPOLOGY_FILE)
        self.state = L.ForwardingState(topo)
        self.datapaths = {}
        self.ports = {}           # dpid -> {port_no: name}
        self.port_stats = {}      # name -> {"time": t, "ports": {no: counters}}
        self.flow_stats = {}
        self.installed = set()    # destination IPs with rules installed
        self.started = time.time()
        kwargs["wsgi"].register(RestApi, {APP_KEY: self})
        self._monitor = hub.spawn(self._poll_stats)
        self.logger.info("selfhealing: topology %s (%d switches, %d hosts)",
                         self.topology_file, len(topo.dpid_of), len(topo.hosts))

    # ------------------------------------------------------------------
    # OpenFlow helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _match(parser, fields):
        return parser.OFPMatch(**fields)

    def _actions(self, dp, actions):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        out = []
        for act in actions:
            kind = act[0]
            if kind == "output":
                out.append(parser.OFPActionOutput(act[1]))
            elif kind == "set_eth_src":
                out.append(parser.OFPActionSetField(eth_src=act[1]))
            elif kind == "set_eth_dst":
                out.append(parser.OFPActionSetField(eth_dst=act[1]))
            elif kind == "dec_ttl":
                out.append(parser.OFPActionDecNwTtl())
            elif kind == "controller":
                out.append(parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER))
        return out

    def _install(self, rule):
        dp = self.datapaths.get(rule["dpid"])
        if dp is None:
            return
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = []
        if rule.get("goto") is not None:
            inst.append(parser.OFPInstructionGotoTable(rule["goto"]))
        elif rule["actions"]:
            inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, self._actions(dp, rule["actions"])))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=rule["table"], priority=rule["priority"],
                                      match=self._match(parser, rule["match"]), instructions=inst,
                                      cookie=rule["cookie"], idle_timeout=FLOW_IDLE_TIMEOUT))

    def _delete(self, dp, table, cookie, mask=0xFFFFFFFFFFFFFFFF):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      cookie=cookie, cookie_mask=mask, match=parser.OFPMatch()))

    def _packet_out(self, dp, port, data, in_port=None):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        actions = [parser.OFPActionOutput(port)]
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port if in_port is not None else ofp.OFPP_CONTROLLER,
                                        actions=actions, data=data))

    # ------------------------------------------------------------------
    # Switch lifecycle
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        self.datapaths[dp.id] = dp
        self.ports.setdefault(dp.id, {})
        for table in (L.ACL_TABLE, L.FWD_TABLE):
            self._delete(dp, table, 0, 0)  # start clean (also after a restart)
        base = {"dpid": dp.id, "cookie": L.COOKIE_BASE, "actions": []}
        self._install(dict(base, table=L.ACL_TABLE, priority=0, match={}, goto=L.FWD_TABLE))
        self._install(dict(base, table=L.FWD_TABLE, priority=0, match={}, actions=[("controller",)]))
        self._install(dict(base, table=L.FWD_TABLE, priority=L.PRIO_ARP, match={"eth_type": L.ARP},
                           actions=[("controller",)]))
        for ethertype in (0x86DD, 0x88CC):  # IPv6, LLDP: not used, never to the controller
            self._install(dict(base, table=L.FWD_TABLE, priority=10, match={"eth_type": ethertype}))
        dp.send_msg(parser.OFPPortDescStatsRequest(dp, 0))
        self.logger.info("switch %s connected", self.state.topo.name_of.get(dp.id, hex(dp.id)))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def _port_desc(self, ev):
        dp = ev.msg.datapath
        changed = False
        for p in ev.msg.body:
            if p.port_no > dp.ofproto.OFPP_MAX:
                continue
            self.ports[dp.id][p.port_no] = p.name.decode() if isinstance(p.name, bytes) else p.name
            changed |= self.state.set_port(dp.id, p.port_no, self._live(dp, p))
        self.state.connected.add(dp.id)
        for block in self.state.blocks.values():
            for rule in self.state.block_rules(block):
                if rule["dpid"] == dp.id:
                    self._install(rule)
        self._recompute()

    @staticmethod
    def _live(dp, port):
        ofp = dp.ofproto
        return not (port.state & ofp.OFPPS_LINK_DOWN) and not (port.config & ofp.OFPPC_PORT_DOWN)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dp, port = msg.datapath, msg.desc
        if port.port_no > dp.ofproto.OFPP_MAX:
            return
        live = msg.reason != dp.ofproto.OFPPR_DELETE and self._live(dp, port)
        name = self.state.topo.name_of.get(dp.id, hex(dp.id))
        if msg.reason == dp.ofproto.OFPPR_DELETE:
            self.ports.get(dp.id, {}).pop(port.port_no, None)
        else:
            self.ports.setdefault(dp.id, {})[port.port_no] = (
                port.name.decode() if isinstance(port.name, bytes) else port.name)
        if self.state.set_port(dp.id, port.port_no, live):
            self.logger.info("link change at %s port %s -> %s; recomputing paths",
                             name, port.port_no, "up" if live else "down")
            self._recompute()

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def _switch_gone(self, ev):
        dp = ev.datapath
        if dp is None or dp.id is None:
            return
        if self.datapaths.get(dp.id) is dp:
            self.datapaths.pop(dp.id, None)
            self.state.connected.discard(dp.id)
            self.logger.info("switch %s disconnected; recomputing paths",
                             self.state.topo.name_of.get(dp.id, hex(dp.id)))
            self._recompute()

    def _recompute(self):
        """Remove every forwarding rule and reinstall them for all located
        hosts on the current live topology."""
        for dp in list(self.datapaths.values()):
            self._delete(dp, L.FWD_TABLE, L.COOKIE_FWD)
        self.installed.clear()
        for host in self.state.topo.hosts.values():
            if host["mac"] in self.state.host_loc:
                self._install_destination(host["ip"])

    def _install_destination(self, ip):
        rules = self.state.plan(ip)
        for rule in rules:
            self._install(rule)
        if rules:
            self.installed.add(ip)
        return bool(rules)

    # ------------------------------------------------------------------
    # Packet-in: ARP, gateway ping, first packets to a destination
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        if self.state.learn(eth.src, dp.id, in_port):
            host = self.state.topo.host_by_mac.get(eth.src)
            if host:
                self.logger.info("located %s at %s port %s", host["name"],
                                 self.state.topo.name_of.get(dp.id), in_port)
                self._install_destination(host["ip"])
        a = pkt.get_protocol(arp.arp)
        if a is not None:
            self._handle_arp(dp, in_port, eth, a)
            return
        ip = pkt.get_protocol(ipv4.ipv4)
        if ip is None:
            return
        if ip.dst in self.state.topo.gateways:
            ic = pkt.get_protocol(icmp.icmp)
            if ic is not None and ic.type == icmp.ICMP_ECHO_REQUEST:
                self._gateway_echo(dp, in_port, eth, ip, ic)
            return
        if ip.dst not in self.state.topo.host_by_ip:
            return
        rules = self.state.plan(ip.dst)
        if not rules:
            self._probe(ip.dst)
            return
        if ip.dst not in self.installed:
            self._install_destination(ip.dst)
        if L.covers(rules, dp.id, eth.dst):
            # resubmit through the pipeline (table 0 blocks still apply);
            # only when a rule will match, so it cannot loop back here
            self._packet_out(dp, dp.ofproto.OFPP_TABLE, msg.data, in_port=in_port)

    def _handle_arp(self, dp, in_port, eth, a):
        if a.opcode != arp.ARP_REQUEST:
            return  # replies to our probes only teach locations (done above)
        mac = self.state.arp_answer(a.dst_ip)
        if mac is None or mac == a.src_mac:
            return
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(ethertype=L.ARP, dst=eth.src, src=mac))
        reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=mac, src_ip=a.dst_ip,
                                   dst_mac=a.src_mac, dst_ip=a.src_ip))
        reply.serialize()
        self._packet_out(dp, in_port, reply.data)

    def _gateway_echo(self, dp, in_port, eth, ip, ic):
        gw_mac = self.state.topo.gateways[ip.dst]
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(ethertype=L.IPV4, dst=eth.src, src=gw_mac))
        reply.add_protocol(ipv4.ipv4(dst=ip.src, src=ip.dst, proto=ip.proto))
        reply.add_protocol(icmp.icmp(type_=icmp.ICMP_ECHO_REPLY, code=0, csum=0, data=ic.data))
        reply.serialize()
        self._packet_out(dp, in_port, reply.data)

    def _probe(self, dst_ip):
        """ARP for an unlocated host from its gateway, on edge ports only."""
        sn = self.state.topo.subnet_of_ip(dst_ip)
        if sn is None:
            return
        req = packet.Packet()
        req.add_protocol(ethernet.ethernet(ethertype=L.ARP, dst="ff:ff:ff:ff:ff:ff", src=sn["gateway_mac"]))
        req.add_protocol(arp.arp(opcode=arp.ARP_REQUEST, src_mac=sn["gateway_mac"], src_ip=sn["gateway"],
                                 dst_mac="00:00:00:00:00:00", dst_ip=dst_ip))
        req.serialize()
        for dpid, dp in self.datapaths.items():
            for port in self.state.edge_ports(dpid, self.ports.get(dpid, {})):
                self._packet_out(dp, port, req.data)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def _error(self, ev):
        msg = ev.msg
        self.logger.warning("switch %s rejected a message: type=%s code=%s data=%s",
                            self.state.topo.name_of.get(msg.datapath.id, hex(msg.datapath.id)),
                            msg.type, msg.code, msg.data[:64].hex() if msg.data else "")

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    def _poll_stats(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
                dp.send_msg(parser.OFPFlowStatsRequest(dp, table_id=dp.ofproto.OFPTT_ALL))
            hub.sleep(STATS_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        dp = ev.msg.datapath
        name = self.state.topo.name_of.get(dp.id, hex(dp.id))
        ports = {}
        for s in ev.msg.body:
            if s.port_no > dp.ofproto.OFPP_MAX:
                continue
            ports[s.port_no] = {
                "name": self.ports.get(dp.id, {}).get(s.port_no),
                "live": (dp.id, s.port_no) not in self.state.port_down,
                "rx_packets": s.rx_packets, "tx_packets": s.tx_packets,
                "rx_bytes": s.rx_bytes, "tx_bytes": s.tx_bytes,
                "rx_dropped": s.rx_dropped, "tx_dropped": s.tx_dropped,
                "rx_errors": s.rx_errors, "tx_errors": s.tx_errors,
                "duration": s.duration_sec + s.duration_nsec / 1e9,
            }
        self.port_stats[name] = {"time": time.time(), "ports": ports}

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats(self, ev):
        dp = ev.msg.datapath
        name = self.state.topo.name_of.get(dp.id, hex(dp.id))
        flows = [{"table": f.table_id, "priority": f.priority, "cookie": f.cookie,
                  "match": dict(f.match.items()), "packets": f.packet_count, "bytes": f.byte_count}
                 for f in ev.msg.body]
        self.flow_stats[name] = {"time": time.time(), "flows": flows}

    # ------------------------------------------------------------------
    # REST operations
    # ------------------------------------------------------------------
    def topology_view(self):
        st, topo = self.state, self.state.topo
        located = {}
        for mac, (dpid, port) in st.host_loc.items():
            host = topo.host_by_mac.get(mac)
            located[host["name"] if host else mac] = {"switch": topo.name_of.get(dpid), "port": port}
        return {
            "topology": self.topology_file,
            "switches": [{"name": n, "dpid": f"{d:016x}", "connected": d in st.connected,
                          "ports": {str(p): nm for p, nm in sorted(self.ports.get(d, {}).items())}}
                         for n, d in sorted(topo.dpid_of.items())],
            "links": [{"src": topo.name_of[a], "src_port": pa, "dst": topo.name_of[b], "dst_port": pb,
                       "live": st.link_live(a, pa, b, pb)} for a, pa, b, pb in topo.links],
            "hosts": located,
        }

    def set_topology(self, name):
        topo, fname = load_topology(name)
        blocks = self.state.blocks
        self.state = L.ForwardingState(topo)
        self.state.blocks = blocks
        self.topology_file = fname
        for dpid, dp in self.datapaths.items():
            if dpid in topo.name_of:
                self.state.connected.add(dpid)
            dp.send_msg(dp.ofproto_parser.OFPPortDescStatsRequest(dp, 0))  # refresh port liveness
        self._recompute()
        return {"topology": fname}

    def pin(self, dst_ip, path):
        err = self.state.validate_path(dst_ip, path)
        if err:
            raise ValueError(err)
        self.state.pinned[dst_ip] = list(path)
        self._recompute()
        return self.state.route_summary(dst_ip)

    def unpin(self, dst_ip):
        self.state.pinned.pop(dst_ip, None)
        self._recompute()
        return self.state.route_summary(dst_ip)

    def add_block(self, body):
        block = self.state.add_block(body.get("match") or {}, body.get("reason", ""), body.get("switch"))
        for rule in self.state.block_rules(block):
            self._install(rule)
        return block

    def remove_block(self, block_id):
        block = self.state.blocks.pop(block_id, None)
        if block is None:
            raise KeyError(block_id)
        for dp in self.datapaths.values():
            self._delete(dp, L.ACL_TABLE, L.COOKIE_BLOCK + block_id)
        return block


def _json(data, status=200):
    return Response(status=status, content_type="application/json", charset="utf-8",
                    body=json.dumps(data, sort_keys=True).encode())


def _body(req):
    try:
        return json.loads(req.body or b"{}")
    except ValueError:
        return None


class RestApi(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[APP_KEY]

    @route("sh", "/sh/health", methods=["GET"])
    def health(self, req, **_):
        app = self.app
        return _json({"ok": True, "topology": app.topology_file, "uptime": time.time() - app.started,
                      "switches_connected": sorted(app.state.topo.name_of.get(d, hex(d))
                                                   for d in app.state.connected)})

    @route("sh", "/sh/topology", methods=["GET"])
    def topology(self, req, **_):
        return _json(self.app.topology_view())

    @route("sh", "/sh/config", methods=["PUT"])
    def config(self, req, **_):
        body = _body(req)
        if not body or "topology" not in body:
            return _json({"error": "body must be {\"topology\": \"file.yaml\"}"}, 400)
        try:
            return _json(self.app.set_topology(body["topology"]))
        except (OSError, KeyError, ValueError) as exc:
            return _json({"error": str(exc)}, 400)

    @route("sh", "/sh/routes", methods=["GET"])
    def routes(self, req, **_):
        st = self.app.state
        return _json({ip: st.route_summary(ip) for ip in sorted(st.topo.host_by_ip)})

    @route("sh", "/sh/routes/{dst}", methods=["PUT"])
    def pin(self, req, dst, **_):
        body = _body(req)
        if not body or not isinstance(body.get("path"), list):
            return _json({"error": "body must be {\"path\": [switch names]}"}, 400)
        try:
            return _json(self.app.pin(dst, body["path"]))
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)

    @route("sh", "/sh/routes/{dst}", methods=["DELETE"])
    def unpin(self, req, dst, **_):
        if dst not in self.app.state.topo.host_by_ip:
            return _json({"error": f"unknown destination {dst}"}, 404)
        return _json(self.app.unpin(dst))

    @route("sh", "/sh/blocks", methods=["GET"])
    def blocks(self, req, **_):
        return _json(sorted(self.app.state.blocks.values(), key=lambda b: b["id"]))

    @route("sh", "/sh/blocks", methods=["POST"])
    def add_block(self, req, **_):
        body = _body(req)
        if body is None:
            return _json({"error": "invalid JSON"}, 400)
        try:
            return _json(self.app.add_block(body), 201)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)

    @route("sh", "/sh/blocks/{block_id}", methods=["DELETE"])
    def remove_block(self, req, block_id, **_):
        try:
            return _json(self.app.remove_block(int(block_id)))
        except (KeyError, ValueError):
            return _json({"error": f"no block {block_id}"}, 404)

    @route("sh", "/sh/stats/ports", methods=["GET"])
    def port_stats(self, req, **_):
        return _json(self.app.port_stats)

    @route("sh", "/sh/stats/flows", methods=["GET"])
    def flow_stats(self, req, **_):
        return _json(self.app.flow_stats)
