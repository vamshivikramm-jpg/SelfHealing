"""Zero Trust policy decision point (Submodule 5.5).

Every access request is checked on its own, in this order:

1. ``DENY_UNKNOWN``     the device on the ingress port is not a known identity
2. ``DENY_IDENTITY``    the claimed IP/MAC do not belong to that device
3. ``DENY_QUARANTINE``  the device is quarantined (only its gateway is allowed)
4. ``DENY_NO_RULE``     no rule allows this role -> destination:port (default deny)
5. ``DENY_TRUST``       a rule matches but current trust is below its ``min_trust``
6. ``ALLOW``            otherwise

Never trust, always verify: identity and trust are re-evaluated per
request, so a device that was allowed a minute ago can be denied now.
Standard library + PyYAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

ALLOW = "ALLOW"
DENY_UNKNOWN = "DENY_UNKNOWN"
DENY_IDENTITY = "DENY_IDENTITY"
DENY_QUARANTINE = "DENY_QUARANTINE"
DENY_NO_RULE = "DENY_NO_RULE"
DENY_TRUST = "DENY_TRUST"
GATEWAY = "gateway"


@dataclass(frozen=True)
class PolicyRule:
    name: str
    from_roles: Tuple[str, ...]
    to: Tuple[str, ...]
    ports: Tuple[int, ...]
    min_trust: float

    def matches(self, role: str, dst: str, port: int) -> bool:
        return role in self.from_roles and dst in self.to and port in self.ports

    def to_dict(self) -> dict:
        return {"name": self.name, "from_roles": list(self.from_roles), "to": list(self.to),
                "ports": list(self.ports), "min_trust": self.min_trust}


@dataclass(frozen=True)
class TrustSettings:
    quarantine_below: float = 0.3
    release_above: float = 0.5
    trusted_at: float = 0.7
    posture_penalty: float = 0.1
    half_life_s: float = 300.0

    def __post_init__(self):
        if not 0 <= self.quarantine_below <= self.release_above <= 1:
            raise ValueError("need 0 <= quarantine_below <= release_above <= 1")
        if self.half_life_s <= 0:
            raise ValueError("half_life_s must be > 0")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    reason: str
    rule: Optional[str] = None
    trust: Optional[float] = None
    # Does any rule let this device's role make this request at all?
    # None when the identity check failed first.
    role_permits: Optional[bool] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ZeroTrustPolicy:
    rules: List[PolicyRule]
    trust: TrustSettings = field(default_factory=TrustSettings)
    anomaly_weights: Dict[str, float] = field(default_factory=dict)
    allow_gateway_when_quarantined: bool = True

    @classmethod
    def from_file(cls, path) -> "ZeroTrustPolicy":
        data = yaml.safe_load(Path(path).read_text()) or {}
        if data.get("default", "deny") != "deny":
            raise ValueError("only a default-deny policy is supported")
        rules = []
        names = set()
        for raw in data.get("rules") or []:
            rule = PolicyRule(
                name=str(raw["name"]), from_roles=tuple(raw["from_roles"]), to=tuple(raw["to"]),
                ports=tuple(int(p) for p in raw["ports"]), min_trust=float(raw.get("min_trust", 0.0)),
            )
            if rule.name in names:
                raise ValueError(f"duplicate rule name {rule.name!r}")
            if not 0 <= rule.min_trust <= 1:
                raise ValueError(f"rule {rule.name!r}: min_trust must be within [0, 1]")
            names.add(rule.name)
            rules.append(rule)
        trust = TrustSettings(**(data.get("trust") or {}))
        weights = {str(k): float(v) for k, v in (data.get("anomaly_weights") or {}).items()}
        allow_gw = bool((data.get("quarantine") or {}).get("allow_gateway", True))
        return cls(rules=rules, trust=trust, anomaly_weights=weights, allow_gateway_when_quarantined=allow_gw)

    def rule_for(self, role: str, dst: str, port: int) -> Optional[PolicyRule]:
        return next((r for r in self.rules if r.matches(role, dst, port)), None)

    def permits_statically(self, role: str, dst: str, port: int) -> bool:
        """Would a fully trusted device of this role be allowed? (ground truth helper)"""
        return self.rule_for(role, dst, port) is not None

    def decide(self, registry, src: str, src_ip: str, src_mac: str, dst: str, port: int,
               trust: float, quarantined: bool) -> Decision:
        identity = registry.get(src)
        if identity is None:
            return Decision(False, DENY_UNKNOWN, f"unknown device {src!r} on ingress port")
        mismatch = registry.verify(src, src_ip, src_mac)
        if mismatch:
            return Decision(False, DENY_IDENTITY, f"identity mismatch: {mismatch}", trust=trust)
        rule = self.rule_for(identity.role, dst, port)
        permits = rule is not None
        if quarantined:
            if dst == GATEWAY and self.allow_gateway_when_quarantined:
                return Decision(True, ALLOW, "quarantined device may reach its gateway", "quarantine-gateway",
                                trust, True)
            return Decision(False, DENY_QUARANTINE, f"{src} is quarantined (trust {trust:.2f})", trust=trust,
                            role_permits=permits)
        if rule is None:
            return Decision(False, DENY_NO_RULE,
                            f"no rule lets role {identity.role!r} reach {dst}:{port} (default deny)", trust=trust,
                            role_permits=False)
        if trust < rule.min_trust:
            return Decision(False, DENY_TRUST,
                            f"trust {trust:.2f} below {rule.min_trust:.2f} required by {rule.name}",
                            rule.name, trust, True)
        return Decision(True, ALLOW, f"allowed by {rule.name}", rule.name, trust, True)

    def to_dict(self) -> dict:
        return {
            "default": "deny",
            "rules": [r.to_dict() for r in self.rules],
            "trust": dict(self.trust.__dict__),
            "anomaly_weights": dict(self.anomaly_weights),
            "allow_gateway_when_quarantined": self.allow_gateway_when_quarantined,
        }
