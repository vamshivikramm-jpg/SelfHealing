"""Device classes for the virtual enterprise network (Submodule 1.2).

Each device type is a distinct class so later modules can dispatch on the
role. Routers and switches are Open vSwitch datapaths managed by the Ryu
controller; hosts and servers are isolated namespaces with static IPs.

Note: routing (L3 forwarding between subnets) is intentionally NOT
implemented here - it belongs to a later module. Router devices are
multi-subnet OpenFlow switches whose gateway interfaces are declared in
the topology configuration.
"""

from mininet.node import Host as _MininetHost
from mininet.node import OVSSwitch


class Switch(OVSSwitch):
    """Enterprise L2 switch (Open vSwitch datapath)."""

    ROLE = "switch"


class Router(OVSSwitch):
    """OpenFlow switch in a router role (gateway of one or more subnets).

    Interfaces (one per subnet) are declared in the topology config.
    Inter-subnet forwarding arrives with the routing/healing modules.
    """

    ROLE = "router"


class Host(_MininetHost):
    """End-user host device (network namespace with static IP)."""

    ROLE = "host"


class Server(_MininetHost):
    """Server device running enterprise services (thin Host marker)."""

    ROLE = "server"

    def __init__(self, name, services=None, **kwargs):
        super().__init__(name, **kwargs)
        self.services = list(services or [])
