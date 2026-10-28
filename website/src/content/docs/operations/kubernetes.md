---
title: "Run workers on Kubernetes"
---

The native runtime manages shared worker **Deployments** and one-time action **Jobs** through the Kubernetes API. Docker remains available. The runtime is qualified on a dedicated two-node k3d 5.9.0 / K3s 1.35.9 cluster with PostgreSQL; operators must qualify their own CNI, PID limits, images and storage.

## Configure a destination

1. Install the operator-reviewed namespaces, admission fence, scoped RBAC and action network policies.
2. Pin actual namespace UIDs, approved worker/helper image digests, endpoint, storage and qualification evidence in `CONTROL_RUNTIME_TARGETS_FILE`.
3. Mount the same private operator file into the API and reconciler and recreate both. Cluster credentials stay with those services.
4. Open **Runtime targets**, then **Workers → Add shared worker**. Choose a pool, name and target; start the new stopped worker explicitly.

![Runtime targets and execution limitations](/flumic-software-factory/images/control-plane-runtime-targets.png)

*Illustrative fixture data, not a live cluster status report.*

[Every target key, example and execution effect](/flumic-software-factory/handbook/kubernetes/) · [Deployment templates and bootstrap](/flumic-software-factory/handbook/kubernetes/#target-registry-and-reload-rules)

## Understand execution and recovery

Projects own repositories, workflow revisions, behavior and grants. Worker pools share compatible capacity across projects. A target ID names an operator-owned destination; it is independent of a project, Kubernetes context or namespace. Workflow schema v1 keeps its historical `docker` container-action identifier, while the pinned worker target chooses the execution adapter.

Each shared worker has a distinct identity and retained private history PVC. Runtime readiness, Hatchet registration and heartbeat are separate observations. Project Job views show target, namespace, Job/Pod UIDs, capture phase and cleanup state. Kubernetes action logs are combined; uncaptured output can be unavailable after a hard deadline.

The controller uses PostgreSQL leases, durable suspended intents, immutable payloads, UID/resourceVersion guards and namespace admission fencing. Cancellation stops execution before guarded cleanup. Recovery preserves accepted configuration and grants; external side effects do not have an exactly-once guarantee.

## Deployment limits

Kubernetes scratch is executable. Helper containers add CPU/memory overhead. `none` means qualified default-deny policy, with Kubernetes' standard node-traffic exception; it does not remove the Pod's network interface. Local-path storage does not provide node-loss high availability. Worker autoscaling, verified graceful drain, multi-user authorization and remote function execution remain separate work.

[Qualification, CI and troubleshooting](/flumic-software-factory/handbook/kubernetes/#qualification-and-reproducible-ci)
