---
title: "Core concepts"
---

## Workflow, run, stage and attempt

A workflow is a registered definition. A run is one execution of that definition. A stage corresponds to a task in that execution. An attempt is a particular try, including retries. A model call belongs to the active stage/attempt when instrumentation is available.

```text
Workflow definition
  └─ Run
      └─ Stage / engine task
          ├─ Attempt 1 → worker → error
          └─ Attempt 2 → worker → recorded model calls → tokens / estimates
```

Engine workflow/task IDs and factory run/attempt IDs are related but not interchangeable. The UI preserves their relationship through links and filters; use those links rather than guessing identifiers.

## Worker roles and instances

`simple` and `developer` are routing roles. An instance is a container/process that advertises its role. Several instances can share one saved worker profile. Adding containers adds execution capacity; it does not bypass workflow concurrency limits or provider quotas.

## Factory revision, profile and snapshot

Factory revisions define shared orchestration and policy. Profiles contain worker behavior and pin a factory revision. Saving a profile creates an immutable revision in a family; cloning creates another family. Runtime snapshots combine the selected settings. Telemetry records the actual loaded revision; creation-time Docker labels may differ after direct process restarts.

[Profile operations](/software-factory/control-plane/profiles/) · [Stage management](/software-factory/workflows/stages/)
