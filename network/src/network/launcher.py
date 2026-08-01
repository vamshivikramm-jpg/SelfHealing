"""CLI launcher for the virtual enterprise network (Submodule 1.3).

Commands:
  up       build and start the network
  down     stop the network and clean up all Mininet artifacts
  reset    clean up, then rebuild and start
  status   report whether the network is currently up

All operations are performed through the NetworkController — the single
interface to the Mininet network.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _cleanup() -> None:
    from mininet.clean import cleanup

    cleanup()


def _ovs_bridges() -> list:
    out = subprocess.run(
        ["ovs-vsctl", "list-br"], capture_output=True, text=True, check=False
    ).stdout
    return [b.strip() for b in out.splitlines() if b.strip()]


def _ovs_ports(bridge: str) -> list:
    out = subprocess.run(
        ["ovs-vsctl", "list-ports", bridge], capture_output=True, text=True, check=False
    ).stdout
    return [p.strip() for p in out.splitlines() if p.strip()]


def _make_controller(args):
    from network.control import NetworkController

    return NetworkController(
        config_path=args.config,
        controller_ip=args.controller_ip,
        controller_port=args.controller_port,
    )


def cmd_up(args) -> int:
    controller = _make_controller(args)
    snapshot = controller.topology_snapshot()
    devices = snapshot["devices"]
    counts = {}
    for dev in devices:
        counts[dev["kind"]] = counts.get(dev["kind"], 0) + 1
    print(
        f"[enterprise-net] building topology '{snapshot['name']}': "
        f"{counts.get('router', 0)} router(s), {counts.get('switch', 0)} switch(es), "
        f"{counts.get('host', 0)} host(s), {counts.get('server', 0)} server(s)"
    )
    controller.start_network()
    print(
        f"[enterprise-net] network up: {len(snapshot['devices'])} devices, "
        f"{len(snapshot['links'])} links "
        f"(controller {args.controller_ip}:{args.controller_port})"
    )
    return 0


def cmd_down(args) -> int:
    from network.control import NetworkController, NetworkNotRunningError

    controller = _make_controller(args)
    try:
        controller.stop_network()
    except NetworkNotRunningError:
        print("[enterprise-net] network already stopped")
    _cleanup()
    print("[enterprise-net] network down; all Mininet artifacts cleaned")
    return 0


def cmd_reset(args) -> int:
    controller = _make_controller(args)
    controller.reset_network()
    print("[enterprise-net] network reset and started")
    return 0


def cmd_status(args) -> int:
    controller = _make_controller(args)
    status = controller.network_status()
    if status["status"] == "stopped":
        print("[enterprise-net] network is DOWN")
        return 0
    print(
        f"[enterprise-net] network is UP: {len(status['nodes'])} device(s), "
        f"{len(status['links'])} link(s)"
    )
    for bridge in sorted(_ovs_bridges()):
        ports = _ovs_ports(bridge)
        detail = ", ".join(ports) if ports else "(no ports)"
        print(f"  {bridge}: {detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enterprise-net",
        description="Launcher for the virtual enterprise network",
    )
    parser.add_argument(
        "--config",
        default="config/enterprise.yaml",
        help="path to the topology YAML config (default: %(default)s)",
    )
    parser.add_argument(
        "--controller-ip",
        default=os.environ.get("RYU_CONTROLLER_IP", "ryu-controller"),
        help="Ryu controller address (default: %(default)s)",
    )
    parser.add_argument(
        "--controller-port",
        type=int,
        default=int(os.environ.get("RYU_CONTROLLER_PORT", "6653")),
        help="Ryu OpenFlow port (default: %(default)s)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="build and start the network")
    sub.add_parser("down", help="stop and clean the network")
    sub.add_parser("reset", help="clean, rebuild and start the network")
    sub.add_parser("status", help="report network state")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "up":
        return cmd_up(args)
    if args.command == "down":
        return cmd_down(args)
    if args.command == "reset":
        return cmd_reset(args)
    if args.command == "status":
        return cmd_status(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
