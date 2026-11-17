"""Enterprise topology graph built from a validated config (Submodule 1.2)."""

from __future__ import annotations

from typing import Dict, Optional

from mininet.topo import Topo

from network import devices
from network.config import TopologyConfig

DEVICE_CLASSES = {
    "router": devices.Router,
    "switch": devices.Switch,
    "host": devices.Host,
    "server": devices.Server,
}


class EnterpriseTopology(Topo):
    """Mininet Topo subclass constructed from a :class:`TopologyConfig`.

    Devices are added with their concrete device classes so Mininet
    instantiates the right types (Router/Switch vs Host/Server). Hosts get
    static IPs and a default route to their subnet gateway.
    """

    def __init__(self, config: TopologyConfig, macs: Optional[Dict[str, str]] = None):
        self.config = config
        self._macs = dict(macs or {})
        self._device_index = {d.name: d for d in config.devices}
        super().__init__()
        self._build()

    def _build(self) -> None:
        for dev in self.config.devices:
            cls = DEVICE_CLASSES[dev.kind]
            if dev.kind in ("router", "switch"):
                self.addSwitch(
                    dev.name,
                    cls=cls,
                    dpid=dev.datapath_id,
                    protocols="OpenFlow13",
                )
            else:
                params = {"ip": dev.ip}
                if dev.gateway:
                    params["defaultRoute"] = f"via {dev.gateway}"
                if dev.name in self._macs:
                    params["mac"] = self._macs[dev.name]
                self.addHost(dev.name, cls=cls, **params)

        for link in self.config.links:
            self.addLink(link.src, link.dst, port1=link.src_port, port2=link.dst_port)

    @property
    def device_index(self) -> dict:
        return self._device_index
