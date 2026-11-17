"""Programs that run INSIDE Mininet hosts (live P5): services, users, attacks.

    python3 -m network.live.hostapps serve  --name srv-web-1 --ports 80,443,22 --log /tmp/selfhealing/sh-auth-srv-web-1.log
    python3 -m network.live.hostapps client --name h2 --config-dir network/config
    python3 -m network.live.hostapps attack --kind port_scan --target 10.0.1.20 --duration 60

Protocol of the services (one line per TCP connection):
    GET <path>               -> "200 OK" and some payload
    LOGIN <account> <pass>   -> "OK" or "FAIL"; every attempt is appended to
                                the auth log as JSON (ts, src_ip, port,
                                account, success)
The valid password of an account is ``<account>-secret`` (lab only).

Normal users follow the role profiles of the simulation
(``network.security.traffic.ROLE_PROFILES``): the same destinations, ports
and average rates, plus occasional logins and rare mistakes. Attacks are
the five simulated kinds done for real:

| kind | what it sends |
|---|---|
| port_scan | TCP connection attempts to ~5 random ports (1-1024) per second |
| brute_force | ~1.6 wrong-password logins per second on port 443 |
| spoofing | raw TCP SYNs to port 22 carrying another device's IP and MAC |
| lateral_movement | connection attempts to ports 22, 5432 and 3389 |
| flood | UDP datagrams to port 80 at ``--pps`` (default 4000/s, ~16 Mbit/s) |

Everything is standard library; attacks stop by themselves after
``--duration`` seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import socketserver
import struct
import sys
import threading
import time
import zlib

ATTACK_KINDS = ("port_scan", "brute_force", "spoofing", "lateral_movement", "flood")


# ---------------------------------------------------------------------------
# services (servers)
# ---------------------------------------------------------------------------

def _make_handler(log_path: str, port: int, lock: threading.Lock):
    class Handler(socketserver.StreamRequestHandler):
        timeout = 3

        def handle(self):
            try:
                line = self.rfile.readline(512).decode(errors="replace").strip()
            except OSError:
                return
            parts = line.split()
            if len(parts) == 3 and parts[0] == "LOGIN":
                account, password = parts[1], parts[2]
                ok = password == f"{account}-secret"
                entry = {"ts": time.time(), "src_ip": self.client_address[0], "port": port,
                         "account": account, "success": ok}
                with lock, open(log_path, "a") as fh:
                    fh.write(json.dumps(entry) + "\n")
                self.wfile.write(b"OK\n" if ok else b"FAIL\n")
            elif parts and parts[0] == "GET":
                self.wfile.write(b"200 OK\n" + b"x" * random.randint(200, 1200) + b"\n")
            else:
                self.wfile.write(b"400\n")

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(name: str, ports, log_path: str) -> None:
    lock = threading.Lock()
    open(log_path, "a").close()
    servers = []
    for port in ports:
        srv = _Server(("0.0.0.0", port), _make_handler(log_path, port, lock))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    print(f"{name}: serving {list(ports)}, auth log {log_path}", flush=True)
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------------------
# normal users
# ---------------------------------------------------------------------------

def _request(ip: str, port: int, line: str, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(line.encode() + b"\n")
            s.recv(2048)
        return True
    except OSError:
        return False


def client(name: str, config_dir: str, seed: int = 0, step_s: float = 5.0) -> None:
    from network.config import load_topology_config
    from network.security.identity import IdentityRegistry
    from network.security.traffic import MISTAKES, ROLE_PROFILES

    topo_file = os.environ.get("SH_TOPOLOGY", "enterprise.yaml")
    topology = load_topology_config(os.path.join(config_dir, topo_file))
    registry = IdentityRegistry.from_config(topology, os.path.join(config_dir, "identities.yaml"))
    me = registry.get(name)
    by_role = {}
    for identity in registry.all():
        by_role.setdefault(identity.role, []).append(identity)
    rng = random.Random(zlib.crc32(f"{name}:{seed}".encode()))
    print(f"{name}: normal {me.role} traffic", flush=True)
    tick = 1.0
    while True:
        start = time.time()
        for dst_role, port, rate in ROLE_PROFILES.get(me.role, []):
            if rng.random() < rate * tick / step_s:
                dst = rng.choice(by_role[dst_role])
                _request(dst.ip, port, "GET /")
        if me.kind == "host" and rng.random() < 0.05 * tick / step_s:
            web = rng.choice(by_role["web_server"])
            good = rng.random() >= 0.02
            _request(web.ip, 443, f"LOGIN {name} {name}-secret" if good else f"LOGIN {name} typo")
        if me.role in MISTAKES and rng.random() < 0.005 * tick / step_s:
            dst_role, port = rng.choice(MISTAKES[me.role])
            _request(rng.choice(by_role[dst_role]).ip, port, "GET /", timeout=0.5)
        time.sleep(max(0.0, tick - (time.time() - start)))


# ---------------------------------------------------------------------------
# attacks
# ---------------------------------------------------------------------------

def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def spoofed_syn(src_mac: str, dst_mac: str, src_ip: str, dst_ip: str, sport: int, dport: int) -> bytes:
    """Ethernet + IPv4 + TCP SYN frame with forged source addresses."""
    tcp = struct.pack("!HHIIBBHHH", sport, dport, random.getrandbits(32), 0, 5 << 4, 0x02, 64240, 0, 0)
    pseudo = socket.inet_aton(src_ip) + socket.inet_aton(dst_ip) + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", _checksum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), random.getrandbits(16), 0, 64, 6, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    ip = ip[:10] + struct.pack("!H", _checksum(ip)) + ip[12:]
    eth = bytes.fromhex(dst_mac.replace(":", "")) + bytes.fromhex(src_mac.replace(":", "")) + b"\x08\x00"
    return eth + ip + tcp


def attack(kind: str, target: str, duration: float, pps: int = 4000, iface: str = "",
           victim_ip: str = "", victim_mac: str = "", dst_mac: str = "") -> int:
    if kind not in ATTACK_KINDS:
        raise SystemExit(f"unknown attack {kind!r}; use one of {ATTACK_KINDS}")
    end = time.time() + duration
    sent = 0
    rng = random.Random()
    print(f"attack {kind} -> {target} for {duration:.0f} s", flush=True)
    if kind == "flood":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = b"F" * 512
        batch = max(1, pps // 50)
        while time.time() < end:
            t0 = time.time()
            for _ in range(batch):
                try:
                    s.sendto(payload, (target, 80))
                    sent += 1
                except OSError:
                    pass
            time.sleep(max(0.0, 0.02 - (time.time() - t0)))
    elif kind == "spoofing":
        if not (iface and victim_ip and victim_mac and dst_mac):
            raise SystemExit("spoofing needs --iface, --victim-ip, --victim-mac and --dst-mac")
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        s.bind((iface, 0))
        while time.time() < end:
            s.send(spoofed_syn(victim_mac, dst_mac, victim_ip, target, rng.randint(20000, 60000), 22))
            sent += 1
            time.sleep(1.0)
    else:
        while time.time() < end:
            if kind == "port_scan":
                port, line, timeout, pause = rng.randint(1, 1024), "GET /", 0.15, 0.05
            elif kind == "brute_force":
                port, line, timeout, pause = 443, f"LOGIN root guess{rng.randint(0, 9999)}", 1.0, 0.6
            else:  # lateral_movement
                port, line, timeout, pause = (22, 5432, 3389)[sent % 3], "GET /", 0.5, 0.6
            _request(target, port, line, timeout=timeout)
            sent += 1
            time.sleep(pause)
    print(f"attack {kind} done: {sent} sent", flush=True)
    return sent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="network.live.hostapps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--name", required=True)
    s.add_argument("--ports", required=True)
    s.add_argument("--log", required=True)
    c = sub.add_parser("client")
    c.add_argument("--name", required=True)
    c.add_argument("--config-dir", required=True)
    c.add_argument("--seed", type=int, default=0)
    a = sub.add_parser("attack")
    a.add_argument("--kind", required=True, choices=ATTACK_KINDS)
    a.add_argument("--target", required=True)
    a.add_argument("--duration", type=float, default=60.0)
    a.add_argument("--pps", type=int, default=4000)
    a.add_argument("--iface", default="")
    a.add_argument("--victim-ip", default="")
    a.add_argument("--victim-mac", default="")
    a.add_argument("--dst-mac", default="")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        serve(args.name, [int(p) for p in args.ports.split(",")], args.log)
    elif args.cmd == "client":
        client(args.name, args.config_dir, args.seed)
    else:
        attack(args.kind, args.target, args.duration, args.pps, args.iface, args.victim_ip, args.victim_mac,
               args.dst_mac)
    return 0


if __name__ == "__main__":
    sys.exit(main())
