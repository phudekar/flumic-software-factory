# Separate action exit, canonical capture and guarded cleanup

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

An action can exit while its collector remains alive, result transport can fail after execution, and a Job may temporarily have more than one Pod. Deleting the root first can lose output or garbage-collect a replaced descendant before ownership checks run.

Create Jobs suspended, bind observed identity, prepare immutable payloads, then admit execution within the original deadline. Persist canonical capture before releasing the collector or cleaning up. Cancellation fences and suspends the Job, then verifies execution has stopped; multiple live Pods are not terminal evidence. Validate every candidate Pod before binding or deletion and preflight recorded payload generations before cascading root deletion. Unexpected root loss does not authorize result capture; valid orphaned/late Pods are stopped through verified identities.

**Consequences:** action completion and cleanup can have different states. Changed payloads retain the stopped root for operator investigation; warnings and tombstones remain available for recovery. Missing capture stays unavailable rather than becoming fabricated success. Remaining whole-second admission budgets can reject work with less than one second left; neither absolute result cutoffs nor native Job deadlines guarantee instantaneous process death.

See [Job runtime](../../control-plane/backend/app/adapters/kubernetes_jobs.py), [broker reconciliation](../../control-plane/backend/app/job_broker.py) and [Kubernetes recovery](../kubernetes.md#action-execution-capture-and-cleanup).
