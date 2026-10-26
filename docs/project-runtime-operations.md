# Project runtime rollout and release qualification

Flumic AI supports multiple projects, multiple repositories per project,
versioned data-defined workflows, shared simple/developer worker pools and
one-time Docker actions. The Control Pane publishes and inspects this runtime;
legacy static workflows remain separate and retain their existing IDs/history.
GitLab, Kubernetes and remote function execution remain planned.

## Choose the configuration owner

| Owner | Values and effects | When changes apply |
| --- | --- | --- |
| Deployment | PostgreSQL/encryption/session settings, Hatchet transport, runtime namespace, Docker broker host/network, infrastructure telemetry | Recreate the affected control service deliberately. Keep the encryption key with database backups. |
| Worker pool | Role, installed action/executor allowlists, normal/durable slots, image/startup transport | Rebuild/recreate affected shared workers deliberately; a database capability string cannot install code. |
| Project | Default models/prompts, behavior profiles, repository connections, encrypted secret versions, pool membership | New runs resolve and pin saved revisions; active runs retain their launch pins. |
| Repository connection | Stable alias, GitHub owner/name, credential reference and behavior overrides | Publish another repository revision. IDs and aliases are project-owned. |
| Workflow revision | Stages/dependencies, default and per-stage repositories, action/executor versions, inputs, conditions, secrets, retries/timeouts and behavior overrides | Immutable publication. Change a graph by publishing another revision. |
| Run / attempt | Explicit input/follow-up bindings, pinned graph/config/repositories/grants, execution identity and state | Recovery reuses the original pin. A new logical attempt has a new identity; lease expiry alone is not terminal proof. |

Business behavior merges project defaults → repository overrides → workflow
values → stage values, subject to deployment limits. A stage repository override
must name another declared alias in the same project. Generic Docker/echo stages
can relay explicit outputs across those aliases; a repository-bound builtin
operation cannot accidentally consume another repository's typed state.

See the [field reference](configuration-control-plane.md),
[workflow authoring guide](control-plane-workflows.md#project-workflow-authoring-and-execution),
[action schemas/bindings](workflow-development.md#data-defined-workflow-revisions)
and [history/accounting contract](project-observability.md).

## Roll out from a current checkout

1. Back up PostgreSQL, the private `.env.control-plane` encryption key/settings,
   and the Control Plane's private snapshot/spool storage. A database backup without
   its original encryption key cannot recover encrypted definitions or grants.
   Rehearse migration on a disposable restore when upgrading a populated install.
2. Build from the current merged repository source. Do not build an old checkout
   against a database already upgraded by a newer image. Existing worker images
   do not acquire new action implementations when API code changes.
3. Start the control services explicitly:

   ```sh
   ./scripts/start-control-plane.sh api reconciler dynamic-runtime
   ```

   The API applies migrations, the reconciler manages worker/container operations
   and job cleanup, and `dynamic-runtime` registers the fixed Hatchet control/wait
   handlers and repairs dispatch intents. This command targets these control
   services; it does not restart existing user worker containers. It also builds
   the repository worker images for later use. Existing workers retain their code
   and configuration until an explicit worker lifecycle action.
4. Create a project and its repository aliases/credential versions. Authorize
   compatible simple and developer pools. Create shared workers and explicitly
   start them through the global worker lifecycle controls. Verify loaded action
   versions and runtime health; registered slot counts are not free/live capacity.
5. Before handing an existing repository to dynamic workflows, pause its legacy
   trigger sources, let active work settle, and explicitly stop the relevant legacy
   workers. Inspect uncertain external effects and queued lifecycle commands. Then
   POST `{}` to
   `/api/v1/projects/{project}/repositories/{repository}/dynamic-mode`.
   The claim-aware check refuses conflicting live legacy owners/operations; it is
   not a request to automatically stop them. Legacy start/restart for that
   repository is fenced once dynamic mode is enabled.
6. Publish and run a small deterministic graph first. Confirm accepted → dispatched
   → stage attempts → broker results → canonical terminal state. A queued receipt
   does not prove that a control handler, compatible worker or job started.

Migrations attach existing profiles/revisions/events/conversations/control
receipts to the stable default project without renaming IDs or restarting
workers. Legacy repository connections are not guessed from every historical
profile. Their SQLite state and file claims are retained; entering dynamic mode
is not an automatic business-state copy or permission to run both execution paths
against the same external issue.

## Docker action protocol

Use a digest-pinned image, an argv command and an explicitly authorized
`docker.run@1` pool. The worker requests a durable job intent; it receives no Docker
socket. The trusted broker launches the container with the documented non-root,
read-only-root, resource/network and mount policy.

| File | Direction | Contract |
| --- | --- | --- |
| `/flumic/input.json` | Read-only input | Resolved bounded JSON business inputs. Keep platform identity in the context file. |
| `/flumic/context.json` | Read-only context | Optional on older pinned jobs; new jobs receive authoritative project/run/stage/execution/repository/worker and trace metadata. |
| `/flumic/credentials.json` | Read-only secret grants | Only explicitly declared supported purposes, resolved from pinned versions. Never print or copy values into outputs. |
| `/flumic/output/result.json` | Writable result | Bounded JSON with `output` and optional supported `usage` records. Write a complete JSON result; partial/invalid files fail validation. |

Example result:

```json
{
  "output": {"checks_passed": true},
  "usage": [{
    "id": "call-1", "provider": "example", "model": "my-model",
    "input_tokens": 120, "output_tokens": 30,
    "reported_cost_usd": 0.002
  }]
}
```

Only report usage the action actually observed. Omit unknown fields instead of
inventing zeros. Estimated and reported values remain separate, and a stable
usage ID deduplicates settlement/recovery. Stdout/stderr and CPU/memory do not
imply model spend. Capture is bounded/redacted, not an invoice or a hostile-code
sandbox. See [one-time Docker actions](docker-action-jobs.md) for exact bounds,
network policy, cancellation and image-author requirements.

## Recover by evidence, not by blind resubmission

- **API/control process disappears:** canonical pins, attempts, dispatch intents and
  claims remain in PostgreSQL. Restart the matching current services. The pump
  repairs accepted-but-not-enqueued windows and retries unavailable dispatch.
- **Worker disappears after job acceptance:** the broker/controller can still
  observe, cancel/time out, capture and settle that job. Terminal job capture is
  committed before cleanup. Restarting a broker reattaches its deterministic job
  identity rather than launching another known-completed effect.
- **Worker disappears during a builtin external mutation:** an expired lease does
  not prove that the mutation stopped. An uncertain attempt can retain capacity
  and claims until an operator reconciles external effects. Do not repeatedly
  trigger a new workflow to hide that uncertainty.
- **Credential rotated or revoked:** rotation does not replace pinned versions.
  Revocation can prevent future delivery/launch. Historical proof-authenticated
  history can still be accepted and redacted against the original grants; it does
  not renew execution access. Never restore another project's grant to unblock work.
- **Cancellation/deadline:** request intent first, then inspect canonical run/job
  terminal state. Engine parent cancellation and a sync child stopping are
  different facts. Builtin time budgets are cooperative; Docker enforcement belongs
  to the broker and may require cleanup after a worker disappears.
- **Unknown effects reconciled manually:** use the project-owned operator resolve
  endpoint only after checking the actual external operation. Its explicit
  `external-effects-reconciled` confirmation is an operator assertion, not a
  detector. See the [runtime API recovery contract](project-runtime-api.md).

Keep the private assignment-history spool private; it contains original proof
material for bounded delayed replay. Full queues may evict old records. Treat
history and cost views as retained known subtotals and use canonical run state
for business recovery.

## Qualification matrix

The release gates use fake GitHub/model implementations and explicit synthetic
usage. They execute no paid provider call or GitHub write.

| Boundary | Executable evidence | What it establishes |
| --- | --- | --- |
| Two projects × two repositories, shared simple/developer roles, parallel branches, mixed builtin/Docker graph | `control-plane/backend/tests/test_project_release.py` and `tests/integration/test_project_release_contract.py` | HTTP ownership, pinned pre-edit config/grants, typed bindings, isolated business state, conversations and usage; the live opt-in path uses real PostgreSQL, Hatchet workers and Docker. |
| Coordinator/worker/broker restart | Combined live contract plus `tests/integration/test_hatchet_dynamic_contract.py` | Durable parent/child replay, accepted jobs settling after submitting-worker loss, no second container identity for a known job. |
| Crash windows, revocation, one-slot contention and duplicate accounting | Backend release fixture and existing state/job/history suites | Deterministic failure injection and admission/isolation invariants. The unit fixture models the completed-child cache established by the separate real-engine contract. |
| Cancellation and deadline without submitting worker | Combined live cancellation/deadline tests and Docker broker tests | Real container termination/capture and canonical settlement; intent is never treated as completed cleanup. |
| Populated legacy IDs/profile history/events and explicit takeover | `test_projects.test_upgrade_preserves_populated_legacy_records`, `test_workflow_state.test_populated_migration_roundtrip_preserves_legacy_and_pins`, claim/takeover API tests | SQLite/PostgreSQL migration preservation and claim-aware transition; no automatic user worker restart. Legacy graph serialization/control tests remain in the normal suites. |
| Schema errors, stale parents, project switching and unknown/zero costs | Backend definition/ownership tests and frontend workflow/project tests | Validation, captured mutation scope, fresh conflict reload and truthful UI state. |

The opt-in combined fixture registers uniquely named workflows/workers in the
selected test tenant, uses a private PostgreSQL schema and only its own job
containers/processes. Workers use the production bootstrap/assignment/observer
path with deterministic test-only external adapters. GitHub/model effects are
fake; this does not qualify provider billing, GitHub mutation semantics, hostile
process isolation, production load, every OS/network policy, or every accessibility
interaction. Synthetic accounting is labeled fixture data, not real model spend.

## Reproduce the gates

Normal CI runs worker/frontend/backend/site checks and the PostgreSQL ownership,
migration and concurrency suite. Real infrastructure gates are opt-in:

```sh
# Export the selected test tenant's HATCHET_* SDK settings privately first.
# Set CONTROL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test DB.
# Point Docker's SDK at the intended test daemon (DOCKER_HOST if needed).
# Use a locally available repo@sha256:... Python image, not a mutable tag.
RUN_DOCKER_JOB_CONTRACT=1 DOCKER_JOB_CONTRACT_IMAGE="$IMAGE" \
  .venv/bin/python -m unittest discover -s tests/integration -p test_docker_job_contract.py -v
FLUMIC_LIVE_HATCHET=1 FLUMIC_LIVE_EVIDENCE_DIR=output/runtime-evidence \
  .venv/bin/python -m unittest discover -s tests/integration -p test_hatchet_dynamic_contract.py -v
FLUMIC_LIVE_PROJECT_RELEASE=1 DOCKER_JOB_CONTRACT_IMAGE="$IMAGE" \
  FLUMIC_LIVE_EVIDENCE_DIR=output/runtime-evidence \
  .venv/bin/python -m unittest discover -s tests/integration -p test_project_release_contract.py -v
```

On GitHub Actions, manually dispatch **Tests** with `run_docker_contracts` and/or
`run_hatchet_contracts`. The latter requires repository secrets
`FLUMIC_RELEASE_HATCHET_TOKEN`, `FLUMIC_RELEASE_HATCHET_HOST_PORT` and
`FLUMIC_RELEASE_HATCHET_SERVER_URL` for a test-only tenant. Optional repository
variable `FLUMIC_RELEASE_HATCHET_TLS_STRATEGY` defaults to `tls`. These credentials
are not required or used by ordinary pushes/PRs. No provider/GitHub credentials
are forwarded to fixture workers. The live job has a bounded 30-minute timeout.

Record the final commit, dependency versions, commands, results and JSON protocol
summaries with the PR/issue evidence. Upload only the safe summary/effect metadata
allowlist. Do not upload grant files, worker spools, private fixture configuration
or raw SDK logs. Local failed runs can retain bounded redacted diagnostic logs in
a private evidence subdirectory. The fixture cleans only its own workers,
containers and database schema; registered test workflow metadata can remain in
the selected tenant for inspection.
