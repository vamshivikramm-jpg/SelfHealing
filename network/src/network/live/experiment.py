"""Short experiment on the live lab (P7).

    python3 -m network.live.experiment --out /tmp/live-run --rounds 1

Runs the dashboard's :class:`LiveRuntime` (no web server) on the redundant
topology and plays a fixed script of incidents on the real network, with a
quiet gap after each:

* failures: physical cut and logical disconnect of sw-a<->sw-b, rtr-core down,
  150 ms latency after a 60 s warning ramp, 20 % packet loss;
* attacks (real programs in the hosts): port scan, brute force, spoofing,
  lateral movement, flood - each from a different host.

Measured from what the loop itself observed, every step:

| metric | definition |
|---|---|
| detection | first step the target's health is not HEALTHY after the fault starts |
| predicted lead | first FAILURE_PREDICTED for the link before the fault starts (warning ramps only) |
| first action / verified | first healing action on the target executed / verified |
| restored | first time from which every affected client/server pair is delivered >= 90 % (real pings) until the fault ends |
| lost | mean undelivered fraction of the affected pairs during the fault |
| attack detection / quarantine | first anomaly / quarantine of the attacker |
| false quarantines | devices quarantined that were not the attacker of a running attack |

Writes ``samples.jsonl`` (one line per step), ``incidents.json`` and
``report.md`` to ``--out``. Needs the live stack and NO running dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from typing import Dict, List, Optional

from network.decision.graph import path_links
from network.simulation.runtime import CONFIG_DIR

FAILURES = (
    {"kind": "link", "target": ["sw-a", "sw-b"], "physical": True, "label": "physical cut sw-a<->sw-b"},
    {"kind": "link", "target": ["sw-a", "sw-b"], "physical": False, "label": "link disconnected sw-a<->sw-b"},
    {"kind": "router", "target": ["rtr-core"], "physical": False, "label": "rtr-core down"},
    {"kind": "latency", "target": ["sw-a", "sw-b"], "magnitude": 150.0, "warning_s": 60.0,
     "label": "150 ms latency on sw-a<->sw-b after a 60 s warning ramp"},
    {"kind": "packet_loss", "target": ["sw-a", "sw-b"], "magnitude": 20.0, "label": "20 % loss on sw-a<->sw-b"},
)
ATTACKS = (
    {"kind": "port_scan", "attacker": "h5"},
    {"kind": "brute_force", "attacker": "h3"},
    {"kind": "spoofing", "attacker": "h4"},
    {"kind": "lateral_movement", "attacker": "h2"},
    {"kind": "flood", "attacker": "h1"},
)


def _first(samples, start, end, predicate) -> Optional[float]:
    for s in samples:
        if start <= s["ts"] <= end and predicate(s):
            return s["ts"]
    return None


class LiveExperiment:
    def __init__(self, out: str, config_dir: str = CONFIG_DIR, warmup_s: float = 90.0, fault_s: float = 60.0,
                 attack_s: float = 40.0, gap_s: float = 45.0, rounds: int = 1):
        from network.live.runtime import LiveRuntime

        self.out = out
        os.makedirs(out, exist_ok=True)
        self.fault_s, self.attack_s, self.gap_s, self.rounds = fault_s, attack_s, gap_s, rounds
        self.rt = LiveRuntime(config_dir, interval_s=2.0, warmup_s=warmup_s)
        self.samples: List[dict] = []
        self.incidents: List[dict] = []
        self._fh = open(os.path.join(out, "samples.jsonl"), "w")
        self._predicted: Dict[str, float] = {}
        self.rt.bus.subscribe(self._on_event)

    def _on_event(self, event) -> None:
        if event.type.value == "FAILURE_PREDICTED":
            target = event.data.get("target")
            self._predicted.setdefault(target, time.time())

    # -- clock ----------------------------------------------------------------------------------
    def run_for(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            t0 = time.time()
            self.rt.tick()
            self._sample()
            time.sleep(max(0.0, self.rt.interval_s - (time.time() - t0)))

    def _sample(self) -> None:
        rt = self.rt
        sec = rt.security
        health = {k: r.status.value for k, r in rt.collector.health().items()}
        anomalies = {}
        for e in sec.recent_events(60):
            if e["type"] == "SECURITY_ANOMALY":
                anomalies.setdefault(e["data"]["subject"], set()).add(e["data"]["kind"])
        delivered = {f"{a}>{b}": rt.service.latest((a, b)) for a, b in rt.service.routes}
        row = {"ts": time.time(), "health": health, "quarantined": sorted(sec.quarantined()),
               "anomalies": {k: sorted(v) for k, v in anomalies.items()}, "delivered": delivered,
               "predicted": sorted(k for k, v in self._predicted.items()),
               "rerouted": len(rt.rerouted())}
        self.samples.append(row)
        self._fh.write(json.dumps(row) + "\n")
        self._fh.flush()

    # -- script -----------------------------------------------------------------------------------
    def run(self) -> dict:
        rt = self.rt
        print("learning normal traffic ...", flush=True)
        while rt.learning:
            self.run_for(rt.interval_s)
        self.run_for(10)
        for r in range(self.rounds):
            for f in FAILURES:
                self._failure(f, r)
            for a in ATTACKS:
                self._attack(a, r)
        report = self.report()
        with open(os.path.join(self.out, "incidents.json"), "w") as fh:
            json.dump({"incidents": self.incidents, "summary": report, "settings": self.settings()}, fh, indent=2)
        with open(os.path.join(self.out, "report.md"), "w") as fh:
            fh.write(self.markdown(report))
        self._fh.close()
        rt.shutdown()
        return report

    def settings(self) -> dict:
        return {"topology": self.rt.topology_file, "interval_s": self.rt.interval_s, "fault_s": self.fault_s,
                "attack_s": self.attack_s, "gap_s": self.gap_s, "rounds": self.rounds,
                "predictor_threshold": getattr(self.rt.predictor, "threshold", None),
                "autonomy": self.rt.decisions.policy.autonomy}

    def _failure(self, f: dict, round_no: int) -> None:
        rt = self.rt
        print(f"failure: {f['label']}", flush=True)
        self._predicted.clear()
        item = rt.inject_failure(f["kind"], f["target"], self.fault_s, f.get("physical", False),
                                 f.get("magnitude"), f.get("warning_s", 0.0))
        self.run_for(f.get("warning_s", 0.0) + self.fault_s + self.gap_s)
        target = "-".join(sorted(f["target"])) if len(f["target"]) == 2 else f["target"][0]
        key = f"link:{target}" if len(f["target"]) == 2 else f"node:{target}"
        start, end = item["failure_start"], item["end"]
        affected = [(a, b) for (a, b), p in rt.service.baseline.items()
                    if (len(f["target"]) == 2 and target in path_links(p)) or
                    (len(f["target"]) == 1 and target in p[1:-1])]
        actions = [a for a in rt.healing.history() if a.get("target") == key and (a.get("started") or 0) >= start - 5]
        executed = min((a["started"] for a in actions if a.get("started")), default=None)
        verified = min((a["verified_at"] for a in actions if a.get("verified_at")), default=None)
        detect = _first(self.samples, start, end + self.gap_s, lambda s: s["health"].get(target, "HEALTHY") != "HEALTHY")
        restored = rt.service.restored_at(affected, start, end) if affected else None
        lost = rt.service.lost_fraction(affected, start, end) if affected else None
        predicted = self._predicted.get(target)
        self.incidents.append({
            "type": "failure", "round": round_no, "label": f["label"], "kind": f["kind"], "target": target,
            "physical": f.get("physical", False), "start": start, "end": end, "affected_pairs": len(affected),
            "detection_s": None if detect is None else round(detect - start, 1),
            "predicted_lead_s": None if predicted is None or predicted > start else round(start - predicted, 1),
            "first_action_s": None if executed is None else round(executed - start, 1),
            "verified_s": None if verified is None else round(verified - start, 1),
            "restored_s": None if restored is None else round(restored - start, 1),
            "lost_fraction": None if lost is None else round(lost, 3),
            "actions": [{"type": a["type"], "state": a["state"]} for a in actions],
        })

    def _attack(self, a: dict, round_no: int) -> None:
        rt = self.rt
        print(f"attack: {a['kind']} from {a['attacker']}", flush=True)
        item = rt.launch_attack(a["kind"], a["attacker"], self.attack_s)
        self.run_for(self.attack_s + self.gap_s)
        start, attacker = item["start"], a["attacker"]
        detect = _first(self.samples, start, start + self.attack_s + self.gap_s,
                        lambda s: attacker in s["anomalies"])
        quarantined = _first(self.samples, start, start + self.attack_s + self.gap_s,
                             lambda s: attacker in s["quarantined"])
        others = sorted({d for s in self.samples if start <= s["ts"] <= start + self.attack_s + self.gap_s
                         for d in s["quarantined"] if d != attacker})
        kinds = sorted({k for s in self.samples if start <= s["ts"] <= start + self.attack_s + self.gap_s
                        for k in s["anomalies"].get(attacker, [])})
        self.incidents.append({
            "type": "attack", "round": round_no, "kind": a["kind"], "attacker": attacker, "start": start,
            "detection_s": None if detect is None else round(detect - start, 1),
            "quarantine_s": None if quarantined is None else round(quarantined - start, 1),
            "anomaly_kinds": kinds, "other_devices_quarantined": others,
        })

    # -- results ----------------------------------------------------------------------------------
    def report(self) -> dict:
        fails = [i for i in self.incidents if i["type"] == "failure"]
        atts = [i for i in self.incidents if i["type"] == "attack"]

        def stat(values):
            v = [x for x in values if x is not None]
            return {"n": len(v), "median": round(statistics.median(v), 1) if v else None,
                    "max": max(v) if v else None}

        return {
            "failures": len(fails),
            "detected": sum(1 for i in fails if i["detection_s"] is not None),
            "detection_s": stat(i["detection_s"] for i in fails),
            "verified_actions": sum(1 for i in fails if i["verified_s"] is not None),
            "restored": sum(1 for i in fails if i["restored_s"] is not None),
            "restored_s": stat(i["restored_s"] for i in fails),
            "predicted": sum(1 for i in fails if i["predicted_lead_s"] is not None),
            "attacks": len(atts),
            "attacks_detected": sum(1 for i in atts if i["detection_s"] is not None),
            "attack_detection_s": stat(i["detection_s"] for i in atts),
            "attackers_quarantined": sum(1 for i in atts if i["quarantine_s"] is not None),
            "false_quarantines": sorted({d for i in atts for d in i["other_devices_quarantined"]}),
        }

    def markdown(self, r: dict) -> str:
        s = self.settings()
        lines = ["# Live lab experiment", "",
                 f"Real network: Mininet + Open vSwitch + self-healing Ryu app, topology `{s['topology']}`, "
                 f"autonomy {s['autonomy']}, step {s['interval_s']} s, faults {s['fault_s']} s, attacks "
                 f"{s['attack_s']} s, {s['gap_s']} s quiet gap after each, {s['rounds']} round(s). Predictor: "
                 f"z-score learnt from the live warm-up, threshold {s['predictor_threshold']}.", "",
                 "## Failures", "",
                 "| incident | detected (s) | predicted lead (s) | first action (s) | verified (s) | "
                 "service restored (s) | traffic lost | actions |",
                 "|---|---|---|---|---|---|---|---|"]

        def f(v, unit=""):
            return "—" if v is None else f"{v}{unit}"

        for i in self.incidents:
            if i["type"] == "failure":
                acts = ", ".join(f"{a['type']} {a['state']}" for a in i["actions"]) or "—"
                lost = "—" if i["lost_fraction"] is None else f"{100 * i['lost_fraction']:.0f} %"
                lines.append(f"| {i['label']} | {f(i['detection_s'])} | {f(i['predicted_lead_s'])} | "
                             f"{f(i['first_action_s'])} | {f(i['verified_s'])} | {f(i['restored_s'])} | {lost} | "
                             f"{acts} |")
        lines += ["", "## Attacks", "",
                  "| attack | attacker | detected (s) | quarantined (s) | anomalies | other devices quarantined |",
                  "|---|---|---|---|---|---|"]
        for i in self.incidents:
            if i["type"] == "attack":
                lines.append(f"| {i['kind']} | {i['attacker']} | {f(i['detection_s'])} | {f(i['quarantine_s'])} | "
                             f"{', '.join(i['anomaly_kinds']) or '—'} | "
                             f"{', '.join(i['other_devices_quarantined']) or 'none'} |")
        lines += ["", "## Summary", "", "```json", json.dumps(r, indent=2), "```", ""]
        return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="network.live.experiment")
    ap.add_argument("--out", required=True)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--warmup", type=float, default=90.0)
    args = ap.parse_args(argv)
    report = LiveExperiment(args.out, warmup_s=args.warmup, rounds=args.rounds).run()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
