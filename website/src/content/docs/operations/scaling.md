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
- Each worker needs access to the same factory SQLite database and lock directory on one host.

A control-plane PostgreSQL database does not make execution state distributed. Kubernetes and cross-host worker fleets require additional runtime/shared-state adapters. There is no verified graceful drain; Stop can interrupt tasks, so plan configuration adoption accordingly.

[Task routing and precedence](/software-factory/handbook/worker-configuration/) · [Runtime architecture](/software-factory/handbook/architecture/)
