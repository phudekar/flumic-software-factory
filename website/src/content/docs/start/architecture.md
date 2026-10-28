---
title: "Architecture and adaptability"
description: "How the control plane, workers, orchestration and adapter boundaries make Flumic AI configurable and extensible."
---

Flumic AI separates operator control, workflow scheduling and development operations. Explicit adapter boundaries keep model, storage, repository and telemetry integration details out of the application core.

![Flumic AI layered architecture with icons for developer experience, Control Plane, orchestration, shared execution, runtime targets, adapters and persistence, plus cross-cutting observability](/flumic-software-factory/images/flumic-architecture.svg)

[Open the full-size diagram](/flumic-software-factory/images/flumic-architecture.svg).

## Read the layers

From top to bottom: developer experience → Control Plane → workflow orchestration → shared execution → runtime targets → integration adapters → persistence. The observability rail spans the layers for logs, metrics, traces and model accounting. The arrows show conceptual responsibilities and dependencies, not a synchronous sequence that every request follows.

The application core is code shared by both worker roles; it is not an extra worker service. PostgreSQL stores Control Plane data, project-qualified dynamic execution state and claims. Legacy static workers retain SQLite and local locks. The detailed guide explains the actual command, bootstrap and execution paths.

## Three architectural responsibilities

| Responsibility | Components | Adaptability |
| --- | --- | --- |
| Operator control | React UI, FastAPI API, PostgreSQL and reconciler | Version settings independently of worker instances; apply lifecycle requests through a durable runtime boundary. |
| Workflow execution | Hatchet, simple/developer worker roles and application operations | Tune task policies and scale roles independently; keep scheduler code separate from development operations. |
| Integration and policy | Agent/repository/state/observer ports, adapters and shared domain policy | Select model implementations and observation tools while retaining typed contracts and common publication checks. |

## Authority and runtime boundaries

Projects own multiple repositories, workflow/configuration revisions, grants and history. Pools share compatible worker capacity across authorized projects. Operator-owned targets select Docker or native Kubernetes; workflow authors cannot provide cluster credentials or arbitrary Pod specifications. The v1 `docker` stage identifier remains the portable container-action contract.

PostgreSQL claims/epochs and immutable execution pins govern reconciliation. Kubernetes adds namespace admission fencing, UID/spec/owner checks and fresh metadata nonces for delayed-request fencing. Capture is persisted before collector release or cleanup; multiple live Pods cannot prove cancellation, and changed descendants block cascading deletion.

Native workers use distinct identities, one-replica Recreate Deployments and private retained history PVCs. Operation completion, Pod readiness, Hatchet registration and heartbeat are separate observations. Local-path storage is not node-loss HA; Kubernetes scratch is executable and action logs are combined. Unknown model usage remains unknown.

Concrete adapters implement the ports: GitHub or GitLab.com, PostgreSQL and Docker or native Kubernetes Deployments/Jobs. Docker retains a trusted broker host; Kubernetes workers share PostgreSQL state across nodes and keep private history volumes. Targets require operator-pinned images, namespace UIDs and qualified isolation; see [deployment configuration](/flumic-software-factory/operations/kubernetes/).

## Accepted decisions

[Read the ADR index](/flumic-software-factory/architecture/decisions/) for the choices, alternatives and operational costs behind project boundaries, native adapters, transactional authority, fencing, cleanup, storage and isolation.

## Go deeper

- [How work flows and why each component helps adaptability](/flumic-software-factory/handbook/architecture/#why-each-component-makes-the-architecture-adaptable)
- [Full architecture, code map and extension contracts](/flumic-software-factory/handbook/architecture/)
- [Configuration surfaces and field-by-field examples](/flumic-software-factory/configuration/)
- [Worker scaling and current deployment limits](/flumic-software-factory/operations/scaling/)
