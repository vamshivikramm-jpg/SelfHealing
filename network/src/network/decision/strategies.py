"""Strategies (Submodule 6.3): which intents a situation calls for.

Network strategies (gated by the combined risk, Eq. 4):

| Situation                                   | Intents |
|---------------------------------------------|---------|
| link or switch/router DOWN                  | RESTORE + REROUTE the traffic that used it (if another path exists) |
| host/server DOWN                            | RESTORE |
| link DEGRADED by latency / loss / drops     | REROUTE away from it, else MONITOR (no alternative) |
| link DEGRADED by utilization                | LOAD_BALANCE onto other paths, else MONITOR |
| link healthy but failure predicted          | proactive REROUTE, else MONITOR |

Security strategies (driven by Module 5's trust levels and anomalies,
which are already a fused score, so they bypass the risk gate):

| Situation                                   | Intent |
|---------------------------------------------|--------|
| device QUARANTINED by Zero Trust            | QUARANTINE |
| flood or volume anomaly from a device       | BLOCK its traffic |
| identity spoofing from a device             | ISOLATE its switch port (needs approval) |

Hosts and servers have a single link, so REROUTE is only possible
around switch/router links; their access links can only be RESTOREd or
MONITORed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from network.decision.graph import TRANSIT_KINDS, NetworkGraph, Path, path_links
from network.decision.intents import Intent, IntentType
from network.decision.risk import RiskAssessment, Signals

Routes = Dict[Tuple[str, str], Path]


def _weights(policy) -> Tuple[float, float, float]:
    return policy.cost_loss, policy.cost_latency, policy.cost_reliability


def _edge_cost(policy, src: str, dst: str, avoid):
    """None for Eq. 6 routing; for the random-reroute baseline a random but
    stable cost per (pair, failure), so the choice does not flap."""
    if getattr(policy, "route_selection", "cost") != "random":
        return None
    import random

    rng = random.Random(f"{policy.random_seed}:{src}:{dst}:{sorted(avoid)}")
    costs = {}
    return lambda edge: costs.setdefault(edge.name, rng.random() + 1e-3)


def _path_cost(graph: NetworkGraph, path: Path, policy) -> float:
    return sum(graph.cost(graph.edges[l], *_weights(policy)) for l in path_links(path))


def reroute_moves(graph: NetworkGraph, routes: Routes, policy, avoid_links=frozenset(), avoid_nodes=frozenset(),
                  only_if_cheaper: bool = False):
    """Move every demand whose route touches the avoided links/nodes.

    Returns ``(moves, stranded)``: moves are dicts with old/new paths;
    stranded demands have no usable alternative.
    """
    moves, stranded = [], []
    for (src, dst), path in sorted(routes.items()):
        links = set(path_links(path))
        if not (links & set(avoid_links) or set(path[1:-1]) & set(avoid_nodes)):
            continue
        found = graph.shortest_path(src, dst, _weights(policy), set(avoid_links), set(avoid_nodes),
                                    edge_cost=_edge_cost(policy, src, dst, set(avoid_links) | set(avoid_nodes)))
        if found is None:
            stranded.append((src, dst))
            continue
        new_path, new_cost = found
        if only_if_cheaper and _edge_cost(policy, src, dst, ()) is None and \
                new_cost >= _path_cost(graph, path, policy):
            stranded.append((src, dst))
            continue
        moves.append({"src": src, "dst": dst, "old": list(path), "new": list(new_path)})
    return moves, stranded


def load_balance_moves(graph: NetworkGraph, routes: Routes, link: str, policy):
    """Greedily move demands off ``link`` while the busiest link gets lighter."""
    current = dict(routes)
    util = graph.projected_utilization({}, 0.0)
    moves = []
    for (src, dst), path in sorted(routes.items()):
        if link not in path_links(path):
            continue
        found = graph.shortest_path(src, dst, _weights(policy), {link}, min_free_mbps=policy.demand_mbps,
                                    edge_cost=_edge_cost(policy, src, dst, {link}))
        if found is None:
            continue
        new_path = found[0]
        before = max(util.get(l, 0.0) for l in path_links(path))
        after_util = dict(util)
        for l in path_links(path):
            after_util[l] = after_util.get(l, 0.0) - 100.0 * policy.demand_mbps / graph.edges[l].capacity_mbps
        for l in path_links(new_path):
            after_util[l] = after_util.get(l, 0.0) + 100.0 * policy.demand_mbps / graph.edges[l].capacity_mbps
        after = max(after_util.get(l, 0.0) for l in set(path_links(path)) | set(path_links(new_path)))
        if after < before:
            util = after_util
            current[(src, dst)] = new_path
            moves.append({"src": src, "dst": dst, "old": list(path), "new": list(new_path)})
    return moves, util.get(link, 0.0)


def _route_post(moves, link=None, node=None) -> List[dict]:
    out = []
    for m in moves:
        check = {"check": "route_avoids", "src": m["src"], "dst": m["dst"]}
        check.update({"link": link} if link else {"node": node})
        out.append(check)
    return out


def network_candidates(graph: NetworkGraph, signals: Signals, assessments: Dict[str, RiskAssessment],
                       routes: Routes, policy) -> List[Intent]:
    intents: List[Intent] = []
    ts = signals.ts
    # act proactively only on scores the predictor itself treats as an alarm
    proactive_at = max(policy.proactive_above, signals.prediction_threshold or 0.0)
    for key, a in assessments.items():
        if a.risk < policy.act_above:
            continue
        report = signals.health.get(key) or {}
        status = report.get("status")
        reasons = " ".join(report.get("reasons", []))
        if a.kind == "node":
            if status != "DOWN":
                continue
            intents.append(Intent(IntentType.RESTORE, key, f"{a.target} is down", a.risk, ts,
                                  {"node": a.target}, [{"check": "node_up", "node": a.target}]))
            if graph.kinds.get(a.target) in TRANSIT_KINDS:
                moves, stranded = reroute_moves(graph, routes, policy, avoid_nodes={a.target})
                if moves:
                    intents.append(Intent(IntentType.REROUTE, key, f"route traffic around failed {a.target}",
                                          a.risk, ts, {"moves": moves, "stranded": stranded, "fault": "DOWN"},
                                          _route_post(moves, node=a.target)))
            continue

        link = a.target
        ends = graph.endpoints(link)
        if any((signals.health.get(f"node:{n}") or {}).get("status") == "DOWN" for n in ends):
            continue  # the failed node's own intents (RESTORE / REROUTE) cover its links
        if status == "DOWN":
            intents.append(Intent(IntentType.RESTORE, key, f"link {link} is down", a.risk, ts,
                                  {"link": link}, [{"check": "link_up", "link": link}]))
            moves, stranded = reroute_moves(graph, routes, policy, avoid_links={link})
            if moves:
                intents.append(Intent(IntentType.REROUTE, key, f"route traffic around failed link {link}",
                                      a.risk, ts, {"moves": moves, "stranded": stranded, "fault": "DOWN"},
                                      _route_post(moves, link=link)))
        elif status == "DEGRADED" and "utilization" in reasons and not (
                "latency" in reasons or "loss" in reasons or "drop" in reasons):
            moves, projected = load_balance_moves(graph, routes, link, policy)
            if moves:
                intents.append(Intent(IntentType.LOAD_BALANCE, key, f"link {link} is congested ({reasons})",
                                      a.risk, ts, {"moves": moves, "projected_utilization_pct": projected},
                                      [{"check": "utilization_below", "link": link,
                                        "pct": policy.utilization_target_pct}]))
            else:
                intents.append(_monitor(key, f"link {link} congested and no other path can take load", a, ts))
        elif status == "DEGRADED":
            moves, _ = reroute_moves(graph, routes, policy, avoid_links={link}, only_if_cheaper=True)
            if moves:
                intents.append(Intent(IntentType.REROUTE, key, f"link {link} is degraded ({reasons})", a.risk, ts,
                                      {"moves": moves}, _route_post(moves, link=link)))
            else:
                intents.append(_monitor(key, f"link {link} degraded ({reasons}); no better path", a, ts))
        elif a.components["prediction"] >= proactive_at:
            moves, _ = reroute_moves(graph, routes, policy, avoid_links={link})
            if moves:
                intents.append(Intent(IntentType.REROUTE, key,
                                      f"failure predicted on {link} (score {a.components['prediction']:.2f})",
                                      a.risk, ts, {"moves": moves, "proactive": True},
                                      _route_post(moves, link=link)))
            else:
                intents.append(_monitor(key, f"failure predicted on {link}; no alternative path", a, ts))
    return intents


def _monitor(key: str, reason: str, a: RiskAssessment, ts: float) -> Intent:
    return Intent(IntentType.MONITOR, key, reason, a.risk, ts, {"escalate": True})


def security_candidates(graph: NetworkGraph, signals: Signals,
                        assessments: Dict[str, RiskAssessment]) -> List[Intent]:
    intents: List[Intent] = []
    ts = signals.ts
    for device in sorted(signals.trust):
        key = f"node:{device}"
        risk = assessments[key].risk if key in assessments else 0.0
        info = signals.trust[device]
        kinds = set(signals.anomalies.get(device, []))
        blocked = [{"check": "access_blocked", "subject": device}]
        if info.get("level") == "QUARANTINED":
            intents.append(Intent(IntentType.QUARANTINE, key,
                                  f"{device} quarantined by Zero Trust (trust {info['trust']:.2f})",
                                  risk, ts, {"subject": device}, blocked))
        if kinds & {"flood", "volume_anomaly"}:
            intents.append(Intent(IntentType.BLOCK, key, f"{device} is flooding the network", risk, ts,
                                  {"subject": device, "anomalies": sorted(kinds)}, blocked))
        if "fingerprint_mismatch" in kinds:
            intents.append(Intent(IntentType.ISOLATE, key, f"{device} is spoofing another device's identity",
                                  risk, ts, {"subject": device, "port_of": device}, blocked))
    return intents
