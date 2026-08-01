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

- Module 1 (Virtual Enterprise Network): Submodule 1.1 (bootstrap) complete; Submodule 1.2 (enterprise topology generator) complete; Submodule 1.3 (Network Control Layer) complete — `NetworkController` is the only interface to Mininet, with `NetworkState` sync, in-process `EventBus` (7 event types), safety exceptions, structured JSON logging; launcher routes through the controller; 68 unit tests green (`make net-test` on Linux). Next: Module 2 (telemetry) or topology expansion
- Research contribution document: v1.0 drafted
