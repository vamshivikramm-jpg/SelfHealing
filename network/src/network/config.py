"""Topology configuration: loading and validation (Submodule 1.2).

Configurations are YAML files describing segments, subnets, devices and
links. All validation happens here so an invalid config can never reach
the Mininet layer. Errors are raised as :class:`ConfigError` with a
specific message.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEVICE_KINDS = ("router", "switch", "host", "server")
SWITCH_KINDS = ("router", "switch")
HOST_KINDS = ("host", "server")

# YAML device section per kind ("switch" -> "switches", not "switchs")
DEVICE_SECTIONS = {kind: f"{kind}s" for kind in DEVICE_KINDS}
DEVICE_SECTIONS["switch"] = "switches"

_DPID_RE = re.compile(r"^[0-9a-f]{1,16}$")


class ConfigError(Exception):
    """Raised when a topology configuration file is invalid."""


@dataclass
class InterfaceConfig:
    port: int
    name: str
    subnet: str
    ip: Optional[str] = None


@dataclass
class DeviceConfig:
    name: str
    kind: str
    datapath_id: Optional[str] = None
    subnet: Optional[str] = None
    ip: Optional[str] = None
    gateway: Optional[str] = None
    services: list = field(default_factory=list)
    interfaces: list = field(default_factory=list)
    role: str = ""


@dataclass
class LinkConfig:
    src: str
    dst: str
    src_port: Optional[int] = None
    dst_port: Optional[int] = None


@dataclass
class SubnetConfig:
    id: str
    cidr: str
    gateway: str
    segment: str = ""


@dataclass
class SegmentConfig:
    id: str
    label: str = ""
    subnets: list = field(default_factory=list)


@dataclass
class TopologyConfig:
    name: str
    version: str = ""
    segments: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    links: list = field(default_factory=list)

    def device(self, name: str) -> Optional[DeviceConfig]:
        return next((d for d in self.devices if d.name == name), None)

    def subnet(self, subnet_id: str) -> Optional[SubnetConfig]:
        for segment in self.segments:
            for subnet in segment.subnets:
                if subnet.id == subnet_id:
                    return subnet
        return None

    @property
    def switch_devices(self):
        return [d for d in self.devices if d.kind in SWITCH_KINDS]

    @property
    def host_devices(self):
        return [d for d in self.devices if d.kind in HOST_KINDS]


def load_topology_config(path) -> TopologyConfig:
    """Load and validate a YAML topology configuration."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"topology config not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    config = _parse(data, path)
    _validate(config)
    return config


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _parse(data: dict, path: Path) -> TopologyConfig:
    if not isinstance(data, dict) or not data.get("name"):
        raise ConfigError(f"config must define a 'name' (file: {path})")

    config = TopologyConfig(
        name=str(data["name"]),
        version=str(data.get("version", "")),
    )

    for seg in data.get("segments", []) or []:
        subnets = []
        for sub in seg.get("subnets", []) or []:
            subnet_id = sub.get("id")
            if not subnet_id:
                raise ConfigError("subnet entries must define an 'id'")
            subnets.append(
                SubnetConfig(
                    id=str(subnet_id),
                    cidr=str(sub.get("cidr", "")),
                    gateway=str(sub.get("gateway", "")),
                    segment=str(seg.get("id", "")),
                )
            )
        config.segments.append(
            SegmentConfig(id=str(seg.get("id", "")), label=str(seg.get("label", "")), subnets=subnets)
        )

    for kind in DEVICE_KINDS:
        for raw in data.get("devices", {}).get(DEVICE_SECTIONS[kind], []) or []:
            config.devices.append(
                DeviceConfig(
                    name=str(raw.get("name", "")),
                    kind=kind,
                    datapath_id=_normalize_dpid(raw.get("datapath_id")),
                    subnet=raw.get("subnet"),
                    ip=raw.get("ip"),
                    gateway=raw.get("gateway"),
                    services=[str(s) for s in (raw.get("services") or [])],
                    interfaces=[
                        InterfaceConfig(
                            port=int(iface.get("port", 0)),
                            name=str(iface.get("name", "")),
                            subnet=str(iface.get("subnet", "")),
                            ip=iface.get("ip"),
                        )
                        for iface in (raw.get("interfaces") or [])
                    ],
                    role=str(raw.get("role", "")),
                )
            )

    for raw in data.get("links", []) or []:
        config.links.append(
            LinkConfig(
                src=str(raw.get("src", "")),
                dst=str(raw.get("dst", "")),
                src_port=raw.get("src_port"),
                dst_port=raw.get("dst_port"),
            )
        )

    return config


def _normalize_dpid(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, int):
        value = format(value, "x")
    dpid = str(value).replace(":", "").lower()
    if not _DPID_RE.match(dpid):
        raise ConfigError(f"invalid datapath id: {value!r} (expected hex, max 16 digits)")
    return dpid


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate(config: TopologyConfig) -> None:
    seen_names = set()
    seen_dpids = set()
    seen_subnets = set()

    for segment in config.segments:
        for subnet in segment.subnets:
            _validate_subnet(subnet)
            if subnet.id in seen_subnets:
                raise ConfigError(f"duplicate subnet id: {subnet.id!r}")
            seen_subnets.add(subnet.id)

    for dev in config.devices:
        if not dev.name:
            raise ConfigError("device entries must define a 'name'")
        if dev.name in seen_names:
            raise ConfigError(f"duplicate device name: {dev.name!r}")
        seen_names.add(dev.name)

        if dev.kind in SWITCH_KINDS:
            if dev.datapath_id is None:
                raise ConfigError(f"switch/router {dev.name!r} must define a 'datapath_id'")
            if dev.datapath_id in seen_dpids:
                raise ConfigError(f"duplicate datapath_id {dev.datapath_id!r} (device {dev.name!r})")
            seen_dpids.add(dev.datapath_id)
            used_ports = set()
            for iface in dev.interfaces:
                if not iface.name:
                    raise ConfigError(f"interface on {dev.name!r} must define a 'name'")
                if iface.port in used_ports:
                    raise ConfigError(f"duplicate port {iface.port} on device {dev.name!r}")
                used_ports.add(iface.port)
                subnet = config.subnet(iface.subnet)
                if subnet is None:
                    raise ConfigError(
                        f"interface {iface.name!r} on {dev.name!r} references unknown subnet {iface.subnet!r}"
                    )
                if iface.ip:
                    _require_ip_in_subnet(dev.name, iface.ip, subnet.cidr)

        if dev.kind in HOST_KINDS:
            subnet = config.subnet(dev.subnet) if dev.subnet else None
            if dev.subnet and subnet is None:
                raise ConfigError(f"host {dev.name!r} references unknown subnet {dev.subnet!r}")
            if dev.ip:
                if subnet is None:
                    raise ConfigError(f"host {dev.name!r} has an ip but no subnet")
                _require_ip_in_subnet(dev.name, dev.ip, subnet.cidr)
            if dev.gateway:
                if subnet is None:
                    raise ConfigError(f"host {dev.name!r} has a gateway but no subnet")
                _require_ip_in_subnet(dev.name, dev.gateway, subnet.cidr)

    if not config.host_devices:
        raise ConfigError("topology must contain at least one host or server")

    _validate_links(config)
    _validate_connectivity(config)


def _validate_subnet(subnet: SubnetConfig) -> None:
    if not subnet.id or not subnet.cidr or not subnet.gateway:
        raise ConfigError("subnet entries must define 'id', 'cidr' and 'gateway'")
    try:
        network = ipaddress.ip_network(subnet.cidr)
    except ValueError as exc:
        raise ConfigError(f"invalid subnet cidr {subnet.cidr!r}: {exc}") from exc
    if network.prefixlen == 32:
        raise ConfigError(f"subnet {subnet.id!r} cidr {subnet.cidr!r} must not be a /32")
    _require_ip_in_subnet(f"subnet {subnet.id!r}", subnet.gateway, subnet.cidr)


def _require_ip_in_subnet(owner: str, ip: str, cidr: str) -> None:
    try:
        network = ipaddress.ip_network(cidr)
        address = ipaddress.ip_address(str(ip).split("/")[0])
    except ValueError as exc:
        raise ConfigError(f"{owner}: invalid ip {ip!r}: {exc}") from exc
    if address not in network:
        raise ConfigError(f"{owner}: ip {ip!r} is outside subnet {cidr!r}")


def _validate_links(config: TopologyConfig) -> None:
    seen = set()
    host_link_count = {}

    for i, link in enumerate(config.links):
        for end in (link.src, link.dst):
            if config.device(end) is None:
                raise ConfigError(f"link #{i} references unknown device {end!r}")
        if link.src == link.dst:
            raise ConfigError(f"link #{i} connects {link.src!r} to itself")
        pair = tuple(sorted((link.src, link.dst)))
        if pair in seen:
            raise ConfigError(f"duplicate link between {pair[0]!r} and {pair[1]!r}")
        seen.add(pair)

        for end in (link.src, link.dst):
            dev = config.device(end)
            if dev is None:
                continue
            if dev.kind in HOST_KINDS:
                host_link_count[end] = host_link_count.get(end, 0) + 1

    for name, count in host_link_count.items():
        if count > 1:
            raise ConfigError(f"host {name!r} must have exactly one link (has {count})")

    for dev in config.host_devices:
        if dev.name not in host_link_count:
            raise ConfigError(f"host {dev.name!r} is not connected to any link")


def _validate_connectivity(config: TopologyConfig) -> None:
    """Every subnet must contain at least one host and be reachable from a switch."""

    switch_devices = {d.name for d in config.switch_devices}

    for subnet in (s for seg in config.segments for s in seg.subnets):
        hosts = [d for d in config.host_devices if d.subnet == subnet.id]
        if not hosts:
            raise ConfigError(f"subnet {subnet.id!r} has no hosts attached")
        attached_to_switch = any(
            (link.src in switch_devices and config.device(link.dst) in hosts)
            or (link.dst in switch_devices and config.device(link.src) in hosts)
            for link in config.links
        )
        if not attached_to_switch:
            raise ConfigError(f"subnet {subnet.id!r} hosts are not attached to any switch")
