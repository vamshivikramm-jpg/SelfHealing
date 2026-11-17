"""Traffic delivery on the live network (Module 7.2, live).

Same interface as :class:`network.healing.service.ServiceModel`, measured
instead of modelled:

* each step, every client/server pair sends a few real pings (in parallel,
  inside the Mininet hosts); the next step reads how many came back, so the
  delivered fraction describes the last interval;
* the installed routes are read back from the Ryu app (``/sh/routes``), so
  ``routes`` is what the switches really do, including pinned paths.
"""

from __future__ import annotations

import re
import subprocess
from typing import Dict, Optional, Tuple

from network.healing.service import ServiceModel

Pair = Tuple[str, str]
_RECEIVED = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")


def parse_ping(output: str) -> Optional[float]:
    """Fraction of pings answered, from ``ping`` output (None if unparseable)."""
    m = _RECEIVED.search(output or "")
    if not m or int(m.group(1)) == 0:
        return None
    return int(m.group(2)) / int(m.group(1))


class LiveServiceModel(ServiceModel):
    def __init__(self, graph, controller, client, store=None, pings: int = 3, interval_s: float = 0.2):
        super().__init__(graph, lambda: None, store)
        self.controller = controller
        self.client = client
        self.pings = pings
        self.interval_s = interval_s
        self._running: Dict[Pair, subprocess.Popen] = {}
        self._measured: Dict[Pair, float] = {}
        self.ip = {d.name: d.ip.split("/")[0] for d in controller.config.host_devices}
        self.switch_of = {}
        for link in controller.config.links:
            for host, other in ((link.src, link.dst), (link.dst, link.src)):
                if host in self.ip and other not in self.ip:
                    self.switch_of[host] = other

    # -- measurement ------------------------------------------------------------------
    def _launch(self) -> None:
        net = self.controller._net
        if net is None:
            return
        for src, dst in self.routes:
            cmd = ["ping", "-n", "-q", "-c", str(self.pings), "-i", str(self.interval_s), "-W", "1", self.ip[dst]]
            try:
                self._running[(src, dst)] = net.get(src).popen(cmd, stdout=subprocess.PIPE,
                                                                stderr=subprocess.STDOUT)
            except Exception:  # noqa: BLE001 - host gone: counted as not delivered
                self._running.pop((src, dst), None)
                self._measured[(src, dst)] = 0.0

    def _collect(self) -> Dict[Pair, float]:
        out = {}
        for pair, proc in list(self._running.items()):
            try:
                stdout, _ = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate()
            text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else (stdout or "")
            value = parse_ping(text)
            out[pair] = 0.0 if value is None else value
        self._running.clear()
        out.update({p: v for p, v in self._measured.items() if p not in out})
        self._measured.clear()
        return out

    def refresh_routes(self) -> None:
        """Replace ``routes`` with the paths the switches actually use."""
        try:
            table = self.client.routes()
        except Exception:  # noqa: BLE001 - keep the last known routes
            return
        for src, dst in list(self.routes):
            info = table.get(self.ip[dst]) or {}
            path = (info.get("paths") or {}).get(self.switch_of.get(src))
            if path:
                self.routes[(src, dst)] = (src, *path, dst)

    def delivered(self, pair: Pair) -> float:
        value = self.latest(pair)
        return 0.0 if value is None else value

    def step(self, ts: float) -> Dict[Pair, float]:
        results = self._collect()
        self.refresh_routes()
        self._launch()
        for pair, value in results.items():
            self.history.setdefault(pair, []).append((ts, value))
        return results
