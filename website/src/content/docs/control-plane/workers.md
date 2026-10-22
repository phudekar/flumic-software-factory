---
title: "Manage workers and images"
---

## Create and start

Choose **Workers → Create worker**, enter a unique name, and choose a saved simple or developer profile. Creation queues an operation and leaves the runtime stopped. Start separately once the image is ready. Watch both runtime state and Hatchet registration; a running container alone does not prove it can execute tasks.

![Managed workers with runtime, Hatchet registration and configuration revision](/software-factory/images/control-plane-workers.png)

## Lifecycle actions

| Action | Behavior |
| --- | --- |
| Start | Prepares the latest linked profile and starts a stopped instance |
| Restart | Stops/recreates with the latest applicable profile/image and starts it |
| Stop | Stops after the grace period; active work may be interrupted and retried |
| Change profile | Requires a stopped worker; replacement profile must have the same worker type |
| Remove stopped runtime | Preserves history, hides the removed instance, and cannot restart it later |

Only managed instances expose these controls. Discovered Compose workers are read-only; use the original Compose deployment. Use **Show removed workers** and **Show exited discovered workers** for historical/stopped entries. Removal can be retried if a container is already absent.

## Build and startup troubleshooting

Worker names open details containing stage-attempt history and usage. **Build logs** tracks assembly, including builds that failed before container creation. **Startup logs** contains runtime output, including failures before task registration. Filtered stage logs cannot reconstruct failures that occurred before ingestion.

Image build settings select an approved Debian/Ubuntu/Alpine base, system packages, pinned CLI versions and a build script. Build scripts execute during assembly and must not contain secrets. Startup scripts run on every process start, are timeout bounded, and should be idempotent. Changing an image requires runtime replacement; a direct Docker restart keeps the installed image.

[Image build architecture and caching](/software-factory/handbook/worker-image-builds/) · [Scaling workers](/software-factory/operations/scaling/)
