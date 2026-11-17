PYTHON ?= python3
NET_EXEC = docker compose exec -T mininet-net

.PHONY: help up down logs config test unit smoke net-up net-down net-reset net-status net-test

help:
	@echo "Targets:"
	@echo "  up         build and start the stack (mininet-net, ryu-controller)"
	@echo "  down       stop and remove containers/networks"
	@echo "  logs       follow container logs"
	@echo "  config     validate docker-compose.yml"
	@echo "  unit       run bootstrap unit tests (host python3 + pytest)"
	@echo "  smoke      run full container smoke test (requires Docker on Linux)"
	@echo "  net-up     build/start the enterprise network inside mininet-net"
	@echo "  net-down   stop and clean the enterprise network"
	@echo "  net-reset  clean, rebuild and start the enterprise network"
	@echo "  net-status show current network state (bridges/ports)"
	@echo "  net-test   run the live integration tests + Module 1 unit tests in-container"

up:
	docker compose up -d --build

down:
	docker compose down -v --remove-orphans

logs:
	docker compose logs -f

config:
	docker compose config -q

unit:
	$(PYTHON) -m pytest tests/unit/test_bootstrap.py -v

smoke:
	bash tests/smoke/test_bootstrap.sh

net-up:
	docker compose up -d --build
	$(NET_EXEC) python3 -m network.launcher up

net-down:
	$(NET_EXEC) python3 -m network.launcher down || true

net-reset:
	docker compose up -d --build
	$(NET_EXEC) python3 -m network.launcher reset

net-status:
	$(NET_EXEC) python3 -m network.launcher status || echo "stack not running"

# Live tests: the real-network integration tests plus the Module 1 unit tests.
# (Modules 2-9 need numpy/scikit-learn/FastAPI and run on the host or in CI.)
NET_UNIT = tests/unit/test_topology.py tests/unit/test_network_control.py tests/unit/test_event_bus.py \
	tests/unit/test_failure.py tests/unit/test_failure_manager.py tests/unit/test_failure_scenarios.py \
	tests/unit/test_network_impairments.py tests/unit/test_mininet_control_paths.py tests/unit/test_ryu_logic.py tests/unit/test_live_backends.py tests/unit/test_live_zero_trust.py

net-test:
	docker compose up -d
	$(NET_EXEC) bash -c "cd /workspace && PYTHONPATH=/workspace/network/src python3 -m pytest $(NET_UNIT) tests/integration -v -p no:cacheprovider"
