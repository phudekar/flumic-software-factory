---
title: "Manage shared workers and images"
---

## Provision capacity

Open **Shared workers → Add shared worker**. Choose a pool, an available runtime
target and a unique worker name. Start the provisioned worker explicitly. Authorize
the pool from the project's **Shared worker pools** page before assigning work.

![Current shared worker controls](/flumic-software-factory/images/control-plane-shared-workers.png)

Pools define role, installed actions and slots. Project configuration defines
models, prompts and behavior. Adding a worker increases capacity without creating
another copy of a project's configuration.

## Lifecycle actions

| Action | Effect |
| --- | --- |
| Start | Starts the provisioned stopped runtime |
| Restart | Stops, recreates with the selected pool image and starts the runtime |
| Stop | Can interrupt active work; graceful drain is not claimed |
| Remove stopped runtime | Removes the stopped runtime while retaining history |

Check operation status, runtime state and Hatchet registration separately. An
accepted request is not a completed operation. Older failed operations remain in
audit history even if a later retry succeeds. Read-only discovered containers may
still appear; they are not a way to provision the retired static fleet.

## Update installed code

```sh
just images-build
```

Build before restarting affected workers. A plain Docker restart cannot change
an image. Keep runtime target identity and the canonical host root stable during
local rebuilds; changing a checkout path can invalidate saved target pins.

The old **Create worker**, **Change profile** and image/profile editing forms have
been removed. Use project configuration for behavior and explicit image builds
for installed dependencies. Inspect runtime startup logs when a worker fails to
register; project stage logs only cover recorded executions.

[Runtime targets](/flumic-software-factory/operations/kubernetes/) ·
[Upgrade and retirement notes](/flumic-software-factory/handbook/legacy-retirement/)
