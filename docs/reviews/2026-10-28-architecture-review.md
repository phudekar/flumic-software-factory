# Architecture and onboarding review — 2026-10-28

## Scope and method

Reviewed the backend composition, project runtime, worker application/adapters,
frontend project views, launch/deployment configuration and documentation entry
points at main `481cb0b`. This is a source-based architectural review with a local
configuration reproduction, not exhaustive verification of every execution path
or a new production/Kubernetes qualification. No running workers, databases or
external repositories were changed.

## Findings in priority order

### 1. Profile inheritance loses parent task overrides

**Correctness issue, reproduced locally.** `worker/factory/configuration.py:221`
merges a child profile over its parent at the top level. A child supplying `tasks`
replaces the entire inherited task map. A parent with `plan_issue` prompt
`parent-prompt` and a `triage_issue` prompt `keep-triage`, followed by a child
changing only the planning model, loses both parent prompts. Global task prompts
then apply instead. This makes a model-only edit change instructions too.

Decide and document the compatibility contract, then merge task entries and fields
without weakening unknown-field validation. Add inheritance regression cases in
`tests/test_worker_config.py` before changing the compiler. This review leaves
runtime behavior unchanged.

### 2. Kubernetes setup needs a concrete deployment wiring recipe

**Onboarding gap.** `website/src/content/docs/operations/kubernetes.md` describes
`CONTROL_RUNTIME_TARGETS_FILE`, but `compose.control-plane.yaml` does not itself
forward that environment variable and mount a private operator file. Setting a
shell variable alone does not make the file visible inside the API/reconciler.

Add a tested private Compose override recipe covering the target file,
authentication material and paths for every consuming control service. Validate
with `docker-compose config` and the existing target qualification; do not imply
that application configuration alone qualifies a cluster. Keep this as a separate
operational change rather than inventing untested launch commands in onboarding.

### 3. Completion policy spans transport and background settlement

**Design opportunity.** `control-plane/backend/app/worker_pools.py:652` combines
worker authentication, executor proof, pinned-secret redaction, deadline state
and completion. `app/workflow_runs.py:112` separately converts broker results and
settles attempts.

Extract a completion module with distinct authenticated-worker and broker-proof
entry points, sharing sanitization and final-state derivation. Preserve the
proof distinctions: a worker cannot self-certify a container result. Retain forged
output, uncertain mutation, cancellation, stale lease, usage and redaction tests.

### 4. Hatchet transport composition is duplicated

**Design opportunity.** `app/main.py:185` and `app/dynamic_runtime.py:61` both load
the default project's factory revision and hardcoded `simple` profile to obtain
transport settings. Their refresh lifetimes differ.

Give operator transport resolution and client ownership one explicit interface.
Separate it from business-profile selection. Specify refresh/restart semantics
before extraction; do not silently rotate credentials during admitted work.
Preserve database-config refresh and fixed-handler composition tests.

### 5. Adapter selection repeats credential and capability knowledge

**Design opportunity.** `worker/factory/bootstrap.py:57,128` and
`worker/factory/workflows/actions.py:22,43,51` repeat factory selection and recognize
specific callable strings for credential purposes.

Introduce trusted installed adapter descriptors for construction, capabilities
and required credential purposes. Keep the approved registry and minimum-purpose
grants; an arbitrary configured callable must not gain arbitrary secrets. Test
custom adapter laziness and cross-project credential isolation.

### 6. Configuration compilation depends upward on an HTTP module

**Design opportunity.** `app/services.py:114` imports the project route module's
validator wrapper; `app/projects.py:317` delegates to the underlying validator.
`Configurations.validate/resolve` also write temporary YAML to compile in-memory
documents.

First depend directly on the shared validator. Then make document compilation the
core interface, with YAML loading as an adapter. Keep exact precedence, validation,
approved factories and snapshot digests stable. Use configuration/project/profile
regression tests rather than snapshot-only implementation tests.

### 7. Runtime providers should own client lifetime explicitly

**Design opportunity.** `app/runtime_router.py:34,78` repeats provider construction,
resource binding, effect guards and backend checks. Injection uses string keys;
`app/ports.py:41` discovers optional cleanup dynamically.

Use typed worker/job provider factories with explicit context-managed lifetimes.
Keep immutable target-pin checks in the router and authority/fencing in the
existing modules. Do not generalize the public target schema beyond supported
runtimes just to justify an abstraction. Preserve client-close, target authority,
namespace/UID ownership and delayed-effect tests.

### 8. Reconciliation loops have inconsistent operational policy

**Design opportunity.** `app/dynamic_runtime.py:42`, `trigger_runtime.py:521` and
`reconciler.py:211` repeat loop/cadence/error handling. Background settlement also
uses fixed batches in `workflow_runs.py`.

A small runner can standardize safe diagnostics, bounded intervals, backoff and
shutdown. Keep transactions and leases inside each operation. Expose only trusted
operator tuning with safe bounds; project requests must not alter authority
limits. Test one failed operation not starving other work and prompt shutdown.

### 9. GitHub JSON responses lack GitLab's size bound

**Robustness gap.** `worker/github_client.py:752` reads provider JSON without a
byte limit. The GitLab adapter already limits response reads. Large responses can
consume memory before business validation.

Add bounded JSON reads and sanitized oversize errors with tests at/above the
limit. Make any operator setting bounded; do not use a project-controlled value
to disable resource protection. This is a separate behavior change.

### 10. Coding adapter construction has surprising lifetime

**Design opportunity.** `worker/factory/application/service.py:409,791` creates an
adapter for capability discovery; generation creates another at `:943`.

Construct one per operation and use it for both context preparation and generation.
Custom factories may allocate clients/processes or return changing capabilities.
Test factory invocation count and capability consistency as well as archive vs
selected-file context handling.

### 11. Managed bootstrap masks configuration failures as outages

**Diagnostic opportunity.** `worker/factory/configuration.py:270` catches transport,
decoding and validation failures together. Invalid saved settings can look like a
retryable availability problem.

Return sanitized failure categories while retaining fail-closed startup. Never
include raw snapshots, credential values or arbitrary exception text in logs.
Test invalid profile vs unavailable transport diagnostics separately.

### 12. Frontend project setup has too many responsibilities in one module

**Design opportunity.** `control-plane/frontend/src/features/projects.tsx` owns
project selection/routing, repositories, credentials, configuration and pools.
Project run views are also imported through a legacy re-export.

Split the existing project shell from setup modules and import project execution
views directly. Group navigation by project execution/setup, global infrastructure
and legacy factory. Preserve project-scoped query invalidation, secret handling,
route tests and forms. Avoid introducing a generic form framework prematurely.

## Documentation improvements included with this review

- A first-class project onboarding path alongside the explicitly labeled legacy
  quick start, with handover and first-run diagnostics.
- A module map connecting source, responsibility, configuration owner and
  diagnostic surface.
- Project/pool/target concepts and configuration effective-time/precedence rules.
- Corrected introduction/landing copy for PostgreSQL project execution and native
  runtimes; updated screen navigation.
- Existing editor, run and stage-attempt screenshots at task checkpoints, labeled
  as retained examples. No new screenshots or live qualification are claimed.

## Suggested implementation order

1. Fix/test inheritance semantics and bounded provider responses as small changes.
2. Qualify the Compose target-mount recipe and bootstrap diagnostics.
3. Extract the configuration compiler and transport provider without changing
   public contracts.
4. Consolidate completion handling, adapter descriptors and runtime construction.
5. Split frontend setup modules and standardize reconciliation tuning.

Preserve PostgreSQL authority, immutable pins, namespace/UID fencing and
capture-before-cleanup. Those constraints account for necessary complexity.
Do not combine these changes into one sweeping rewrite. The accepted ADRs remain
applicable; this report proposes changes rather than accepting new decisions.
