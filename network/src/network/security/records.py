"""Security observations (Submodule 5.2).

``FlowRecord``: one connection attempt as the network sees it. ``src`` is
the device physically attached to the ingress switch port (known to the
network); ``src_ip`` / ``src_mac`` are what the packets *claim*. A spoofed
flow claims another device's addresses.

``AuthEvent``: one login attempt from a device to a service.

``label`` / ``attack_id`` are ground truth for evaluation only; no
detector or policy decision may read them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BENIGN = "benign"
MISTAKE = "benign_mistake"  # a legitimate user asking for something the policy forbids


@dataclass(frozen=True)
class FlowRecord:
    ts: float
    src: str
    src_ip: str
    src_mac: str
    dst: str
    dst_port: int
    protocol: str = "tcp"
    packets: int = 1
    bytes: int = 64
    label: str = BENIGN
    attack_id: Optional[str] = None

    @property
    def is_attack(self) -> bool:
        return self.label not in (BENIGN, MISTAKE)


@dataclass(frozen=True)
class AuthEvent:
    ts: float
    device: str
    target: str
    account: str
    success: bool
    label: str = BENIGN
    attack_id: Optional[str] = None

    @property
    def is_attack(self) -> bool:
        return self.label not in (BENIGN, MISTAKE)


@dataclass(frozen=True)
class Anomaly:
    """A detector's warning about one subject in one step (``a_j`` in Eq. 2).

    ``magnitude`` in (0, 1] scales the anomaly type's severity weight.
    """

    ts: float
    subject: str
    kind: str
    magnitude: float
    detail: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)
