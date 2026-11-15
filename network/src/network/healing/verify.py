"""Verification of post-conditions (Submodule 7.4).

Each check looks at what the network *does*, observed after the action,
never at whether the command returned successfully:

| Check | Passes when |
|---|---|
| ``link_up`` | telemetry reports the link up (telemetry reads physical truth) |
| ``node_up`` | telemetry reports the node up |
| ``route_avoids`` | the installed route avoids the link/node AND the demand is actually delivered (>= 90 %) |
| ``utilization_below`` | telemetry utilization is under the target (simulated load does not follow routes, so this can fail even when the move was right) |
| ``access_blocked`` | a test flow from the device is dropped by the switch rules |
"""

from __future__ import annotations

from typing import Optional, Tuple

from network.decision.graph import path_links
from network.telemetry.metrics import Metric

DELIVERED_OK = 0.9


class Verifier:
    def __init__(self, collector, service, security=None):
        self.collector = collector
        self.service = service
        self.security = security

    def check(self, post: dict, ts: float) -> Tuple[bool, str]:
        kind = post.get("check")
        store = self.collector.store
        if kind == "link_up":
            value = store.latest_value(Metric.LINK_STATUS, post["link"])
            return value == 1.0, f"link {post['link']} status {value}"
        if kind == "node_up":
            value = store.latest_value(Metric.NODE_STATUS, post["node"])
            return value == 1.0, f"node {post['node']} status {value}"
        if kind == "route_avoids":
            pair = (post["src"], post["dst"])
            path = self.service.routes.get(pair, ())
            avoids = (post["link"] not in path_links(path)) if "link" in post else (post["node"] not in path[1:-1])
            delivered = self.service.latest(pair)
            ok = avoids and delivered is not None and delivered >= DELIVERED_OK
            return ok, f"{pair}: avoids={avoids}, delivered={delivered}"
        if kind == "utilization_below":
            value = store.latest_value(Metric.LINK_UTILIZATION_PCT, post["link"])
            ok = value is not None and value < post["pct"]
            return ok, f"{post['link']} utilization {value} (target < {post['pct']})"
        if kind == "access_blocked":
            return self._blocked(post["subject"])
        return False, f"unknown check {kind!r}"

    def _blocked(self, subject: str) -> Tuple[bool, str]:
        if self.security is None:
            return False, "no enforcement backend"
        from network.security.records import FlowRecord

        identity = self.security.registry.get(subject)
        target = next((i.name for i in self.security.registry.all() if i.role == "web_server"), subject)
        probe = FlowRecord(0.0, subject, identity.ip, identity.mac, target, 443)
        enforcer = self.security.enforcer
        counters = (enforcer.blocked, enforcer.admitted)
        allowed, rule = enforcer.admit(probe)
        enforcer.blocked, enforcer.admitted = counters  # a probe is not traffic
        return (not allowed), f"probe from {subject} {'allowed' if allowed else 'dropped'} ({rule.reason})"
