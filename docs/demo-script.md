# Final demonstration script (~15 minutes)

Everything shown runs on the **simulated** network. Say so at the start;
it is the honest framing and the dashboard banner says it too.

## Before the session (5 minutes)

```bash
cd SelfHealing
VIEWER_MODE=simulation python3 -m uvicorn viewer.app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 (internet needed once for the topology view).
Wait **at least 60 seconds** before injecting anything: the predictor and
the Zero Trust detectors learn normal behaviour first. Have
`docs/results.md` open in another tab for the numbers.

## 1. The problem and the idea (2 min) — Overview tab

- One enterprise network: core router, two access switches, five hosts,
  web and database servers; a redundant core link (simulation-only).
- The closed loop (research doc Algorithm 1): telemetry → failure
  prediction → Zero Trust → decision engine → self-healing → verify.
- Point at the KPI cards: everything healthy, nothing rerouted, autonomy L2.

## 2. A failure software cannot fix (3 min)

Control tab → Inject failure: **Link down**, target `sw-a-sw-b`,
duration 120 s, **Physical damage** ticked → Inject. Go to Overview.

- Within one step the core link turns dashed red and the health pill
  shows "1 down".
- Decisions & Healing: the engine decided RESTORE **and** REROUTE.
- RESTORE: the command "succeeds" but the check reads the link still down
  → retried → **ESCALATED** to a person. Explain: verification looks at
  the network, not at the command (the brief's requirement).
- REROUTE: verified; Overview now shows cyan rerouted links through the
  router and "Rerouted demands 4". Users keep working.
- After the planned repair, traffic fails back automatically.

## 3. Predicting a failure (2 min)

Control tab: **High latency**, target `sw-a-sw-b`, duration 60 s,
**Warning ramp first 60 s** → Inject.

- Predictions tab: the link's score rises before the failure starts; the
  link glows purple on the topology ("failure predicted").
- Telemetry tab (click the link): latency ramps up, then jumps past the
  red threshold line when the failure starts.
- Be honest: the warning ramp is a designed pattern; on this data the
  simple z-score baseline already catches most such failures.

## 4. An attack (2 min)

Control tab → Launch attack: **Identity spoofing**, attacker `h4`, 45 s.

- Zero Trust tab: h4's trust collapses, it becomes QUARANTINED; security
  events show the identity mismatch. Every spoofed request was denied
  because identity is verified on each request (default deny).
- Decisions & Healing: QUARANTINE executed and verified with a probe flow
  that the switch rules drop.

## 5. Humans stay in control (2 min)

Control tab → Autonomy **L1**. Inject another link failure (e.g. `h3-sw-a`,
not physical). Decisions & Healing now shows it **waiting for approval**
with Approve / Reject. Approve it; watch it execute and verify. Set
autonomy back to L2.

## 6. Results (3 min) — `docs/results.md`

Show the healing table and figures: mean time to repair and user-visible
loss with vs without healing; the verification ablation (without checks
the system reports success for repairs that did nothing); Zero Trust
AUROC / false-accept / false-reject; scalability. Close with the
limitations: simulated data, designed warning patterns and attacks,
correctness judged against written rules, no live network yet — and the
Linux-lab next steps in `docs/deployment.md`.

## If something goes wrong

- Banner says OFFLINE PREVIEW → wrong Python environment (see
  docs/deployment.md, Troubleshooting).
- Need a guaranteed sequence? Control tab → **Run demo** plays a scripted
  ~7-minute version of steps 2–4 automatically.

## Live-lab variant (same steps on the real emulated network)

Run the same script on the live lab to show it is not only a simulation:

1. On the VM, ≥ 5 minutes before: `cd ~/SelfHealing && docker compose up -d && make live-dashboard`.
   Open `http://<vm-ip>:8000` on the presentation laptop. The banner says
   **LIVE LAB**; wait until the "learning normal traffic" countdown ends.
2. Step 2 (cut cable): Control → Link down, `sw-a-sw-b`, **Physical damage**.
   The link turns red within ~5 s (probes stop getting through, while the
   controller still thinks it is fine); REROUTE is verified by real pings;
   RESTORE escalates.
3. Step 4 (attack): Control → Port scan from h5. A real scanner runs inside
   h5; h5 is quarantined in a few seconds and the quarantine is verified by a
   ping that gets no answer (Zero Trust tab, Decisions & Healing tab).
4. Point out the differences from simulation: telemetry is *measured*, the
   attack is a real program, the drop rules are in the switches
   (on the VM: `docker compose exec mininet-net ovs-ofctl -O OpenFlow13 dump-flows sw-b`).
5. Results for the live lab: `docs/results-live.md`.
6. Afterwards: `make live-dashboard-stop`.
