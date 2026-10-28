# Fence namespace incarnations and remote resource mutations

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

A namespace can be recreated with the same name, and a Job, Secret or Pod can be replaced while retaining familiar names or labels. A client-side namespace read cannot fence a delayed write atomically. Kubernetes also preserves resourceVersion for a no-op update, so reasserting suspension alone cannot invalidate an older unsuspend request.

Require an operator-installed native ValidatingAdmissionPolicy and a namespace annotation containing its actual UID. The controller verifies UID and annotation, tests admission with positive/negative probes, and uses UID/resourceVersion preconditions. Bindings prove expected spec, owner and project identity; only explicitly known API defaults are normalized. Start retries and cancellation approve fresh metadata fence nonces before writing them remotely, retain approved pending metadata for lost-reply recovery, and invalidate old resource versions before selecting a new start budget.

**Consequences:** namespace bootstrap is an installation requirement, and future harmless API defaults may require reviewed normalization. Unexpected replacements fail closed. Cluster and namespace administrators remain trusted; this does not protect against an administrator forging infrastructure authority.

See [admission installation](../../deploy/kubernetes/README.md), [Kubernetes API boundary](../../control-plane/backend/app/adapters/kubernetes_api.py) and [Job fencing](../../control-plane/backend/app/adapters/kubernetes_jobs.py).
