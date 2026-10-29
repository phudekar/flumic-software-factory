# Project workflow runtime API

The project runtime runs immutable workflow revisions on shared worker pools. A project can own several repository connections, workflow definitions and configuration revisions; workers remain deployment infrastructure and can serve multiple projects through explicit pool memberships. The project workflow UI provides definition editing, immutable revision history, explicit run submission and pinned stage/job inspection; see [the Control Pane workflow guide](control-plane-workflows.md#project-workflow-authoring-and-execution). These APIs require the existing Control Plane administrator session and project ownership checks; they are not a public multi-tenant authentication boundary.

## Deployment and activation

Use PostgreSQL for the shared runtime. Apply database migrations through the normal API startup, then run the `dynamic-runtime` service from `compose.control-plane.yaml` alongside `api` and `reconciler`. It registers the fixed control coordinator and durable wait handlers in Hatchet. Its background pump repairs missing dispatch intents, retries engine submissions, resumes accepted follow-ups and settles broker-confirmed jobs even when the original worker or parent has disappeared.

The control runtime receives database/encryption/Hatchet configuration and has no Docker socket. The reconciler owns Docker job execution. Shared workers receive scoped assignment credentials through the API. `CONTROL_RUNTIME_NAMESPACE` defaults to `flumic-dynamic-v1`; use the same value on the API and control runtime. It selects the registered control workflow names, not a project or repository. Restart the control runtime when changing saved default-factory Hatchet transport/TLS settings. Business settings always come from each run's database pin.

Existing shared pools keep their configured action allowlists. Reconfigure and recreate workers deliberately to advertise the installed builtin catalog or `docker.run@1`; starting the new service does not promote old workers. Configure project pool memberships and capabilities before launching. See [shared pools](control-plane.md#shared-worker-pools-and-assignment-authorization) and [workflow development](workflow-development.md).

## Configure and seed a project

Create repository connections, secret versions and pool memberships using the [Control Plane APIs](control-plane.md). Stop and drain legacy workers for a repository before switching its connection to dynamic mode. A conflicting live legacy owner makes this operation fail; do not bypass the drain check.

Examples use an administrator session cookie saved as `cookies.txt` and `CSRF` set to the `csrf` value returned by `/api/v1/auth/login`. Replace IDs with the values returned by your deployment. For state-changing requests, send the configured public origin (the examples use its default) to satisfy the session's origin checks.

```sh
CONTROL=http://localhost:3400
PROJECT=<project-id>
REPOSITORY=<repository-connection-id>
curl -b cookies.txt -H 'Origin: http://localhost:3400' -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' -d '{}' \
  "$CONTROL/api/v1/projects/$PROJECT/repositories/$REPOSITORY/dynamic-mode"
```

Seed create-only builtin templates with the project-owned **repository alias**, pool IDs and secret-version IDs:

```json
{
  "repository": "backend",
  "simple_pool": "<shared-simple-pool-id>",
  "developer_pool": "<shared-developer-pool-id>",
  "secrets": {"openai": "<openai-secret-version-id>"}
}
```

POST this body to `/api/v1/projects/{project_id}/workflow-definitions/seed-builtins`. Here `repository` is the connection alias (`backend`), not its UUID. The response contains immutable revision IDs for issue, feedback and verification workflows. Seeding is idempotent and does not overwrite an operator's edited workflow. Supply only the model providers needed by the selected configuration. Repository GitHub credentials come from its saved connection. To create custom graphs, use the [definition/revision and stage guide](workflow-development.md#data-defined-workflow-revisions); publishing rejects stale parents and unsupported executor policy.

## Launch, inspect and cancel

```sh
curl -b cookies.txt -H 'Origin: http://localhost:3400' -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: issue-42-attempt-1' \
  -d '{"revision_id":"<issue-revision-id>","input":{"issue_number":42},"bindings":{"<repository-connection-id>":{"issue":"<issue-revision-id>","feedback":"<feedback-revision-id>","verify":"<verification-revision-id>"}}}' \
  "$CONTROL/api/v1/projects/$PROJECT/workflow-runs"
```

Use input matching the first stage's contract. The builtin issue template takes an issue command; custom echo graphs can take arbitrary JSON objects. A `bindings` map selects pinned follow-up workflow revisions per repository connection. It is required for every repository used by a non-echo builtin, including the seeded templates; echo-only/custom Docker graphs may omit it:

```json
{"bindings":{"<repository-connection-id>":{
  "issue":"<issue-revision-id>",
  "feedback":"<feedback-revision-id>",
  "verify":"<verification-revision-id>"
}}}
```

A `202` response means the pinned invocation and dispatch intent were accepted. It does not mean a worker ran or a business operation completed. The same project-qualified idempotency key and identical request returns the original run; changed input/revision conflicts. The run freezes graph, repository/configuration revisions, follow-up targets and secret-version references. Later edits do not retarget it. Revocation can prevent further credential resolution; it never substitutes a newer secret.

| Operation | Endpoint under `/api/v1/projects/{project_id}` | Meaning |
| --- | --- | --- |
| List | `GET /workflow-runs` | Owned run receipts; use supported pagination query parameters. |
| Inspect | `GET /workflow-runs/{run_id}` | Canonical state, pinned revision IDs, attempt metadata, follow-up status and engine dispatch receipts. |
| Cancel | `POST /workflow-runs/{run_id}/cancel`, body `{}` | Persist cancellation intent and stop new scheduling. Repeat safely. |
| Reconcile | `POST /workflow-runs/{run_id}/reconcile`, body `{}` | Requeue ID-only control dispatch and pending waits owned by this source run, even when the source completed. Does not replay completed business effects. |
| Inspect job | `GET /jobs/{job_id}` | Project-owned broker receipt; accepted/running differs from completed. |

Engine IDs are diagnostic receipts; they cannot be used as an alternative project run lookup. Engine payloads contain only project/run/execution or wait IDs. Effective configuration, prompts, credentials and business output stay in the encrypted control-plane state.

After cancellation, new worker context, renewals, state/resource operations and follow-up submissions are rejected; terminal acknowledgements and owned job status remain available. An already-issued external call may still finish. Cancellation remains `cancelling` while an active action or job has uncertain external effects. Docker jobs settle through broker terminal proof. A builtin that never received context can be proven unstarted; one that received context remains fenced until its authentic completion or operator reconciliation. The echo fast path is marked started at claim. Existing accepted follow-up runs are separate invocations; cancelling their source does not recursively cancel already-launched runs.

For a lost builtin worker, first establish that its external operations have stopped and reconcile their effects. Then POST to `/workflow-runs/{run_id}/executions/{execution_id}/resolve`:

```json
{"state":"failed","confirmation":"external-effects-reconciled"}
```

`state` may also be `cancelled`. This audited acknowledgement is not a kill command and is rejected for Docker jobs, which require broker reconciliation. Confirmed failure may admit a configured retry generation, so never use it while an earlier external operation may still run.

## Worker protocol and execution budget

Workers authenticate using their registration token and assignment lease proof, never an administrator cookie. Claim fixes the execution identity; context resolution checks the pinned project/run/assignment, role and grant versions. Workers cannot submit alternate project IDs, settings or credentials as authority.

| Assignment endpoint under `/api/v1/shared-worker/assignments/{assignment_id}` | Purpose |
| --- | --- |
| `/context` | Deliver the pinned builtin context, minimal credentials, origin metadata and deadline; records business start before returning. |
| `/renew` | Renew the current lease while eligibility and deadline permit. |
| `/state` | Project/repository-scoped `get`, `put` or `delete`; payload includes lease proof and key. |
| `/resource` | Claim an allowed canonical external resource; conflicts return `409`. |
| `/followups` | Accept a typed, server-recomputed follow-up against pinned targets. `accepted: true` means durable intent. |
| `/jobs` | Submit the stored Docker spec digest; the broker derives its authority from the assignment. |
| `/jobs/{job_id}/status` | Read that assignment's broker receipt with its original proof, including terminal status. |
| `/events` | Deliver bounded history with the original assignment proof; server derives scope and validates stable event/call identities. |
| `/complete` | Report a typed builtin result. Only the fixed read-only `issue.load@1` policy may report `failed: true` after an exception. Docker results require broker proof instead. |

Leases last 120 seconds and clients renew every 20 seconds. Stage `timeout_seconds` begins at first context delivery (at claim for echo), using the frozen stage value. After the deadline, new credentials/state operations/renewals stop and a late builtin completion is recorded as failed. This is cooperative fencing: an already-running external call may still finish. The generic Hatchet action handler has a 3660-second outer budget covering the maximum 3600-second stage budget plus acknowledgement time. Expiry alone never proves an external action stopped or permits duplicate execution. The installed `issue.load@1` action is explicitly read-only: its authenticated exception receipt safely marks the attempt failed and permits configured logical retries. Errors from potentially mutating actions retain the fence for reconciliation. Provider exception text is omitted from failure receipts.

Follow-ups are encrypted, deduplicated and processed after their source attempt becomes terminal. Waits resolve only the pinned GitHub grant and poll using the frozen settings; no model client is created. Source cancellation prevents pending follow-ups and wait resumption. Invalid pinned follow-ups become blocked without stopping independent intents. Transient engine outages leave pending dispatches for retry. If a GitHub outage exhausts a wait handler's engine retries, inspect the source run's `followups` receipts and POST its `/reconcile` endpoint to requeue the same stored wait ID and pins. No new workflow definition or business attempt is created. Inspect persisted state rather than interpreting engine acceptance as completion.

See [Docker action contracts](docker-action-jobs.md), [configuration effects](configuration-control-plane.md) and [architecture](architecture.md) for executor limits and deployment responsibilities.

For attribution, capture policy, scoped history/usage filters and durable delivery, see [project observability](project-observability.md).

## GitLab repository triggers

GitLab.com trigger support passed the disposable-project qualification for issue #16. See [GitLab setup and qualification](gitlab.md) for supported behavior and limits; self-managed GitLab remains planned. A trigger belongs to one project and one validated repository connection; its saved workflow definition selects the graph to run. Webhook content cannot choose another project, repository or workflow.

The owned `/api/v1/projects/{project_id}/repository-triggers` endpoints list and create bindings. Creation accepts:

```json
{
  "repository_id": "<validated-gitlab-connection-id>",
  "definition_id": "<workflow-definition-id>",
  "event_kind": "issue-ready",
  "enabled": true,
  "configuration": {
    "require_ready_label": true,
    "input_defaults": {},
    "followups": {
      "<repository-connection-id>": {
        "issue": "<issue-definition-id>",
        "feedback": "<feedback-definition-id>",
        "verify": "<verification-definition-id>"
      }
    }
  }
}
```

| Field | Effect |
| --- | --- |
| `repository_id` | Immutable connection UUID. Its verified GitLab numeric project ID is the source authority. |
| `definition_id` | Owned definition UUID, whose default repository must match the binding. Acceptance resolves and pins its current revision. |
| `event_kind` | Immutable `issue-ready`, `feedback`, or `verify`. Use separate bindings for each workflow. |
| `enabled` | Disabling stops new acceptance and polling; it does not cancel accepted runs. |
| `require_ready_label` | Defaults to `true`. Issue readiness requires the repository's configured ready label. Setting `false` permits open issues without that label; existing plan-created and PR-open labels still exclude them. |
| `input_defaults` | Up to the configuration's 64 KiB total limit. Fresh issue/MR identity fields override these operator defaults. Keep credentials in scoped secret versions. |
| `followups` | Definition UUIDs, rather than revision UUIDs used by manual launch. Each supplied repository requires all three roles. Acceptance freezes their current revisions into the run bundle. Required for builtin workflows that create follow-ups. |

GET `/{identifier}` returns public configuration and token-version identity. PUT requires `parent_id` equal to the current configuration `revision_id`; a stale edit returns `409`. The repository and event kind cannot be changed. POST `/{identifier}/rotate-token` and `/revoke-token` require `parent_id` equal to the current `token_version_id` (`null` after revocation). Creation and rotation return `webhook_token` once. Save it directly in GitLab's webhook secret-token field; list/detail/history responses never return its plaintext. Rotation immediately invalidates the old token. Revocation stops webhook authentication; disable the binding to stop polling as well.

GitLab sends POST `/api/v1/hooks/gitlab/{binding_id}` with `X-Gitlab-Token` and, when available, `webhook-id` (or legacy `Idempotency-Key`). Without a stable message ID, acceptance uses a content digest. `X-Gitlab-Event-UUID` is correlation metadata and can be shared by recursive events, so it is not used as a unique delivery ID. See [GitLab webhook headers](https://docs.gitlab.com/user/project/integrations/webhooks/). This endpoint uses the binding's token instead of administrator cookies or CSRF. The body limit is 1 MiB. Duplicate delivery IDs return their original durable receipt; reuse with different content returns `409`. An accepted receipt records a pinned run, not completed execution. GET the owned `/{identifier}/receipts` endpoint for the most recent 100 receipts.

The control runtime polls one leased binding at a time, at most five listed and five previously tracked resources per scan, with a 30-second interval per binding. It uses only that connection's scoped GitLab credential. Source reads have a five-second request timeout and a two-page limit; incomplete reads fail closed and retry later. Each scan has its own receipt identity so timestamp omissions cannot prevent a fresh tracked read. Receipts accumulate in the database; there is no automatic retention policy yet.

Ready issues run once per observed not-ready → ready transition. Routine comments do not rearm them. Feedback uses increasing human note IDs, retains pending feedback during an active run, and ignores system notes. Editing an existing note does not create a new note ID; add a new comment or launch manually. Verification requires a merged MR with a merge commit, and queues the verification workflow; its checks still determine success. Closed-unmerged changes never imply success. Fork merge requests are excluded. Polling cannot reconstruct a readiness toggle that occurs entirely between observations. Retry a `409` caused by an in-flight configuration/source-observation change; fresh acceptance rechecks current state and credentials.


## Kubernetes runtime configuration

Native Kubernetes worker Deployments and one-time Jobs are implemented and qualified on a dedicated two-node k3d/K3s cluster. See the [operator configuration reference](kubernetes.md) for every target setting, explicit controller authentication, namespace fencing, storage, networking, PID limits and capture/cleanup behavior. Docker remains available alongside the operator-configured Kubernetes target.
