---
title: "Architecture and adaptability"
description: "How the control plane, workers, orchestration and adapter boundaries make Flumic AI configurable and extensible."
---

Flumic AI separates operator control, workflow scheduling and development operations. Explicit adapter boundaries keep model, storage, repository and telemetry integration details out of the application core.

![Flumic AI system architecture showing Control Plane services, Hatchet, worker roles and external integration adapters](/flumic-software-factory/images/flumic-architecture.svg)

[Open the full-size diagram](/flumic-software-factory/images/flumic-architecture.svg).

## Three architectural responsibilities

| Responsibility | Components | Adaptability |
| --- | --- | --- |
| Operator control | React UI, FastAPI API, PostgreSQL and reconciler | Version settings independently of worker instances; apply lifecycle requests through a durable runtime boundary. |
| Workflow execution | Hatchet, simple/developer worker roles and application operations | Tune task policies and scale roles independently; keep scheduler code separate from development operations. |
| Integration and policy | Agent/repository/state/observer ports, adapters and shared domain policy | Select model implementations and observation tools while retaining typed contracts and common publication checks. |

A concrete implementation behind a port is still required: the current system uses GitHub, Docker and shared single-host execution state. GitLab workflows and Kubernetes orchestration are planned; they are not enabled by changing a provider name.

## Go deeper

- [How work flows and why each component helps adaptability](/flumic-software-factory/handbook/architecture/#why-each-component-makes-the-architecture-adaptable)
- [Full architecture, code map and extension contracts](/flumic-software-factory/handbook/architecture/)
- [Configuration surfaces and field-by-field examples](/flumic-software-factory/configuration/)
- [Worker scaling and current deployment limits](/flumic-software-factory/operations/scaling/)
