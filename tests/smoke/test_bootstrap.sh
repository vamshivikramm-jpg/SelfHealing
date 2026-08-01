#!/usr/bin/env bash
# Submodule 1.1 smoke test.
# Verifies: compose builds/starts, Mininet installed, OVS running,
# Ryu installed and reachable on TCP 6653 from the data plane.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

compose() { docker compose "$@"; }

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_healthy() {
    local name="$1" tries=0
    until [ "$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null)" = "healthy" ]; do
        tries=$((tries + 1))
        if [ "$tries" -ge 30 ]; then
            echo "FAIL: $name not healthy after 30s" >&2
            docker compose logs "$name"
            exit 1
        fi
        sleep 2
    done
    echo "OK: $name healthy"
}

echo "==> validating compose file"
compose config -q

echo "==> building and starting stack"
compose up -d --build

wait_healthy mininet-net
wait_healthy ryu-controller

echo "==> mininet installed"
compose exec -T mininet-net mn --version

echo "==> open vswitch running"
compose exec -T mininet-net ovs-vsctl show

echo "==> ryu importable"
compose exec -T ryu-controller python -c "import ryu; print('ryu', ryu.__version__)"

echo "==> ryu controller reachable on 6653 from data plane"
compose exec -T mininet-net python -c "
import socket
s = socket.socket()
s.settimeout(3)
s.connect(('ryu-controller', 6653))
s.close()
print('OK: OpenFlow port 6653 accepting connections')
"

echo ""
echo "SMOKE TEST PASSED"
