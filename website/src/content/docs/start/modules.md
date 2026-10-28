---
title: "Module and configuration map"
description: "Find each module's purpose, configuration owner, extension seam and diagnostic view."
---

Start with [the architecture diagram](/flumic-software-factory/start/architecture/)
for relationships, then use this map to decide where a change belongs. A module
may contain several Python files; it does not necessarily mean another service.

## What runs where

| Module | Purpose and source | Configure through | Diagnose through |
| --- | --- | --- | --- |
| Control UI | React project views and operator forms; `control-plane/frontend/src/features/` | Browser origin and API deployment; project selection scopes project views | Browser request errors and matching API logs |
| Control API | FastAPI commands, validation and queries; `control-plane/backend/app/main.py` and route installers | Private deployment settings; saved project/repository/workflow revisions | Authenticated API responses, command receipts and audit history |
| Configuration compiler | Resolve precedence, validate settings and produce immutable snapshots; `app/services.py`, `worker/factory/configuration.py` | Project defaults → repository overrides → workflow → stage; legacy profiles have a separate task precedence | Validation errors, loaded revision and pinned context |
| Run state and claims | Admission, stage transitions and canonical execution identity; `app/workflow_state.py`, `runtime_claims.py` | PostgreSQL and operator limits; runs keep launch pins | Project run/attempt state and recovery diagnostics |
| Dynamic coordinator | Fixed Hatchet control handlers, stage dispatch and durable waits; `app/dynamic_runtime.py`, `worker/factory/workflows/` | Hatchet transport and published workflow graph | Handler registration, outbox/dispatch progress and stage attempts |
| Shared workers | Execute installed actions for authorized projects; `worker/factory/shared_worker.py` | Pool role/capabilities, image and startup transport | Global Workers plus project assignment/attempt details |
| Application operations | Intake, planning, coding, review and publication; `worker/factory/application/` | Effective models, prompts, policies and task limits | Stage outcomes, structured logs and model-call records |
| Runtime broker/adapters | Worker lifecycle and one-time container actions; `app/job_broker.py`, `runtime_router.py`, `app/adapters/` | Operator-approved Docker/Kubernetes target and immutable runtime pins | Runtime targets, lifecycle receipts and Job capture/cleanup state |
| Repository and model adapters | Translate stable operation contracts to provider behavior; `worker/factory/adapters/` | Repository connections, granted credentials, model choices and trusted installed factories | Provider checks/errors and redacted model history |
| History and observability | Persist correlated events/usage and optionally export OTLP; `app/history.py`, `worker/factory/adapters/otel.py` | Retention/capture policy, price file and telemetry endpoint | Project logs, usage coverage, traces and optional SigNoz dashboards |

Paths beginning with `app/` live under `control-plane/backend/`.

## Choose the right guide

| Goal | Read |
| --- | --- |
| First project, repository and run | [Project onboarding](/flumic-software-factory/start/project-onboarding/) |
| Explain each worker setting and example | [Worker settings](/flumic-software-factory/handbook/configuration-workers/) |
| Change retries, scheduling, labels or task prompts | [Workflow settings](/flumic-software-factory/handbook/configuration-workflows/) |
| Configure authentication, database, transport or filesystem paths | [Control Plane settings](/flumic-software-factory/handbook/configuration-control-plane/) |
| Author and link new stages | [Workflow development](/flumic-software-factory/handbook/workflow-development/) |
| Execute one-time container actions | [Container contract](/flumic-software-factory/handbook/docker-action-jobs/) |
| Deploy native worker Deployments and Jobs | [Kubernetes](/flumic-software-factory/operations/kubernetes/) |
| Integrate another client with the backend | [API recipes](/flumic-software-factory/api/recipes/) and [project API](/flumic-software-factory/handbook/project-runtime-api/) |
| Understand trace/token/cost coverage | [Project accounting](/flumic-software-factory/handbook/project-observability/) |

## Verify configuration adoption

For example, changing a project's model default affects newly admitted runs after
resolution. A stage-level model override still wins. An existing run retains its
original settings. Changing a worker image is different: build/publish the image
and explicitly recreate the worker; adding a capability string does not load code.

For legacy managed workers, save a profile revision and explicitly adopt it at
compatible startup/restart. Editing checked-in YAML alone does not replace the
saved database configuration. See [ownership and precedence](/flumic-software-factory/configuration/).

## Investigate from the execution outward

1. Find the project run and stage attempt, including its immutable revision.
2. Check assignment and worker registration before assuming a scheduler fault.
3. For container actions, inspect target and Job state, then captured results.
4. Follow trace/model links for reported tokens and known estimated cost.

![Stage attempts with execution identity and status](/flumic-software-factory/images/control-plane-stage-attempts.png)

This retained screenshot illustrates attempt inspection; it is not a fresh
installation baseline. Do not treat missing logs or usage as success or zero cost.

## Extend at an existing seam

Prefer a workflow/configuration change when the installed actions already express
the behavior. Add a trusted builtin action for reusable application behavior, or
use the container result contract for a packaged one-time action. Implement a
repository/model adapter when the external integration varies. Runtime adapters
own infrastructure and recovery; workflow YAML does not bypass their policy.

Read the [accepted architecture decisions](/flumic-software-factory/architecture/decisions/)
before changing claims, immutable pins, capture ordering or cleanup. These are
correctness constraints, not incidental complexity.
