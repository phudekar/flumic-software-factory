# Architecture decision records

These accepted decisions explain the implemented project and Kubernetes architecture. They preserve the alternatives and operational costs that are easy to miss when reading code. They record existing decisions; they do not introduce a new runtime or deployment promise.

| ADR | Decision |
| --- | --- |
| 0001 | [Projects own execution; worker pools share capacity](0001-projects-and-shared-worker-pools.md) |
| 0002 | [Use native runtime adapters behind portable workflow contracts](0002-native-runtime-adapters.md) |
| 0003 | [Use PostgreSQL claims and immutable execution authority](0003-postgresql-runtime-authority.md) |
| 0004 | [Fence namespace incarnations and remote resource mutations](0004-namespace-and-resource-fencing.md) |
| 0005 | [Separate action exit, canonical capture and guarded cleanup](0005-capture-before-cleanup.md) |
| 0006 | [Keep worker identity and history private across restarts](0006-private-worker-history-volumes.md) |
| 0007 | [Use bounded action transport and explicit isolation limits](0007-container-isolation-and-observation.md) |

Read the [overall architecture and diagram](../architecture.md) first and the [Kubernetes operator guide](../kubernetes.md) before enabling a target. Shared worker pools do not erase project ownership, and shared contracts do not imply identical runtime isolation.

Testing mechanics, temporary CI timeout choices, commit ordering and per-run verification records remain implementation history rather than ADRs. The [approved Kubernetes design](../superpowers/specs/2026-10-26-kubernetes-runtime-design.md) supplies additional design context; current supported behavior is documented in the operator guides.
