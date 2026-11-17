# Live lab experiment

Real network: Mininet + Open vSwitch + self-healing Ryu app, topology `enterprise-redundant.yaml`, autonomy L2, step 2.0 s, faults 60.0 s, attacks 40.0 s, 45.0 s quiet gap after each, 1 round(s). Predictor: z-score learnt from the live warm-up, threshold 0.8.

## Failures

| incident | detected (s) | predicted lead (s) | first action (s) | verified (s) | service restored (s) | traffic lost | actions |
|---|---|---|---|---|---|---|---|
| physical cut sw-a<->sw-b | 2.1 | — | 2.0 | 6.0 | 2.0 | 0 % | RESTORE FAILED_BACK, REROUTE FAILED_BACK, RESTORE ESCALATED |
| link disconnected sw-a<->sw-b | 2.1 | — | 2.0 | 6.0 | 2.0 | 0 % | RESTORE FAILED_BACK, REROUTE FAILED_BACK |
| rtr-core down | 2.1 | — | 2.0 | 6.0 | — | — | RESTORE FAILED_BACK |
| 150 ms latency on sw-a<->sw-b after a 60 s warning ramp | 0.0 | 46.0 | 34.0 | 38.0 | 0.0 | 0 % | REROUTE FAILED_BACK |
| 20 % loss on sw-a<->sw-b | 4.0 | — | 20.0 | 26.0 | 24.0 | 10 % | REROUTE FAILED_BACK |

## Attacks

| attack | attacker | detected (s) | quarantined (s) | anomalies | other devices quarantined |
|---|---|---|---|---|---|
| port_scan (invalid: VM paused) | h5 | — | — | — | none |
| brute_force | h3 | 4.0 | 6.0 | auth_failures | none |
| spoofing | h4 | 0.0 | 2.0 | fingerprint_mismatch, policy_violation | none |
| lateral_movement | h2 | 2.0 | 24.0 | policy_violation | none |
| flood | h1 | 2.0 | — | volume_anomaly | none |

## Summary

```json
{
  "failures": 5,
  "detected": 5,
  "detection_s": {
    "n": 5,
    "median": 2.1,
    "max": 4.0
  },
  "verified_actions": 5,
  "restored": 4,
  "restored_s": {
    "n": 4,
    "median": 2.0,
    "max": 24.0
  },
  "predicted": 1,
  "attacks": 4,
  "attacks_detected": 4,
  "attack_detection_s": {
    "n": 4,
    "median": 2.0,
    "max": 4.0
  },
  "attackers_quarantined": 3,
  "false_quarantines": [],
  "invalid_incidents": [
    "port_scan"
  ]
}
```
