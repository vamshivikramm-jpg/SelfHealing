"""Ryu OpenFlow 1.3 controller app — datapath/topology registry.

Submodule 1.1 bootstrap. Maintains a live registry of switches and their
ports, and exposes a serializable topology snapshot. Deliberately contains
no forwarding, telemetry, or healing logic; later modules extend this app.

Extension points for future modules:
- Forwarding / flow installation (Module 1.2+)
- Metrics export (Module 2)
- Healing actions via ofctl (Module 7)
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3


class TopologyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._datapaths = {}
        self._ports = {}

    def _register(self, dp):
        self._datapaths[dp.id] = dp
        self._ports[dp.id] = {p.port_no: p for p in dp.ports.values()}
        self.logger.info(
            "switch joined dpid=%s ports=%d", hex(dp.id), len(self._ports[dp.id])
        )

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _handle_switch_features(self, ev):
        self._register(ev.msg.datapath)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _handle_port_status(self, ev):
        msg = ev.msg
        dpid = msg.datapath.id
        port = msg.desc.port_no
        self.logger.info(
            "port status dpid=%s port=%s reason=%s", hex(dpid), port, msg.reason
        )
        if dpid in self._ports:
            if msg.reason in (msg.datapath.ofproto.OFPPR_DELETE,):
                self._ports[dpid].pop(port, None)
            else:
                self._ports[dpid][port] = msg.desc

    def get_topology(self):
        """Serializable snapshot of the known data plane.

        Consumed by future modules (telemetry, decision engine, dashboard).
        """
        return {
            "switches": [
                {
                    "dpid": hex(dpid),
                    "ports": sorted(self._ports.get(dpid, {})),
                }
                for dpid in sorted(self._datapaths)
            ]
        }
