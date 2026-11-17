"""Live lab back-ends (Mininet + Open vSwitch + the self-healing Ryu app).

The same interfaces the simulation uses, implemented on the real network:

* :class:`RyuClient` — REST client for ``controller/apps/selfhealing_app.py``.
* :class:`LiveSource` — telemetry from the controller's active link probes,
  OpenFlow port counters and the switches' connection state.
* :class:`LiveFaults` — "physical" faults the controller does not know
  about (all packets on a link dropped with ``tc``), like a cut fibre.
* :class:`LiveServiceModel` — traffic delivery measured with real pings,
  routes read back from the controller.
* :class:`LiveExecutor` / :class:`LiveVerifier` — healing actions as pinned
  paths and drop rules through the controller's REST API.
* :class:`LiveEnforcer`, :class:`LiveSecurityFeed`, :class:`LiveTraffic` —
  Zero Trust decided per flow in the switches, fed by real flows and logins
  from real services, users and attacks running in the hosts.
"""

from network.live.executor import LiveExecutor, LiveVerifier
from network.live.faults import LiveFaults
from network.live.ryu_client import RyuClient, RyuError
from network.live.security import LiveEnforcer, LiveSecurityFeed, LiveTraffic, attach_live_enforcer
from network.live.service import LiveServiceModel
from network.live.source import LiveSource

__all__ = ["LiveEnforcer", "LiveExecutor", "LiveFaults", "LiveSecurityFeed", "LiveServiceModel", "LiveSource",
           "LiveTraffic", "LiveVerifier", "RyuClient", "RyuError", "attach_live_enforcer"]
