"""Device identities (Submodule 5.1).

Every host and server is a known identity: role, owner, the IP and MAC it
must present, its base trust and its posture (managed, patched). The
registry is built from the topology (names, IPs, subnets, gateways) plus
``config/identities.yaml``; the two must cover exactly the same devices.

Switches and routers are infrastructure, not access subjects, and are not
part of the registry.

Standard library + PyYAML; no Mininet imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

SUBJECT_KINDS = ("host", "server")


class IdentityError(ValueError):
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    name: str
    kind: str
    role: str
    owner: str
    ip: str
    mac: str
    subnet: str
    gateway: str
    base_trust: float
    managed: bool = True
    patched: bool = True

    def effective_base_trust(self, posture_penalty: float = 0.1) -> float:
        """Base trust minus ``posture_penalty`` per missing posture item."""
        penalty = posture_penalty * ((not self.managed) + (not self.patched))
        return max(0.0, min(1.0, self.base_trust - penalty))

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _strip_prefix(ip: Optional[str]) -> str:
    return (ip or "").split("/")[0]


class IdentityRegistry:
    def __init__(self, identities: Iterable[DeviceIdentity]):
        self._by_name: Dict[str, DeviceIdentity] = {}
        self._by_ip: Dict[str, DeviceIdentity] = {}
        self._by_mac: Dict[str, DeviceIdentity] = {}
        for identity in identities:
            for index, key in ((self._by_name, identity.name), (self._by_ip, identity.ip),
                               (self._by_mac, identity.mac.lower())):
                if key in index:
                    raise IdentityError(f"duplicate identity attribute {key!r}")
                index[key] = identity

    @classmethod
    def from_config(cls, topology, identities_path) -> "IdentityRegistry":
        """Combine the topology (duck-typed TopologyConfig) with the identity YAML."""
        path = Path(identities_path)
        data = yaml.safe_load(path.read_text()) or {}
        declared = data.get("devices") or {}
        subjects = [d for d in topology.devices if d.kind in SUBJECT_KINDS]
        names = {d.name for d in subjects}
        missing = names - set(declared)
        extra = set(declared) - names
        if missing or extra:
            raise IdentityError(f"identity file and topology differ: missing={sorted(missing)} "
                                f"unknown={sorted(extra)}")
        out: List[DeviceIdentity] = []
        for device in subjects:
            entry = declared[device.name]
            for key in ("role", "owner", "mac", "base_trust"):
                if key not in entry:
                    raise IdentityError(f"{device.name}: missing {key!r}")
            base = float(entry["base_trust"])
            if not 0.0 <= base <= 1.0:
                raise IdentityError(f"{device.name}: base_trust must be within [0, 1]")
            out.append(DeviceIdentity(
                name=device.name, kind=device.kind, role=str(entry["role"]), owner=str(entry["owner"]),
                ip=_strip_prefix(device.ip), mac=str(entry["mac"]).lower(), subnet=device.subnet or "",
                gateway=device.gateway or "", base_trust=base,
                managed=bool(entry.get("managed", True)), patched=bool(entry.get("patched", True)),
            ))
        return cls(out)

    def get(self, name: str) -> Optional[DeviceIdentity]:
        return self._by_name.get(name)

    def by_ip(self, ip: str) -> Optional[DeviceIdentity]:
        return self._by_ip.get(ip)

    def by_mac(self, mac: str) -> Optional[DeviceIdentity]:
        return self._by_mac.get(mac.lower())

    def names(self) -> List[str]:
        return sorted(self._by_name)

    def all(self) -> List[DeviceIdentity]:
        return [self._by_name[n] for n in self.names()]

    def with_role(self, role: str) -> List[DeviceIdentity]:
        return [i for i in self.all() if i.role == role]

    def verify(self, device: str, claimed_ip: str, claimed_mac: str) -> Optional[str]:
        """None if the claims match ``device``'s identity, else the reason."""
        identity = self.get(device)
        if identity is None:
            return f"unknown device {device!r}"
        problems = []
        if claimed_ip != identity.ip:
            problems.append(f"IP {claimed_ip} is not {device}'s ({identity.ip})")
        if claimed_mac.lower() != identity.mac:
            problems.append(f"MAC {claimed_mac} is not {device}'s ({identity.mac})")
        return "; ".join(problems) or None
