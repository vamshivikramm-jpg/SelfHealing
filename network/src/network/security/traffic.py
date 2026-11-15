"""Simulated traffic and attacks (Submodule 5.2).

Generates, per time step, the flows and login attempts a small enterprise
would produce, from role profiles, plus labelled attacks:

- ``port_scan``         many ports on a server, one packet each
- ``brute_force``       repeated failed logins (and SSH connection attempts)
- ``spoofing``          flows that claim another device's IP and MAC
- ``lateral_movement``  repeated access to services the role may not use
- ``flood``             very high packet volume towards the web server

Benign users also make occasional *mistakes* (asking for a forbidden
service once) and occasional failed logins, so a good system must
tolerate some noise rather than punish every deny.

Everything here is SIMULATED and the attacks are SYNTHETIC: detection
results on this data show the pipeline catches these designed patterns,
not that it would catch real attackers.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from network.security.identity import IdentityRegistry
from network.security.records import MISTAKE, AuthEvent, FlowRecord

ATTACK_KINDS = ("port_scan", "brute_force", "spoofing", "lateral_movement", "flood")

# role -> list of (destination role, port, flows per step)
ROLE_PROFILES: Dict[str, List[Tuple[str, int, float]]] = {
    "admin": [("web_server", 443, 0.6), ("web_server", 22, 0.05), ("db_server", 22, 0.05)],
    "employee": [("web_server", 443, 0.8), ("web_server", 80, 0.2)],
    "contractor": [("web_server", 443, 0.4)],
    "web_server": [("db_server", 5432, 1.5)],
    "db_server": [],
}
# role -> (destination role, port) that a mistaken user might try
MISTAKES = {
    "admin": [("db_server", 5432)],
    "employee": [("web_server", 22), ("db_server", 5432), ("db_server", 22)],
    "contractor": [("web_server", 22), ("db_server", 5432), ("web_server", 8080)],
}


@dataclass(frozen=True)
class AttackSpec:
    kind: str
    attacker: str
    start_s: float
    duration_s: float
    target: str
    victim: Optional[str] = None  # spoofing: whose identity is claimed
    attack_id: str = ""

    def __post_init__(self):
        if self.kind not in ATTACK_KINDS:
            raise ValueError(f"unknown attack kind: {self.kind!r}")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if self.kind == "spoofing" and not self.victim:
            raise ValueError("spoofing needs a victim")

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    def active(self, t: float) -> bool:
        return self.start_s <= t < self.end_s


@dataclass(frozen=True)
class SecurityPlan:
    duration_s: float
    step_s: float = 5.0
    attacks: Tuple[AttackSpec, ...] = ()
    seed: int = 0
    mistake_rate: float = 0.005       # per device per step
    login_failure_rate: float = 0.02  # benign failed logins

    def to_dict(self) -> dict:
        return asdict(self)


def generate_security_plan(registry: IdentityRegistry, duration_s: float, seed: int = 0,
                           n_attacks: int = 5, step_s: float = 5.0, warmup_s: float = 600.0,
                           gap_s: Tuple[float, float] = (300.0, 900.0),
                           attack_duration_s: Tuple[float, float] = (60.0, 240.0),
                           kinds: Sequence[str] = ATTACK_KINDS) -> SecurityPlan:
    """Random but reproducible: a quiet warm-up, then non-overlapping attacks
    from randomly chosen hosts (never the admin workstation, whose identity
    is the spoofing target)."""
    rng = random.Random(seed)
    hosts = [i.name for i in registry.all() if i.kind == "host" and i.role != "admin"]
    admins = [i.name for i in registry.with_role("admin")]
    web = registry.with_role("web_server")[0].name
    db = registry.with_role("db_server")[0].name

    def snap(x):
        return round(x / step_s) * step_s

    attacks: List[AttackSpec] = []
    cursor = warmup_s
    for i in range(n_attacks):
        kind = rng.choice(list(kinds))
        attacker = rng.choice(hosts)
        target = web if kind == "flood" else rng.choice([web, db])
        victim = rng.choice(admins) if kind == "spoofing" else None
        if kind == "spoofing":
            target = db
        duration = snap(rng.uniform(*attack_duration_s))
        attacks.append(AttackSpec(kind, attacker, cursor, duration, target, victim, f"{kind}-{i + 1}"))
        cursor += duration + snap(rng.uniform(*gap_s))
    if attacks and attacks[-1].end_s > duration_s:
        raise ValueError(f"{n_attacks} attacks need about {int(attacks[-1].end_s)} s; increase duration_s")
    return SecurityPlan(duration_s=duration_s, step_s=step_s, attacks=tuple(attacks), seed=seed)


class TrafficGenerator:
    def __init__(self, registry: IdentityRegistry, plan: SecurityPlan):
        self.registry = registry
        self.plan = plan
        self._rng = random.Random(plan.seed * 7919 + 1)
        self._by_role: Dict[str, List[str]] = {}
        for identity in registry.all():
            self._by_role.setdefault(identity.role, []).append(identity.name)

    def step(self, t: float, ts: float):
        """Flows and auth events for the step starting at run time ``t``
        (seconds from start), stamped with absolute time ``ts``."""
        flows: List[FlowRecord] = []
        auth: List[AuthEvent] = []
        for identity in self.registry.all():
            flows.extend(self._benign_flows(identity, ts))
            auth.extend(self._benign_auth(identity, ts))
        for attack in self.plan.attacks:
            if attack.active(t):
                f, a = self._attack(attack, ts)
                flows.extend(f)
                auth.extend(a)
        return flows, auth

    # -- benign ------------------------------------------------------------

    def _count(self, rate: float) -> int:
        whole = int(rate)
        return whole + (1 if self._rng.random() < rate - whole else 0)

    def _flow(self, src, dst_name, port, ts, packets, label="benign", attack_id=None, claim=None):
        identity = claim or self.registry.get(src)
        return FlowRecord(ts=ts, src=src, src_ip=identity.ip, src_mac=identity.mac, dst=dst_name,
                          dst_port=port, packets=packets, bytes=packets * self._rng.randint(400, 1400),
                          label=label, attack_id=attack_id)

    def _benign_flows(self, identity, ts) -> List[FlowRecord]:
        out = []
        for dst_role, port, rate in ROLE_PROFILES.get(identity.role, []):
            for _ in range(self._count(rate)):
                dst = self._rng.choice(self._by_role[dst_role])
                out.append(self._flow(identity.name, dst, port, ts, self._rng.randint(5, 60)))
        if identity.role in MISTAKES and self._rng.random() < self.plan.mistake_rate:
            dst_role, port = self._rng.choice(MISTAKES[identity.role])
            dst = self._rng.choice(self._by_role[dst_role])
            out.append(self._flow(identity.name, dst, port, ts, self._rng.randint(1, 5), label=MISTAKE))
        return out

    def _benign_auth(self, identity, ts) -> List[AuthEvent]:
        if identity.kind != "host" or self._rng.random() > 0.05:
            return []
        target = self._rng.choice(self._by_role["web_server"])
        ok = self._rng.random() >= self.plan.login_failure_rate
        return [AuthEvent(ts, identity.name, target, identity.name, ok)]

    # -- attacks -----------------------------------------------------------

    def _attack(self, a: AttackSpec, ts: float):
        flows: List[FlowRecord] = []
        auth: List[AuthEvent] = []
        if a.kind == "port_scan":
            for port in self._rng.sample(range(1, 1025), 25):
                flows.append(self._flow(a.attacker, a.target, port, ts, 1, a.kind, a.attack_id))
        elif a.kind == "brute_force":
            for _ in range(8):
                auth.append(AuthEvent(ts, a.attacker, a.target, "root", False, a.kind, a.attack_id))
                flows.append(self._flow(a.attacker, a.target, 22, ts, 3, a.kind, a.attack_id))
        elif a.kind == "spoofing":
            victim = self.registry.get(a.victim)
            for _ in range(3):
                flows.append(self._flow(a.attacker, a.target, 22, ts, 10, a.kind, a.attack_id, claim=victim))
        elif a.kind == "lateral_movement":
            for port in (22, 5432, 3389):
                flows.append(self._flow(a.attacker, a.target, port, ts, self._rng.randint(3, 10),
                                        a.kind, a.attack_id))
        else:  # flood
            for _ in range(40):
                flows.append(self._flow(a.attacker, a.target, 80, ts, 2000, a.kind, a.attack_id))
        return flows, auth
