# Kubernetes workers and one-time Jobs — issue #17

Status: approved by the user on 2026-10-27, including executable Kubernetes scratch, combined logs and unavailable output after a hard deadline. Implementation is pending the implementation-plan review.

## Intent and decisions already made

One Control Pane should manage shared workers and one-time container actions on Kubernetes as well as Docker. Projects, repository grants, immutable workflows, stage links and execution history keep their existing contracts. Issue #17 remains open until real-cluster qualification proves execution and recovery, not just manifest generation.

The user selected **native Kubernetes API adapters** and a **separate local k3d cluster**. They authorized creation of that cluster. They approved this detailed design on 2026-10-27. Existing deployed workers/control services are outside this implementation’s mutation scope.

The isolated development cluster is `flumic-issue17`, with one server and one agent. Its dedicated kubeconfig is outside the repository, its API is loopback-only, and commands always specify the kubeconfig/context. No current/default Kubernetes context is switched. The cluster is a development prerequisite, not evidence that Flumic Kubernetes support is implemented.

## Approaches

1. **Native adapters — selected.** Extend existing `WorkerRuntime` and `JobRuntime` boundaries with Kubernetes implementations. Reuse the transactional Control Pane reconciler and broker. This adds target routing and Kubernetes-specific resource/transport handling without a new controller product.
2. **Separate operator/custom resources — not selected.** A Kubernetes controller could own Flumic CRDs, but would add CRD versioning, another reconciliation authority and an additional installation/upgrade path. It is unnecessary for this version.

## Scope and non-goals

Deliver namespace-scoped worker Deployments, one-time Jobs, scoped credentials, registry/image settings, network policy, logs, limits, scheduling, cancellation and restart recovery. Preserve Docker behavior and permit Docker/Kubernetes workers to share project pools when their declared capabilities match.

Use only the shared-v1 project runtime on Kubernetes. Legacy workers that depend on shared SQLite/local execution files remain Docker-only. Kubernetes does not introduce direct database credentials into shared workers or action containers.

Not included: a new workflow engine, a separate operator, cloud-cluster provisioning, automatic node autoscaling/HPA, arbitrary kubeconfig upload from a project, multi-user tenant isolation, hostile cluster-administrator fencing, guaranteed exactly-once external side effects, or automatic migration of existing live workers. Helm/manifests describe installation; they do not replace the Control Pane's durable lifecycle authority.

## Current seams and changes required

- `app/ports.py` already separates persistent worker lifecycle from one-time jobs.
- `app/reconciler.py` persists lifecycle operations and performs runtime I/O outside database transactions.
- `app/job_broker.py` already owns job scope, immutable launch intent, grants, cancellation, deadlines and encrypted results. Its external reference and routing need explicit runtime target/UID persistence.
- `app/worker_runtime.py` supplies assignment-scoped state and claims through authenticated HTTP APIs backed by transactional state.
- Current API target discovery, worker creation and frontend controls hardcode `local-docker`. Replace those assumptions with operator-registered target records and target-aware routing.
- Docker input/result transport and shared-history spool use host mounts. Kubernetes must not mount the Control Pane host filesystem or rely on identical paths across nodes.

## Targets, routing and immutable authority

Keep `local-docker` as the compatibility default. Add one operator-configured Kubernetes target in v1, with separate configured worker/action namespaces, deployment identity and stable target ID. The target registry interface may later accept more clusters; this version need not implement arbitrary multi-cluster administration.

Cluster transport, namespaces, approved image digests/pull-secret references, storage class and scheduling/network policy are **deployment-owned infrastructure configuration**. Project/model/prompt/repository values remain database-owned. Expose nonsecret target capabilities/health to the UI, never kubeconfig contents, bearer tokens or raw API errors.

Support an explicitly named kubeconfig/context for local development and a dedicated in-cluster ServiceAccount for deployments. Never fall back to the ambient current context. Use TLS verification; no insecure-skip-TLS option in the product path.

Workers retain `target_id`. Lifecycle operations route through that immutable target. An assignment's actual worker target is pinned into its job intent/reference before Kubernetes I/O; later pool edits, worker edits or process configuration cannot redirect an existing job. Missing target fields in old stored Docker documents resolve to `local-docker`; new non-Docker data requires an explicit target. Unknown/mismatched/removed targets fail closed.

Keep schema-v1 workflow documents unchanged. The existing `executor: docker`, `docker@1` and `docker.run@1` names remain compatibility identifiers for the existing OCI action contract, not a promise that Docker Engine executes every placement. Worker/pool capability and selected target choose the backend. Document this naming explicitly; a future schema rename must be a separate versioned change.

## Persistent workers

Use one Deployment with `replicas: 1` per Control Pane worker identity, deterministic names, immutable deployment/target/worker labels and expected-spec fingerprints. Use Recreate rollout strategy so a configure/restart does not intentionally run two Pods using one worker token. Scaling means adding distinct worker instances to a shared pool; do not simply increase replicas behind one identity.

Implement create/configure/start/stop/restart/remove/logs and reconcile asynchronous observed states. `created`/stopped means a Deployment scaled to zero; start requests one replica. Distinguish intent accepted, Pod scheduled and worker actually registered/healthy. Surface Pending, ImagePullBackOff, CrashLoopBackOff, unschedulable and disconnected states without calling them healthy. Make API calls outside database locks and retain recoverable operations on uncertain effects.

Pin approved multi-architecture worker images by digest, validate shared protocol capability, and provide explicit imagePullSecrets references. Do not perform implicit Docker image builds on the Kubernetes controller host. Shared workers receive only Hatchet infrastructure transport and their scoped worker token; project source/model grants arrive through the existing assignment protocol.

Mount pricing/configuration through immutable ConfigMaps. Use a private, per-worker PVC for the already-redacted outbound history spool, with an operator-selected storage class and bounded storage. It is not business state or a shared execution-claim filesystem. Accepted state/history/usage remain in PostgreSQL. Pod restart recovers unacknowledged spool entries. Rescheduling availability and permanent volume/node loss depend on the selected storage backend: local-path on k3d is a development constraint, not highly available storage. Production documentation must require portable storage where recovery across permanent node failure is expected. PVC ownership and removal must be explicit; worker removal must not silently discard unacknowledged history.

## One-time action Jobs and transport

Use `batch/v1` Jobs, `restartPolicy: Never`, `backoffLimit: 0`, deterministic job identity and bounded active deadlines. Flumic owns stage retries; the Kubernetes controller must not create application retries intentionally. Kubernetes Jobs can still exhibit at-least-once behavior around controller/node failures, so callers must make external effects idempotent using the pinned execution/job ID.

Create the Job initially suspended. Persist/verify its UID and expected spec before creating owned immutable payload resources and unsuspending it. Use UID/resourceVersion tests for mutations and UID preconditions for deletion. An existing resource with another UID/spec/owner is a conflict, never something to adopt or delete based only on a friendly name/label. A lost create reply is recovered by an exact identity/spec read. Once execution has been observed, a missing Job is an uncertain external effect; do not recreate it automatically.

Preserve the existing `/flumic` input/context/credential/output contract without hostPath mounts or a requirement to install Python into arbitrary action images:

1. Split bounded input JSON into immutable ConfigMaps below the Kubernetes per-object size limit; context is a separate bounded payload. Record expected byte counts/digests and a fixed maximum shard count. Credentials use a separate immutable Secret mounted read-only only into the action container.
2. A trusted digest-pinned Python helper image supplies an init container that validates/reassembles payload into a private input volume. The original action image runs its original explicit argv. No shell is added around an arbitrary command.
3. A small ordinary collector container, from the same trusted helper image, mounts only output plus its private control directory. It remains available after the action container exits so bounded result/usage files can be retrieved through fixed `pods/exec` commands. It receives no provider, Control Pane or Kubernetes credentials; automatic ServiceAccount token mounting is disabled for the entire Job Pod.
4. Observe the **action container** termination/exit/OOM state independently of the collector. Collection uses safe fixed paths, no symlink following, exact output/usage size and schema bounds. A missing/unavailable collector is a collection failure, not success. No action-controlled executable or path is invoked in the collector.
5. Keep the collector alive until the broker commits the sanitized/encrypted capture **within the pinned hard deadline**. Cleanup then releases it and confirms resource termination/deletion. A lost collection reply or controller restart can reread the same output while the collector remains alive. Kubernetes Job completion may therefore lag action completion until capture is committed; expose that distinction honestly.

Kubernetes Pod logs normally combine stdout/stderr. v1 must label them as a bounded combined stream, rather than claim portable separation: return that stream in `stdout`, empty `stderr`, and expose target/log-stream metadata to the UI. Docker's split streams remain unchanged. Do not depend on alpha log-stream feature gates. Output/usage JSON semantics remain unchanged.

The hard deadline/cancellation takes priority over collector retention. If it terminates the collector before capture is committed, collection returns an explicit unavailable-result capture, best-effort bounded Pod logs and empty output; it must not throw/retry forever or reinterpret this as success. The canonical state is timed-out/cancelled. No result is accepted after its deadline, even if the action exited successfully earlier. Surviving uncaptured output across a hard deadline, permanent node loss or volume loss is not guaranteed; external side effects may already have happened. This portability limitation was explicitly included in the approved design.

Garbage collection/TTL must not remove uncaptured evidence before canonical outcome settlement; this restriction does not override the hard deadline. Use explicit broker-owned cleanup/tombstones. Secret/ConfigMap owners must match the verified Job UID; do not delete similarly named unrelated resources. During cancellation/deadline handling, confirm the action Pod is stopped before settling the canonical execution; an unavailable API means cleanup pending, never confirmed cancellation. Deadline callbacks and late Job creation must be reconciled against durable tombstones.

## Security, networking and resources

The operator bootstraps dedicated namespaces and least-privilege RBAC for worker lifecycle and job management. Action containers receive no cluster API token or Docker socket, hostPath, host networking, host PID/IPC, privileged mode or elevated Linux capabilities. The controller's `pods/exec` authority is restricted to the dedicated action namespace and used only for the fixed trusted collector protocol. Kubernetes admins remain trusted.

Default-deny action networking requires an enforcing CNI and verified policies. Map the legacy `bridge` choice to an operator-defined allowed-egress policy, not arbitrary host networking. Document Kubernetes NetworkPolicy/node-traffic limitations; do not claim a disabled network interface equivalent to Docker `network=none`. Admission must reject a target whose required isolation capability has not been configured/qualified. Never silently run a requested isolated stage with open networking.

Map CPU/memory to requests/limits and temporary writable data to bounded memory volumes. Include init/collector overhead in target capacity reporting. Kubernetes does not expose a portable per-container PID limit: require an operator-configured and qualified Pod PID ceiling for the target, document that helper processes share that ceiling, and reject unsupported requested PID values rather than ignore them. Declare the target's supported/enforced resource and network semantics explicitly. No broad cluster privileges should be introduced merely to pretend these constraints are discoverable from namespace-only APIs.

Docker currently mounts `/tmp` with `noexec`. Standard Kubernetes memory-backed `emptyDir` does not provide that per-volume mount option. **Approved portability difference:** Kubernetes scratch space is writable/executable, explicitly reported as a target capability difference; Docker keeps its existing `noexec` behavior. This remains a trusted-developer runtime, not an untrusted-code sandbox. If Docker-equivalent `noexec` is required, do not implement this proposal as-is: revise the target/storage/runtime prerequisites and qualify enforcement before admitting those actions. Never claim equivalent hardening or silently downgrade a policy that requires `noexec`.

Pin absolute job/stage deadlines at durable intent creation. Add the necessary deadline field to the internal launch contract, preserving old Docker defaults. Set Kubernetes activeDeadlineSeconds from the remaining authorized budget, and retain the broker's independent deadline/cancellation reconciliation. Never extend a job deadline on retry/restart.

## UI, configuration and observability

Replace static Docker labels with target selectors and real capabilities. Worker details show target, namespace, Deployment/Pod UID, image, desired/observed status, readiness, scheduling failures, bounded logs and lifecycle operation state. Job/stage detail shows backend, Job/Pod UID, action exit/OOM/deadline, capture/cleanup state and combined-log labeling.

Preserve project/run/stage trace correlation, conversations and token/cost accounting through the existing APIs. Document every new setting with examples, apply/restart behavior and failure modes; include setup/RBAC, registry, storage, connectivity, CNI and cleanup examples. Kubernetes status remains planned until the real release gate passes. Configuration changes alone must not advertise support.

## Verification and completion

TDD covers target routing/default compatibility, forged/foreign UID/spec refusal, suspension/Secret creation ordering, lost API responses, input shards and result bounds, symlink rejection, denied grants, cross-project history/state, collector crash/replay, scheduling/image failure, cancellation/late-create/deadline fencing, stale worker leases and cleanup preconditions.

Run worker, backend SQLite/PostgreSQL, frontend/build and website gates. Reuse existing Docker/Hatchet contracts to prove compatibility. Add explicit opt-in real Kubernetes qualification on the owned k3d cluster: distinct workers and Jobs on different nodes, project/repository isolation, secrets and network isolation checks, actual logs/results/usage, scheduling failure, cancellation/deadlines, controller restart/recovery and guarded resource cleanup. Never use an ambient context; pin the cluster/namespace identity and explicit write scope. No paid model calls or existing worker restarts.

Before real qualification, produce the concrete resource inventory and cleanup guard and obtain any required live-resource approval. A successful cluster bootstrap alone does not satisfy this gate. Follow the standing one-issue workflow: one final independent review, fix Important/Critical regressions, PR, all exact-head CI, merge, then close #17 with evidence. Do not start #18 automatically.

## Calls left to the builder

The choices above were initially implementation proposals and were approved with this design on 2026-10-27. The builder may settle internal module/file splits, Kubernetes Python SDK version compatible with the qualified API, bounded retry/backoff constants, deterministic resource-name encoding, helper packaging, storage-size defaults and individual UI arrangement. It must not weaken authority, silently ignore unsupported limits, add a separate operator or change workflow-authoring contracts to avoid transport work.

## Primary references

- https://kubernetes.io/docs/concepts/workloads/controllers/job/
- https://kubernetes.io/docs/concepts/services-networking/network-policies/
- https://kubernetes.io/docs/concepts/security/service-accounts/
- https://k3d.io/stable/usage/commands/k3d_cluster_create/

## Builder check and approval notes

A fresh read-only builder check raised two material questions: collector survival versus hard deadlines, and Docker's `/tmp noexec` versus Kubernetes scratch-volume behavior. This revision resolves the first with a hard-deadline-first rule and explicit unavailable results, and presents the second as an explicit proposal for the user's design approval. The user approved both clarifications on 2026-10-27. Combined log streams, target PID prerequisites and storage-backend recovery limits are also explicit portability differences above. No product code is implemented by this document.

Planning correction: the draft used `/factory` when referring to the existing file protocol. Source inspection shows `/flumic/input.json`, optional `/flumic/context.json`, `/flumic/credentials.json` and `/flumic/output/result.json` containing `output` and optional `usage`. The approved intent is to preserve that existing protocol; the corrected path does not introduce a new authoring contract.
