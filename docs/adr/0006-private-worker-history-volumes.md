# Keep worker identity and history private across restarts

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

Shared workers need durable event spools during Control API outages. A shared history volume or overlapping identity can mix pending records, while Deployment-owned storage can be garbage-collected when a worker is removed.

Each native worker uses a distinct identity, a one-replica Recreate Deployment and a private retained PVC without a Deployment owner reference. Configuration payloads use immutable versioned Secrets/ConfigMaps. Reconfiguration stops the old Pod before adopting the new manifest. Removal preflights current and previous recorded payload generations; UID, normalized spec or owner changes block cascading deletion. Worker runtime readiness, Hatchet registration and heartbeat remain separate observations.

**Consequences:** history can replay with deduplication after restart, and worker removal deliberately leaves a spool volume to manage. Local-path storage is node-bound and does not provide node-loss HA. Configure/start operation completion does not certify Pod readiness or Hatchet registration. Multi-replica identity overlap, automatic rolling migration and verified graceful drain are not supported.

See [worker adapter](../../control-plane/backend/app/adapters/kubernetes_workers.py) and [worker lifecycle and storage](../kubernetes.md#worker-lifecycle-configuration-and-storage).
