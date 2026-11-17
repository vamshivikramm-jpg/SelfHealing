"""Builds and runs a Mininet network from a topology config (Submodule 1.2)."""

from __future__ import annotations

import os
from typing import Optional

import yaml
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import RemoteController

from network import devices
from network.config import TopologyConfig, load_topology_config
from network.enterprise_topology import EnterpriseTopology

DEFAULT_CONFIG_PATH = "config/enterprise.yaml"

CONTROLLER_IP_ENV = "RYU_CONTROLLER_IP"
CONTROLLER_PORT_ENV = "RYU_CONTROLLER_PORT"


def load_identity_macs(path: str) -> dict:
    """Host/server MAC addresses from identities.yaml (Zero Trust expects
    devices to present these). Missing file -> Mininet picks MACs."""
    if not os.path.isfile(path):
        return {}
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return {name: d["mac"] for name, d in (data.get("devices") or {}).items() if d.get("mac")}


class NetworkBuilder:
    """Factory for the virtual enterprise network.

    Builds a Mininet instance from a validated topology config and binds
    its switches to the external Ryu controller (a RemoteController).
    """

    def __init__(
        self,
        config: Optional[TopologyConfig] = None,
        config_path: Optional[str] = None,
        controller_ip: Optional[str] = None,
        controller_port: Optional[int] = None,
    ):
        self.config = config or load_topology_config(config_path or DEFAULT_CONFIG_PATH)
        self.controller_ip = controller_ip or os.environ.get(CONTROLLER_IP_ENV, "ryu-controller")
        self.controller_port = int(
            controller_port or os.environ.get(CONTROLLER_PORT_ENV, "6653")
        )
        self.net: Optional[Mininet] = None
        self.macs = load_identity_macs(
            os.path.join(os.path.dirname(config_path or DEFAULT_CONFIG_PATH), "identities.yaml")
        )

    def build(self) -> Mininet:
        """Create (but do not start) the Mininet network."""
        topo = EnterpriseTopology(self.config, macs=self.macs)
        # controller=None + addController keeps this compatible across
        # Mininet versions (newer releases dropped **params on Mininet()).
        self.net = Mininet(
            topo=topo,
            controller=None,
            host=devices.Host,
            switch=devices.Switch,
            link=TCLink,  # lets the controller add latency/loss/bandwidth limits
        )
        self.net.addController(
            "ryu",
            controller=RemoteController,
            ip=self.controller_ip,
            port=self.controller_port,
        )
        return self.net

    def start(self) -> Mininet:
        """Build if needed, start the network, attach config metadata."""
        if self.net is None:
            self.build()
        self.net.start()
        self._attach_metadata()
        return self.net

    def stop(self) -> None:
        """Stop the network and free its resources."""
        if self.net is not None:
            self.net.stop()
            self.net = None

    def cleanup(self) -> None:
        """Remove all Mininet artifacts (bridges, namespaces, interfaces).

        Safe to call whether or not the network is running.
        """
        from mininet.clean import cleanup

        cleanup()

    def _attach_metadata(self) -> None:
        for node in list(self.net.switches) + list(self.net.hosts):
            node.config = self.config.device(node.name)
