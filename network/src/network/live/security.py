"""Zero Trust on the live network (Module 5, live P5).

* :class:`LiveEnforcer` — drop-in for ``SimulatedEnforcer`` inside
  :class:`~network.security.engine.ZeroTrustEngine`. Whenever the compiled
  rule set changes (trust levels, quarantines, manual blocks) it is pushed
  to the Ryu app (``PUT /sh/zt``), which decides every new flow at its
  ingress switch with exactly these rules. ``admit`` (kept from the
  simulated enforcer) is only used to count decision/enforcement mismatches.
* :class:`LiveSecurityFeed` — per step, the engine's inputs from the real
  network: one ``FlowRecord`` per flow that sent packets since the last step
  (``GET /sh/flows``: which device is wired to the ingress port, the
  addresses the packets claimed, real packet/byte counts), and one
  ``AuthEvent`` per login attempt logged by the servers. Replies to allowed
  connections, ICMP (monitoring pings) and traffic to non-devices are not
  access requests and are left out.
* :class:`LiveTraffic` — starts the services, the normal users and attacks
  inside the Mininet hosts (``network.live.hostapps``).

No record carries ground-truth labels: every flow is ``benign`` as far as
the data says; attacks are known only to the operator who launched them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from network.security.enforcement import ALLOW, FlowRule, SimulatedEnforcer
from network.security.records import AuthEvent, FlowRecord

SERVICE_PORTS = {"http": 80, "https": 443, "postgres": 5432, "ssh": 22}
PROTOCOLS = {6: "tcp", 17: "udp"}
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def rule_to_json(rule: FlowRule) -> dict:
    match = dict(rule.match)
    if "src_mac" in match:
        match["src_mac"] = str(match["src_mac"]).lower()
    return {"table": rule.table, "priority": rule.priority, "match": match,
            "action": "allow" if rule.action == ALLOW else "drop", "reason": rule.reason}


class LiveEnforcer(SimulatedEnforcer):
    def __init__(self, client):
        super().__init__()
        self.client = client
        self.pushes = 0
        self.last_error: Optional[str] = None

    def apply(self, rules: List[FlowRule]) -> Dict[str, List[FlowRule]]:
        first = not self._rules
        diff = super().apply(rules)
        if first or diff["added"] or diff["removed"]:
            try:
                self.client.request("PUT", "/sh/zt", {"rules": [rule_to_json(r) for r in self._rules]})
                self.pushes += 1
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - retried on the next change/step
                self.last_error = str(exc)
                self._rules = []  # force a push next time
        return diff


def attach_live_enforcer(engine, client) -> LiveEnforcer:
    """Swap a ZeroTrustEngine's enforcer for the live one and push its rules now."""
    enforcer = LiveEnforcer(client)
    engine.enforcer = enforcer
    engine._apply_rules()
    return enforcer


class LiveSecurityFeed:
    def __init__(self, client, registry, auth_logs: Dict[str, str], active_s: float = 15.0):
        self.client = client
        self.registry = registry
        self.auth_logs = dict(auth_logs)   # server name -> log path
        self.active_s = active_s
        self._counts: Dict[int, int] = {}
        self._max_id = 0
        self._offsets: Dict[str, int] = {}
        self.last_error: Optional[str] = None
        self.skipped = 0
        self._baseline: Optional[int] = None  # flows up to this id predate the feed

    def _start(self) -> None:
        """Ignore flows the controller recorded before this feed existed
        (it keeps them for a while, e.g. from a previous run)."""
        try:
            body = self.client.request("GET", "/sh/flows?after=0&active=0")
            self._baseline = max((int(f["id"]) for f in body.get("flows", [])), default=0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return
        self._max_id = self._baseline
        for path in self.auth_logs.values():  # and logins already in the logs
            try:
                self._offsets[path] = os.path.getsize(path)
            except OSError:
                pass

    def step(self, ts: float) -> Tuple[List[FlowRecord], List[AuthEvent]]:
        return self._flows(ts), self._auth(ts)

    def _flows(self, ts: float) -> List[FlowRecord]:
        if self._baseline is None:
            self._start()
            if self._baseline is None:
                return []
        try:
            body = self.client.request("GET", f"/sh/flows?after={self._max_id}&active={self.active_s}")
            self.last_error = None
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []
        out = []
        for f in body.get("flows", []):
            fid = int(f["id"])
            if fid <= self._baseline:
                continue
            previous = self._counts.get(fid)
            delta = int(f["packets"]) - (previous or 0)
            self._counts[fid] = int(f["packets"])
            self._max_id = max(self._max_id, fid)
            if previous is not None and delta <= 0:
                continue
            if f.get("reply") or f.get("dst_port") is None or self.registry.get(f.get("dst")) is None:
                self.skipped += 1
                continue
            packets = max(1, delta)
            avg = int(f["bytes"]) / max(1, int(f["packets"]))
            out.append(FlowRecord(ts=ts, src=f["src"], src_ip=f["src_ip"], src_mac=f["src_mac"], dst=f["dst"],
                                  dst_port=int(f["dst_port"]), protocol=PROTOCOLS.get(int(f["proto"]), "ip"),
                                  packets=packets, bytes=int(packets * avg)))
        if len(self._counts) > 20000:  # forget flows the controller no longer reports
            for fid in sorted(self._counts)[:10000]:
                del self._counts[fid]
        return out

    def _auth(self, ts: float) -> List[AuthEvent]:
        out = []
        for server, path in sorted(self.auth_logs.items()):
            try:
                with open(path) as fh:
                    fh.seek(self._offsets.get(path, 0))
                    lines = fh.readlines()
                    self._offsets[path] = fh.tell()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                device = self.registry.by_ip(entry.get("src_ip", ""))
                out.append(AuthEvent(ts=ts, device=device.name if device else entry.get("src_ip", "?"),
                                     target=server, account=str(entry.get("account")),
                                     success=bool(entry.get("success"))))
        return out


class LiveTraffic:
    """Starts and stops the programs in the Mininet hosts."""

    def __init__(self, controller, registry, config_dir: str, topology_file: str = "enterprise.yaml",
                 log_dir: str = "/tmp"):
        self.controller = controller
        self.registry = registry
        self.config_dir = config_dir
        self.topology_file = topology_file
        self.log_dir = log_dir
        self.processes: Dict[str, subprocess.Popen] = {}
        self.attacks: List[dict] = []

    @property
    def auth_logs(self) -> Dict[str, str]:
        return {d.name: os.path.join(self.log_dir, f"sh-auth-{d.name}.log")
                for d in self.controller.config.host_devices if d.kind == "server"}

    def _popen(self, host: str, args: List[str]) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=PACKAGE_ROOT, SH_TOPOLOGY=self.topology_file, PYTHONUNBUFFERED="1")
        cmd = [sys.executable, "-m", "network.live.hostapps", *args]
        return self.controller._net.get(host).popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL)

    def start(self, clients: bool = True) -> None:
        for server in self.controller.config.host_devices:
            if server.kind != "server":
                continue
            ports = sorted({SERVICE_PORTS[s] for s in (server.services or []) if s in SERVICE_PORTS} | {22})
            log = self.auth_logs[server.name]
            open(log, "w").close()
            self.processes[f"serve:{server.name}"] = self._popen(
                server.name, ["serve", "--name", server.name, "--ports", ",".join(map(str, ports)), "--log", log])
        time.sleep(0.5)
        if clients:
            for identity in self.registry.all():
                self.processes[f"client:{identity.name}"] = self._popen(
                    identity.name, ["client", "--name", identity.name, "--config-dir", self.config_dir])

    def launch_attack(self, kind: str, attacker: str, duration_s: float = 60.0, target: Optional[str] = None,
                      victim: Optional[str] = None, pps: int = 4000) -> dict:
        web = self.registry.with_role("web_server")[0]
        db = self.registry.with_role("db_server")[0]
        if target is None:
            target = (db if kind == "spoofing" else web).name
        args = ["attack", "--kind", kind, "--target", self.registry.get(target).ip,
                "--duration", str(duration_s), "--pps", str(pps)]
        if kind == "spoofing":
            victim_id = self.registry.get(victim) if victim else self.registry.with_role("admin")[0]
            me = self.registry.get(attacker)
            gw = [int(x) for x in me.gateway.split(".")]
            args += ["--iface", self.controller._net.get(attacker).defaultIntf().name,
                     "--victim-ip", victim_id.ip, "--victim-mac", victim_id.mac,
                     "--dst-mac", "02:fe:" + ":".join(f"{x:02x}" for x in gw)]
            victim = victim_id.name
        proc = self._popen(attacker, args)
        record = {"kind": kind, "attacker": attacker, "target": target, "victim": victim,
                  "started": time.time(), "duration_s": duration_s, "pid": proc.pid}
        self.processes[f"attack:{len(self.attacks)}:{kind}"] = proc
        self.attacks.append(record)
        return record

    def stop(self) -> None:
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self.processes.values():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.processes.clear()
