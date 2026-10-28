---
title: "Scale worker capacity"
---

## Scale roles independently

Simple workers handle intake/planning/review while developer workers handle coding. Add the role that is saturated, then inspect engine registration, active tasks and stage duration. More containers do not override task routing, concurrency, provider rate limits or shared state constraints.

For Compose:

```sh
./scripts/start-workers.sh --scale worker=3 --scale developer-worker=2 -d
```

For managed instances, create additional uniquely named workers using the intended saved profile and start each explicitly. Profiles allow consistent behavior across a role; their next starts/restarts pick up saved revisions.

## Limits to coordinate

- `workflow_concurrency` bounds active issue workflows.
- Per-task `concurrency` can serialize specific stages.
- Worker labels determine which role can accept a task.
- Provider quotas, coding timeouts and host CPU/memory bound useful parallelism.
- Legacy static workers need the same SQLite database and lock directory on one host. Project shared workers use PostgreSQL through the Control Pane.

Use **Workers → Add shared worker** to add role-compatible capacity to a pool on an operator-configured Kubernetes target. Each identity gets a one-replica Deployment and private retained PVC. PostgreSQL execution state and fenced claims support workers on different nodes; local-path PVCs still pin history to their storage node. There is no automatic worker autoscaler or verified graceful drain. Stop can interrupt tasks; see [Kubernetes deployment and limits](/flumic-software-factory/operations/kubernetes/).

[Task routing and precedence](/flumic-software-factory/handbook/worker-configuration/) · [Runtime architecture](/flumic-software-factory/handbook/architecture/)
