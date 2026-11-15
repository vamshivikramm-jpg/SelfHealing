"""Policy enforcement as switch rules (Submodule 5.6).

Compiles the policy, identities and current trust into OpenFlow-style
rules in two tables, the usual Zero Trust split between *who you are*
and *what you may do*:

Table 0 — identity binding (port security)
    one allow rule per device: packets from that device's port must carry
    its own MAC and IP; anything else from the port is dropped.

Table 1 — access
    priority 300  drop everything from a quarantined device
    priority 310  ...except to its gateway (if the policy allows it)
    priority 100  allow role -> destination:port, only for devices whose
                  current trust meets the rule's min_trust
    priority 0    drop (default deny)

``SimulatedEnforcer`` keeps an in-memory flow table, applies rule sets
(reporting what changed) and admits or blocks simulated flows. Installing
the same rules on Open vSwitch through Ryu needs the Linux lab;
``FlowRule.as_openflow()`` gives the match/actions in the shape Ryu's
``ofctl_rest`` API expects, but that path is NOT tested yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from network.security.policy import GATEWAY

ALLOW = "allow"
DROP = "drop"


@dataclass(frozen=True)
class FlowRule:
    table: int
    priority: int
    match: Tuple[Tuple[str, object], ...]
    action: str
    reason: str
    subject: Optional[str] = None

    def match_dict(self) -> Dict[str, object]:
        return dict(self.match)

    def as_openflow(self, registry) -> dict:
        """Ryu ofctl_rest-style flow entry (untested against a real switch)."""
        m = self.match_dict()
        match: Dict[str, object] = {"eth_type": 0x0800}
        if "src" in m:
            identity = registry.get(m["src"])
            match["ipv4_src"] = identity.ip
            if self.table == 0:
                match["eth_src"] = identity.mac
        if "dst" in m:
            if m["dst"] == GATEWAY and "src" in m:
                match["ipv4_dst"] = registry.get(m["src"]).gateway
            else:
                match["ipv4_dst"] = registry.get(m["dst"]).ip
        if "port" in m:
            match["ip_proto"] = 6
            match["tcp_dst"] = m["port"]
        if self.action == ALLOW:
            actions = [{"type": "GOTO_TABLE", "table_id": 1}] if self.table == 0 else \
                [{"type": "OUTPUT", "port": "NORMAL"}]
        else:
            actions = []
        return {"table_id": self.table, "priority": self.priority, "match": match, "actions": actions}


def _rule(table, priority, action, reason, subject=None, **match) -> FlowRule:
    return FlowRule(table, priority, tuple(sorted(match.items())), action, reason, subject)


def compile_rules(policy, registry, trust: Dict[str, float], quarantined: Iterable[str]) -> List[FlowRule]:
    quarantined = set(quarantined)
    rules: List[FlowRule] = []
    for identity in registry.all():
        rules.append(_rule(0, 100, ALLOW, f"{identity.name} presents its own MAC/IP", identity.name,
                           src=identity.name, src_ip=identity.ip, src_mac=identity.mac))
    rules.append(_rule(0, 0, DROP, "unbound source (spoofed or unknown)"))
    for name in sorted(quarantined):
        rules.append(_rule(1, 300, DROP, f"{name} is quarantined", name, src=name))
        if policy.allow_gateway_when_quarantined:
            rules.append(_rule(1, 310, ALLOW, f"{name} may reach its gateway", name, src=name, dst=GATEWAY))
    for rule in policy.rules:
        for identity in registry.all():
            if identity.role not in rule.from_roles or identity.name in quarantined:
                continue
            if trust.get(identity.name, 0.0) < rule.min_trust:
                continue
            for dst in rule.to:
                for port in rule.ports:
                    rules.append(_rule(1, 100, ALLOW, f"{rule.name}", identity.name,
                                       src=identity.name, dst=dst, port=port))
    rules.append(_rule(1, 0, DROP, "default deny"))
    return rules


class SimulatedEnforcer:
    def __init__(self):
        self._rules: List[FlowRule] = []
        self.blocked = 0
        self.admitted = 0

    @property
    def rules(self) -> List[FlowRule]:
        return list(self._rules)

    def apply(self, rules: List[FlowRule]) -> Dict[str, List[FlowRule]]:
        """Replace the table; returns the rules added and removed."""
        old, new = set(self._rules), set(rules)
        self._rules = sorted(rules, key=lambda r: (r.table, -r.priority, r.match))
        return {"added": sorted(new - old, key=lambda r: (r.table, -r.priority, r.match)),
                "removed": sorted(old - new, key=lambda r: (r.table, -r.priority, r.match))}

    def admit(self, flow) -> Tuple[bool, FlowRule]:
        """Run a flow through table 0 then table 1."""
        claims = {"src": flow.src, "src_ip": flow.src_ip, "src_mac": flow.src_mac.lower(),
                  "dst": flow.dst, "port": flow.dst_port}
        for table in (0, 1):
            rule = next(r for r in self._rules if r.table == table and _matches(r, claims))
            if rule.action == DROP:
                self.blocked += 1
                return False, rule
        self.admitted += 1
        return True, rule


def _matches(rule: FlowRule, claims: Dict[str, object]) -> bool:
    return all(claims.get(k) == v for k, v in rule.match)
