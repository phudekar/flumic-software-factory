---
title: "Scale worker capacity"
---

## Scale roles independently

Simple workers handle intake/planning/review while developer workers handle coding. Add the role that is saturated, then inspect engine registration, active tasks and stage duration. More containers do not override task routing, concurrency, provider rate limits or shared state constraints.

Open **Workers → Add shared worker**, select an authorized pool and runtime target,
and give each instance a unique name. Add simple-role capacity for planning/review
and developer-role capacity for coding. Both Docker and Kubernetes targets use
the same project execution and pool authorization contracts.

## Limits to coordinate

- `workflow_concurrency` bounds active issue workflows.
- Per-task `concurrency` can serialize specific stages.
- Worker labels determine which role can accept a task.
- Provider quotas, coding timeouts and host CPU/memory bound useful parallelism.
- Shared workers use PostgreSQL execution state through the Control Pane.

Use **Workers → Add shared worker** to add role-compatible capacity to a pool on an operator-configured Kubernetes target. Each identity gets a one-replica Deployment and private retained PVC. PostgreSQL execution state and fenced claims support workers on different nodes; local-path PVCs still pin history to their storage node. There is no automatic worker autoscaler or verified graceful drain. Stop can interrupt tasks; see [Kubernetes deployment and limits](/flumic-software-factory/operations/kubernetes/).

[Task routing and precedence](/flumic-software-factory/handbook/worker-configuration/) · [Runtime architecture](/flumic-software-factory/handbook/architecture/)
