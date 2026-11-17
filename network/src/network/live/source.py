"""Live telemetry source (Module 2 on the real network).

Reads the self-healing Ryu app once per collection:

* ``/sh/stats/links`` — measured one-way latency and probe loss per link
  (active probes, see ``controller/apps/selfhealing_app.py``) and whether the
  link has carrier (ports up, switches connected);
* ``/sh/topology`` — which switch port each link/host uses;
* ``/sh/stats/ports`` — OpenFlow port counters (bytes, packets, drops). They
  reset when a switch restarts, like the simulated counters.

Status follows BFD semantics: a link is up only if it has carrier AND its
probes get through (a link that drops every probe is down even though the
controller still believes it is connected — a "physical" fault). Node CPU
and memory are not reported: every Mininet node shares one kernel, so
per-node figures would be fiction. All samples are ``simulated=False``.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from network.telemetry.metrics import Metric, Sample, make_sample
from network.telemetry.sources.base import TelemetrySource

LINE_RATE_MBPS = 1000.0
MIN_PROBES = 3  # probes needed before a 100 % loss reading marks a link down


class LiveSource(TelemetrySource):
    simulated = False

    def __init__(self, controller, client, name: str = "live", line_rate_mbps: float = LINE_RATE_MBPS):
        self._controller = controller
        self._client = client
        self.name = name
        self.line_rate_mbps = line_rate_mbps

    def collect(self, timestamp: float) -> List[Sample]:
        state = self._controller.state
        impairments = self._controller.impairments()
        measured = self._client.links().get("links", {})
        topo = self._client.topology()
        ports = self._client.ports()
        switch_up = {s["name"]: bool(s["connected"]) for s in topo.get("switches", [])}
        port_of = self._port_map(topo)
        running = bool(state.is_running)

        samples: List[Sample] = []
        link_up: Dict[str, bool] = {}
        for link in sorted(state.links.values(), key=lambda l: l.name):
            m = measured.get(link.name, {})
            online = all(self._node_online(state, n, switch_up) for n in (link.src, link.dst))
            dead = (m.get("probes") or 0) >= MIN_PROBES and m.get("loss_pct") == 100.0
            up = running and online and bool(m.get("carrier")) and not dead
            link_up[link.name] = up
            labels = {"src": link.src, "dst": link.dst}

            def sample(metric, value, _name=link.name, _labels=labels):
                return make_sample(metric, _name, value, timestamp, self.name, False, _labels)

            capacity = float(impairments.get(link.name, {}).get("bandwidth_mbps", self.line_rate_mbps))
            samples += [sample(Metric.LINK_STATUS, 1.0 if up else 0.0),
                        sample(Metric.LINK_CAPACITY_MBPS, capacity)]
            if up:
                if m.get("latency_ms") is not None:
                    samples.append(sample(Metric.LINK_LATENCY_MS, float(m["latency_ms"])))
                if m.get("loss_pct") is not None:
                    samples.append(sample(Metric.LINK_PACKET_LOSS_PCT, float(m["loss_pct"])))
            else:
                samples.append(sample(Metric.LINK_PACKET_LOSS_PCT, 100.0))
            counters = self._counters(ports, port_of.get(link.name))
            if counters is not None:
                samples += [sample(Metric.LINK_BYTES, counters["rx_bytes"] + counters["tx_bytes"]),
                            sample(Metric.LINK_PACKETS, counters["rx_packets"] + counters["tx_packets"]),
                            sample(Metric.LINK_DROPPED_PACKETS, counters["rx_dropped"] + counters["tx_dropped"])]

        for name, node in sorted(state.nodes.items()):
            up = running and self._node_online(state, name, switch_up)
            if up and node.kind in ("router", "switch"):
                # physically dead switch: connected to Ryu, but no link of it passes probes
                mine = [l for l in state.links.values() if name in (l.src, l.dst)]
                probed = [l for l in mine if (measured.get(l.name, {}).get("probes") or 0) >= MIN_PROBES]
                if probed and all(measured[l.name].get("loss_pct") == 100.0 for l in probed):
                    up = False
            samples.append(make_sample(Metric.NODE_STATUS, name, 1.0 if up else 0.0, timestamp, self.name,
                                       False, {"kind": node.kind}))
        return samples

    @staticmethod
    def _node_online(state, name: str, switch_up: Dict[str, bool]) -> bool:
        node = state.nodes.get(name)
        if node is None or not node.is_online:
            return False
        if node.kind in ("router", "switch"):
            return switch_up.get(name, False)
        return True

    @staticmethod
    def _port_map(topo: dict) -> Dict[str, tuple]:
        """link name -> (switch, port) whose counters describe the link."""
        out = {}
        for link in topo.get("links", []):
            out["-".join(sorted((link["src"], link["dst"])))] = (link["src"], link["src_port"])
        for host, loc in (topo.get("hosts") or {}).items():
            out["-".join(sorted((host, loc["switch"])))] = (loc["switch"], loc["port"])
        return out

    @staticmethod
    def _counters(ports: dict, where: Optional[tuple]) -> Optional[dict]:
        if where is None:
            return None
        switch, port = where
        return ((ports.get(switch) or {}).get("ports") or {}).get(str(port))

    def describe(self) -> dict:
        info = super().describe()
        info.update(controller=self._client.base_url, line_rate_mbps=self.line_rate_mbps)
        return info
