#!/usr/bin/env bash
set -euo pipefail

echo "[mininet-net] starting Open vSwitch"

mkdir -p /var/run/openvswitch

if [ ! -f /etc/openvswitch/conf.db ]; then
    ovsdb-tool create /etc/openvswitch/conf.db /usr/share/openvswitch/vswitch.ovsschema
fi

# Kernel datapath: module must be loaded on the host. Not fatal - OVS can
# still run (userspace fallback), but kernel bridges require it.
modprobe openvswitch 2>/dev/null || \
    echo "[mininet-net] WARNING: openvswitch kernel module unavailable (host must run 'modprobe openvswitch')"

ovsdb-server /etc/openvswitch/conf.db \
    --remote=punix:/var/run/openvswitch/db.sock \
    --pidfile --detach

ovs-vswitchd --pidfile --detach || \
    ovs-vswitchd --pidfile --detach --no-mlockall

ovs-vsctl --no-wait init
sleep 2

echo "[mininet-net] OVS status:"
ovs-vsctl show

echo "[mininet-net] ready; keeping container alive"
tail -f /dev/null
