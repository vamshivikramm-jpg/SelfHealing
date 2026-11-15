"""Decision policy (Module 6): loaded from ``config/decision_policy.yaml``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple

import yaml

AUTONOMY_LEVELS = ("L1", "L2", "L3")


@dataclass(frozen=True)
class RiskWeights:
    prediction: float = 0.30
    trust: float = 0.15
    threat: float = 0.15
    fault: float = 0.40

    def __post_init__(self):
        values = (self.prediction, self.trust, self.threat, self.fault)
        if any(v < 0 for v in values):
            raise ValueError("risk weights must be >= 0")
        if abs(sum(values) - 1.0) > 1e-6:
            raise ValueError(f"risk weights must sum to 1 (Eq. 4), got {sum(values):.3f}")


@dataclass(frozen=True)
class DecisionPolicy:
    autonomy: str = "L2"
    weights: RiskWeights = field(default_factory=RiskWeights)
    act_above: float = 0.15
    fault_levels: Dict[str, float] = field(default_factory=lambda: {"DOWN": 1.0, "DEGRADED": 0.5})
    proactive_above: float = 0.5
    cost_loss: float = 1.0
    cost_latency: float = 0.1
    cost_reliability: float = 10.0
    demand_mbps: float = 10.0
    utilization_target_pct: float = 70.0
    cooldown_s: float = 60.0
    max_quarantined: int = 2
    protected_kinds: Tuple[str, ...] = ("server", "switch", "router")
    always_approve: Tuple[str, ...] = ("ISOLATE",)

    def __post_init__(self):
        if self.autonomy not in AUTONOMY_LEVELS:
            raise ValueError(f"autonomy must be one of {AUTONOMY_LEVELS}")
        if not 0 <= self.act_above <= 1:
            raise ValueError("act_above must be within [0, 1]")

    @classmethod
    def from_file(cls, path) -> "DecisionPolicy":
        data = yaml.safe_load(Path(path).read_text()) or {}
        risk = data.get("risk") or {}
        cost = data.get("path_cost") or {}
        safety = data.get("safety") or {}
        return cls(
            autonomy=str(data.get("autonomy", "L2")),
            weights=RiskWeights(**(risk.get("weights") or {})),
            act_above=float(risk.get("act_above", 0.15)),
            fault_levels={str(k): float(v) for k, v in (risk.get("fault_levels") or
                                                         {"DOWN": 1.0, "DEGRADED": 0.5}).items()},
            proactive_above=float((data.get("prediction") or {}).get("proactive_above", 0.5)),
            cost_loss=float(cost.get("loss", 1.0)),
            cost_latency=float(cost.get("latency", 0.1)),
            cost_reliability=float(cost.get("reliability", 10.0)),
            demand_mbps=float(data.get("demand_mbps", 10.0)),
            utilization_target_pct=float(data.get("utilization_target_pct", 70.0)),
            cooldown_s=float(safety.get("cooldown_s", 60.0)),
            max_quarantined=int(safety.get("max_quarantined", 2)),
            protected_kinds=tuple(safety.get("protected_kinds", ("server", "switch", "router"))),
            always_approve=tuple(safety.get("always_approve", ("ISOLATE",))),
        )

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["weights"] = dict(self.weights.__dict__)
        data["protected_kinds"] = list(self.protected_kinds)
        data["always_approve"] = list(self.always_approve)
        return data
