# Docs Index — AI-Powered Autonomous Zero Trust Self-Healing Network

Design and research documentation for the project. Implemented one module at a time; see the development roadmap for ordering.

## Blueprint Documents

| Document | Description |
|---|---|
| [architecture.md](architecture.md) | System architecture, module interaction matrix, high-level data flow |
| [modules.md](modules.md) | Per-module responsibilities and interfaces |
| [database-schema.md](database-schema.md) | ER diagram, table catalog, constraints |
| [api-design.md](api-design.md) | REST endpoints: method, request, response, status codes |
| [class-diagrams.md](class-diagrams.md) | Interfaces, abstract classes, services, repositories, controllers (SOLID) |
| [sequence-diagrams.md](sequence-diagrams.md) | Failure prediction, healing, trust verification, decision making |
| [docker-deployment.md](docker-deployment.md) | Container architecture, networks, compose topology |
| [development-roadmap.md](development-roadmap.md) | Phase-by-phase build plan with exit criteria |
| [research-contribution.md](research-contribution.md) | Novelty, existing approaches, proposed algorithm, mathematical model, comparison, metrics, evaluation methodology — basis of the IEEE paper and final report |

## Status

- **Module 1 (Virtual Enterprise Network): COMPLETE** — Version 1.0.0. Includes configurable enterprise topology (1.2), Network Control Layer with `NetworkController`, `NetworkState`, in-process `EventBus` (13 event types), safety exceptions, structured JSON logging (1.3), Failure Injection Framework with 6 typed failure classes, `FailureRegistry`, `FailureManager` for router/switch/link/impairment injection and recovery (1.4A–1.4C), and reusable failure scenarios (1.4D). 219 unit tests passing. No AI, telemetry, trust, or healing logic.
- Research contribution document: v1.0 drafted
