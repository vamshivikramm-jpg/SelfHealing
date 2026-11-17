# Results on the live lab (real emulated network)

The main evaluation ([`results.md`](results.md)) runs on the simulated
network: 10 seeds per condition, statistics. This page is the **live
counterpart**: one scripted run of every failure kind and every attack on
the real emulated network — Mininet hosts and Open vSwitch switches on a
Linux VM, controlled by the self-healing Ryu app — with measured telemetry,
real attack programs and rules enforced by the switches. It is **one round**:
single measurements, no confidence intervals.

Raw data: [`results/live-run/`](results/live-run/) (`report.md`,
`incidents.json`, per-step `samples.jsonl.gz`). Reproduce on the VM (stop
the dashboard first):

```bash
docker compose exec mininet-net bash -c "cd /workspace && PYTHONPATH=network/src \
  python3 -m network.live.experiment --out /tmp/selfhealing/live-run"
```

## Setup

Topology `enterprise-redundant.yaml` (rtr-core, sw-a, sw-b with a direct
sw-a↔sw-b link, 5 hosts, 2 servers), autonomy L2, one step every 2 s, every
device running its normal role traffic. The predictor was the z-score model
learnt from 90 s of live warm-up (alarm threshold 0.80). Each failure lasts
60 s and each attack 40 s, with 45 s of quiet after each. Every metric is
computed from what the loop itself observed step by step (health, delivery
by real pings, healing actions, Zero Trust state).

## Failures

| incident | detected (s) | predicted lead (s) | first action (s) | verified (s) | service restored (s) | traffic lost | actions |
|---|---|---|---|---|---|---|---|
| physical cut sw-a↔sw-b | 2.1 | — | 2.0 | 6.0 | 2.0 | 0 % | REROUTE verified → failed back after repair; RESTORE escalated |
| link disconnected sw-a↔sw-b | 2.1 | — | 2.0 | 6.0 | 2.0 | 0 % | RESTORE + REROUTE verified → failed back |
| rtr-core down | 2.1 | — | 2.0 | 6.0 | n/a¹ | n/a¹ | RESTORE verified |
| 150 ms latency on sw-a↔sw-b after a 60 s warning ramp | 0.0² | **46** | 34.0 | 38.0 | 0.0 | 0 % | REROUTE verified → failed back |
| 20 % loss on sw-a↔sw-b | 4.0 | — | 20.0 | 26.0 | 24.0 | 10 % | REROUTE verified → failed back |

Times are seconds after the fault started. ¹ No client/server path used
rtr-core (the direct sw-a↔sw-b link carries them), so no traffic was
affected. ² The link was already DEGRADED by the warning ramp when the fault
proper started.

## Attacks (real programs in the hosts)

| attack | attacker | detected (s) | quarantined (s) | anomalies | other devices quarantined |
|---|---|---|---|---|---|
| port scan | h5 | *invalid³* | *invalid³* | — | — |
| brute force (wrong-password logins) | h3 | 4.0 | 6.0 | auth_failures | none |
| identity spoofing (forged IP + MAC) | h4 | 0.0 | 2.0 | fingerprint_mismatch, policy_violation | none |
| lateral movement | h2 | 2.0 | 24.0 | policy_violation | none |
| flood (4000 packets/s) | h1 | 2.0 | not quarantined⁴ | volume_anomaly | none |

³ The VM was suspended (host went to sleep) for 25 minutes in the middle of
the port scan, so its timings are meaningless and it is excluded; the
analysis marks any incident with unobserved time inside it as invalid. A port
scan on the live network is covered by `tests/integration/test_live_runtime.py`
(h5 quarantined, verified by ping) and `test_zero_trust_live.py` (detected).
⁴ h1 is the admin workstation (base trust 0.9); one volume anomaly (weight
0.2) lowers its trust but not below the quarantine level — see below.

## Summary

| | live lab (1 round) |
|---|---|
| failures detected | 5 / 5, median 2.1 s (≈ one step) |
| healing actions verified | 5 / 5 incidents |
| service restored (incidents that affected traffic) | 4 / 4, median 2 s, max 24 s |
| failure predicted before it started | 1 / 1 with a warning ramp, 46 s ahead |
| attacks detected | 4 / 4 valid, median 2 s |
| attackers quarantined | 3 / 4 |
| innocent devices quarantined | 0 |

## What this shows (and what it does not)

* **The closed loop works on a real data plane.** Hard faults (cut,
  disconnect, switch down) were detected in one 2 s step and repaired or
  routed around within 6 s, with the repair *verified* by real pings. The
  physical cut — which the controller itself could not see — was found by
  the link probes, rerouted in 2 s, and the RESTORE that cannot fix physical
  damage was correctly escalated to a human. After each repair the routes
  failed back.
* **Prediction can give early warning on the live network.** The model, which
  learnt only what this network looks like when healthy, flagged the
  latency ramp 46 s before the failure. The decision engine still acted only
  34 s *after* the fault started — the proactive risk path needs a stronger
  signal than the early ramp — so on this run prediction warned operators
  early but did not make healing earlier. The simulation found the same:
  prediction helps when detection is slow, and detection here is 2 s.
* **Soft faults are slower.** 20 % loss took 20 s before the first action: loss
  is measured over a 5 s probe window and must persist before the risk score
  passes the action threshold. 10 % of the affected traffic was lost meanwhile.
* **Zero Trust caught every valid attack in 0–4 s** and never quarantined an
  innocent device. Spoofed packets are dropped at the first switch, so the
  spoofer was quarantined in one step.
* **The flood was detected but its sender was not quarantined.** A single
  volume anomaly on a high-trust admin device is, by design, not enough
  (anomaly weights in `zero_trust_policy.yaml`). Whether that is right is a
  policy choice: raising the flood/volume weight would quarantine it, at the
  risk of punishing legitimate bursts.

Limitations: one round on a 9-device emulated network on a 2-CPU VM; no
confidence intervals; the attacks and failures are the ones we designed;
"emulated" means real switches and real packets, but virtual links on one
machine, not physical hardware.
