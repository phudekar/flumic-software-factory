# Use native runtime adapters behind portable workflow contracts

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

Docker is the default deployment runtime. Supporting Kubernetes through a separate operator and custom resources would add another control loop, installation contract and workflow representation to maintain.

The Control Plane uses native Kubernetes API adapters for shared-worker Deployments and one-time action Jobs, alongside the Docker adapters. The existing workflow schema v1 retains its historical `docker` container-stage identifier; the selected worker target determines which runtime executes it. Targets are operator-owned configuration, not workflow-provided cluster URLs or Pod specifications. The registry currently permits built-in Docker and at most one Kubernetes target.

**Consequences:** authors keep the same stage contracts across runtimes and workers can share application code. Kubernetes installation still requires approved images, storage, namespace admission policy, RBAC and isolation qualification. Adapter boundaries do not imply identical networking, log streams or scratch-space behavior. No CRDs, automatic cluster provisioning, HPA or legacy-worker migration are provided.

See [runtime router](../../control-plane/backend/app/runtime_router.py), [target contracts](../../control-plane/backend/app/runtime_targets.py) and [Kubernetes setup](../kubernetes.md).
