# Use PostgreSQL claims and immutable execution authority

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

Host-local locks and independent SQLite databases cannot coordinate lifecycle effects across API replicas, shared workers and Kubernetes nodes. A remote resource name or an expired lease is insufficient proof that a new controller owns an execution.

PostgreSQL stores canonical project execution state, pinned target/configuration/grant snapshots, runtime leases with owner epochs, and scoped resource bindings. Claims fence canonical writes and durable intent precedes remote effects. A Job intent may have no UID only before observation; the actual remote UID is bound before payload delivery or execution. Historical views use admitted snapshots rather than a worker's current settings.

**Consequences:** controllers recover through shared transactional authority instead of host-file locks. Legacy static workflows retain their existing shared-host SQLite/file-lock adapter, and SQLite protocol tests do not certify distributed operation. An expired owner cannot commit outcomes; settling a timeout may require a fresh reconciliation pass. Schema downgrade is blocked while new runtime authority records remain. Database claims cannot make external source-host or model effects exactly once.

See [runtime claims](../../control-plane/backend/app/runtime_claims.py), [resource bindings](../../control-plane/backend/app/runtime_resources.py) and [rollout and recovery](../project-runtime-operations.md).
