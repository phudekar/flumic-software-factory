# Flumic AI control plane

The first implementation is a separate React/TypeScript application with a
FastAPI backend, PostgreSQL, and a background lifecycle reconciler. Its compact
navigation, workflow tables, status indicators, and stage-detail views take
inspiration from [Hatchet's dashboard](https://hatchet.run/). The full target
architecture and phased acceptance gates remain in
[the architecture plan](control-plane-architecture.md).

## Start

```sh
./scripts/start-control-plane.sh
```

Open **http://localhost:3400**. Sign in using `CONTROL_ADMIN_PASSWORD` from the
private root `.env.control-plane` file. The launcher generates this file once
with mode 0600, including a random database password and encryption key. It is
ignored by Git and excluded from Docker build contexts. No default password is
shipped. Back up the database and encryption key together; losing the key makes
stored conversation content unreadable. Do not casually rotate the key: this
initial implementation also derives worker ingestion credentials from it.

The launcher loads the existing Hatchet `development` CLI profile, builds updated
worker images, and starts only the control-plane Compose project. It does not
restart existing factory workers. The database is internal; the web port is
bound to loopback. Both Hatchet and SigNoz remain independent services.

The local trusted backend and reconciler receive Docker socket access. They
restrict API actions to factory-owned worker resources, but the socket itself
is privileged host access. This release is for a trusted local operator, with a
single administrator session model. Native Kubernetes targets use explicit operator-owned credentials and scoped RBAC; multi-user roles and OIDC remain future work. Do not publish it as a
multi-user or Internet-facing service.

## Included in the first implementation

- React overview, workers, workflow runs/stages, logs, model history, usage,
  configuration, and task-prompt views. Data refreshes by bounded polling.
- Password authentication, server-side expiring sessions, HttpOnly/SameSite
  cookies, CSRF checks, origin checks, login throttling, safe errors, and an audit
  record for configuration/lifecycle changes and conversation access.
- Discovery of this factory's current Compose workers as read-only resources.
  Unrelated Docker resources are excluded.
- Creation of stopped worker instances from approved images and a chosen profile;
  explicit start/restart/stop/removal through durable, idempotent operations.
- Saved worker profiles with independent immutable revisions, cloning, prompts,
  startup scripts, optimistic edit conflicts, and shared factory settings.
  Linked workers resolve the latest profile on their next process start.
- Hatchet run/task status through an adapter that excludes input/output payloads.
  Startup logs come from Docker; task logs explicitly sent to Hatchet can be read
  for a selected stage. Instrumented workers additionally send correlated logs.
- Redacted model request/result capture for new control-plane workers, encrypted
  content storage, and token/cost accounting events. API requests include the
  assembled system messages. Coding CLIs provide submitted prompts and final
  results, not their full internal conversations.
- Separate runtime and workflow adapters. Docker is available by default; an operator can register one Kubernetes target for shared workers and native Jobs. **Runtime targets** shows safe capabilities; **Add shared worker** selects a pool and destination. See [Kubernetes configuration](kubernetes.md).

## Worker lifecycle and configuration

The Workers view separates managed instances from discovered Compose containers.
Choose **Create worker**, provide a unique name and profile, then create it.
Creation queues a command and leaves the runtime stopped. **Start** is separate
and can begin normal issue scanning, GitHub operations, and paid model calls.

**Stop** interrupts a managed worker after its runtime grace period; active tasks
may be retried by Hatchet. Graceful drain is disabled until pause/assignment-race
semantics are verified. Existing Compose workers cannot be stopped or adopted
through this first release. Manage those through their original deployment.

### Saved worker profiles

Open **Configuration → Worker profiles**. Define a new profile from an approved
worker type, or rename/edit a selected profile and **Clone as new profile**.
A clone has its own identity and revision history. Create workers by selecting
one of these saved profiles; multiple workers can share a profile.

The profile picker lists one entry per saved profile at its latest version.
Choose **Version history** to inspect older revisions. Historical versions are
read-only; use **Back to latest version** to edit, or clone the historical version
into a new profile. Existing revision URLs continue to open the requested version.

Fresh installations seed the commented [`default`](../config/worker-profiles/default.yaml)
and [`developer`](../config/worker-profiles/developer.yaml) profiles from the
repository. See the [bootstrap profile guide](../config/worker-profiles/README.md)
for inheritance, validation and use with an existing installation. Existing saved
profiles are never overwritten from these files.

A profile includes models, coding backend, model/patch limits, API retries,
logging, capture policy, task model overrides, additional system prompts and a
startup script. The **Worker prompts** view edits prompts for the selected
profile. Built-in instructions and output schemas remain supplied by the image.
Editing an existing profile appends a revision to the same profile identity.
All linked workers pick it up on their next start/restart; running processes keep
their loaded settings. Cloning affects only workers created or reassigned to the
clone. Stale concurrent edits return a conflict instead of overwriting changes.

The Workers view links names to stage-attempt history and worker-specific usage.
The restart button stops and recreates the runtime with the latest linked
profile, then starts it. It can interrupt active work. A stopped worker's profile
action can assign a different saved profile of the same worker type. Starting a
stopped worker automatically prepares the latest profile revision.

New worker images also fetch the latest profile at **every process startup**,
including direct Docker restarts, using their own scoped worker token. Managed
startup fails if the control plane is unavailable rather than silently running
an old profile. Standalone YAML workers do not use this integration. Existing
containers built before this update need one control-plane restart/recreation to
receive the new bootstrap code; this migration does not restart them automatically.

### Factory settings and migration

**Configuration → Factory settings** owns repository/policy, state, orchestration,
labels, scan cron, task routing, engine timeouts/retries and concurrency. These
register shared Hatchet definitions and are deliberately separate from local
model/API retry behavior. Worker profiles pin a factory revision. To adopt shared
changes, select the new factory revision in each participating profile and save;
then restart its workers with matching shared definitions. Coordinated workflow
registration migration and rolling deployment/drain are not implemented.

Workflow listing, input examples and manual-trigger repository validation read the
latest saved factory revision from the database on each request. Repository and
Hatchet TLS changes take effect in the control-plane adapter without restarting
the API. The bootstrap YAML is not reloaded for these operations. Running workers
still use their pinned configuration until their profiles are updated and they
restart, as described above.

The initial YAML configuration is imported into a factory revision. Existing
managed workers are linked to imported profiles with matching settings, preserving
their exact existing snapshot IDs and runtime state. Workers with identical source
revision, worker type and settings share an imported profile. Compiled worker
snapshots are excluded from factory revision history. Edits do not change files
in Git. Legacy full-configuration revision/export API routes remain for
compatibility; the UI uses the scoped factory/profile APIs.

Each worker runtime mounts a read-only snapshot under an instance/revision path.
Process startup resolves its current saved profile without modifying that mounted
snapshot; telemetry records the actual loaded configuration revision. Container
labels describe the creation snapshot and can differ after a direct Docker
restart. Host mounts and images remain server-controlled. Approved images must
carry control-protocol version 3 for configurable file limits and error capture.
Older containers need a restart through the control plane to receive the updated
image; a direct Docker restart retains the older code.

### Startup scripts

A trusted administrator can supply arbitrary POSIX shell commands in a profile:

```yaml
settings:
  startup_script: |
    python -m pip install --no-cache-dir -r /data/requirements.txt
  startup_timeout_seconds: 600
```

The script runs via `/bin/sh -eu` before task registration on **every process
start**. Keep it idempotent and pin dependency versions in the requirements file.
It runs with the container user's permissions (root in the current images),
network, environment and existing data mounts, so it can install prerequisites.
It cannot add host mounts or select another image through the profile. Do not put
access credentials in script text; use the already-injected environment. Shell
exports apply to the script subprocess, not to the subsequent Python worker.

The script is limited to 32 KiB and its timeout to 1–3600 seconds (default 600).
A nonzero exit or timeout aborts worker startup; the container's restart policy
may retry. Background script processes are terminated before the worker starts.
Up to 64 KiB of redacted output and the result are available in runtime logs after
completion. Startup hooks are administrator-supplied code; model-generated task
content is never executed as a startup hook. For standalone YAML workers, place
these fields in the profile's mapping rather than inside `settings`.

## Conversation capture and accounting

Capture defaults to `conversation_capture: redacted`. A worker profile/default
can choose `metadata_only` or `disabled`. Metadata-only suppresses conversation
bodies. Disabled suppresses model-call history capture; normal stage observability
continues. Existing workers are not instrumented retroactively.

Redaction happens before the local disk spool and again on ingestion. It removes
known environment credential values, recognizable secret patterns, authorization
headers, and private keys. This is best effort: ordinary repository content can
still be sensitive and is visible to the trusted administrator. Messages are
UTF-8 bounded at 64 KiB. Oversized interactions omit content and mark a size limit.
The spool is bounded to 500 already-redacted event files per worker. Delivery is
best effort with retries; overflow/outages can leave gaps and this is not an
invoice ledger. The spool lives under `/data/control-spool/<instance>` and must
be protected along with other factory data.

Ingestion tokens are scoped to one managed worker identity. Workers never receive
the database encryption key or direct database credentials. Repeated event IDs
are deduplicated; request/response content is encrypted separately from event
metadata. Content expires after seven days, execution metadata after 90 days,
and operator audit records after 180 days. Cleanup runs in the reconciler. Backup
retention is separate, and expired data may remain in older backups.

Model history distinguishes API exchange versus CLI prompt/result coverage.
Provider-internal reasoning and unsupported CLI tool transcripts are not captured.
Unknown usage/prices remain unknown. Claude outer accounting is excluded when
its per-model records are available, avoiding double counting. Estimated and
CLI-reported USD are shown separately. Prices used for recorded estimates are
included in call metadata. Luna aggregates above 272K input tokens remain unpriced
because the applicable per-request tier cannot be established. Smaller estimates
may still omit unreported cache writes, tool fees, or pricing-mode differences.

The run view has a stage filter and a **View logs** link on each stage card,
activity entry and worker stage-attempt row. Links open the logs tab with the
stage (and, where applicable, worker and retry) already selected. The tab and
filters are encoded in the URL, so reloads and shared links retain the view.
Changing or clearing the stage keeps the logs tab open.

Stage selection scopes event history, model calls and cost using engine task
IDs; an attempt selection further isolates a retry. Worker detail links preserve
worker/task/attempt context. Log tables separate timestamp, severity, entry type,
stage and worker. Model exchanges expand into distinct redacted input/output
blocks on demand (with audited content reads). New captures include request and
response timestamps; older captures show them as unavailable. Hatchet task logs
are hidden when filtering to an attempt because their retry identity is not
available. Worker attempt lists use the latest 500 matching attempt events;
other event views use up to 500 matching events and indicate a full page.

The Usage page reports a known subtotal for up to the latest 10,000 retained
accounting events; it displays missing-usage and unpriced counts and marks a
truncated query. It is not a billing reconciliation or budget enforcement system.

## Current limits and next milestones

This is the first integrated local release, not completion of every item in the
architecture plan. Remaining work includes verified drain, explicit adoption,
automatic coordinated rollouts, a source-fragment YAML editor and richer diff
review, role-based multi-user authorization, SSE, direct SigNoz log querying,
more scalable paginated cost rollups.

Stage logs currently combine the instrumented worker event stream with Hatchet
logs when available. SigNoz continues receiving standard OpenTelemetry signals,
but its query adapter is not yet implemented in the control plane. The run view
shows stage cards, not a reconstructed DAG with full dependency edges. Every
adapter returns unavailable/unknown states instead of fabricated monitoring data.

Project workflows use PostgreSQL execution state and fenced database claims, allowing shared workers on different Kubernetes nodes. Each worker keeps its own retained history PVC; local-path volumes are node-bound and do not provide node-loss HA. Legacy static workers retain their single-host SQLite/file-lock adapter. See [runtime configuration and qualification](kubernetes.md).

## Development and verification

```sh
.venv/bin/pip install -r control-plane/backend/requirements.lock
cd control-plane/frontend
npm ci
npm run build
npm test
```

From the repository root:

```sh
PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -q
.venv/bin/python -m unittest discover -s tests -q
ruff check control-plane/backend worker tests
```

The backend test suite uses SQLite by default. Set `CONTROL_TEST_DATABASE_URL` to
a dedicated PostgreSQL test connection to exercise the same tests against
PostgreSQL; each test creates and drops a uniquely named schema. The database
user must have schema creation permission. Never use test fixtures to start
normal factory workers or make paid provider calls.

Production schema changes run with Alembic before API startup. No production
`create_all` runs on requests. The reconciler is a separate process and claims
commands with row locks and skip-locked semantics. After a bounded stale lease,
an interrupted operation can resume; Docker create is idempotent by ownership ID.


## Error visibility and log formats

Use **Parsed / Raw** to switch stage logs or worker runtime output. Stage raw
mode shows the same redacted, filtered API records as parsed mode, and its URL
parameter survives reload. Structured JSON inside an engine log message is parsed
into message, severity and stage columns; its embedded severity takes precedence
over the transport envelope. Plain or malformed messages are preserved as text.
Raw mode retains the original redacted record. Runtime raw mode preserves text and JSON lines;
parsed mode preserves plain traceback lines as messages. Raw does not bypass
redaction or decrypt conversations automatically.

Failed attempts and failed model calls include `error_type` and a bounded,
redacted `error_message`. Known factory/validation/database error messages are
included. Arbitrary SDK exception bodies remain excluded; model request failures
preserve exception class and HTTP status without provider response bodies.
Stage failures appear at ERROR level, including historical failed-attempt records
whose message is missing (clearly labelled as not captured). The workflow adapter
also exposes Hatchet's redacted latest task error. These engine errors appear
when viewing all retries, and are excluded from individual retry filters because
Hatchet's latest error may belong to another attempt.

Startup/bootstrap failures happen before stage registration and remain available
in the worker's runtime logs. Control-plane outages can prevent ingestion; events
missing from historical records cannot be reconstructed by changing the viewer.
See [the error-path audit](telemetry-error-audit.md) for covered boundaries.

## Worker images from a base OS

Worker profiles can define `base_image`, `system_packages`, `image_setup_script`,
`codex_version`, and `claude_version`. Use **Configuration → Worker image build** to paste or edit a single YAML mapping:

```yaml
base_image: python:3.12-slim
system_packages:
  - git
  - curl
codex_version: '0.163.0'
claude_version: '2.1.306'
image_setup_script: |
  python -m pip install extra-library
```

The editor validates YAML structure, duplicate keys, supported fields and value
types before saving or cloning. Omitted fields reset to the editor defaults.
Existing settings are loaded into the editor; saves preserve all unrelated
worker settings and prompts. Parsed values are versioned with the profile;
comments and YAML formatting are not persisted after reload.
Debian/Ubuntu and Alpine are supported; choose a base containing `/bin/sh` and a
working package manager. For CLI workers, `node:24-bookworm-slim` supplies Node
and npm; API workers can use `python:3.12-slim`.

Create assembles the worker runtime and creates a stopped container. Identical
build inputs reuse the resulting image. View **Build logs** on the Workers table
or open the worker detail page for live polling, including failed builds that
never created a container. Changes to build settings take effect on the next
control-plane Start/Restart that replaces the runtime. A plain Docker restart
cannot replace an image. Startup scripts remain separate and run on every start.

Profiles with an empty base image retain legacy prebuilt-image behavior. Existing
saved profiles are not changed by editing bootstrap YAML. To opt in, save a profile
revision with a base image. If creation fails, correct the profile and apply it
using the worker's **Change profile** action, then start the resulting container.

Build scripts run as root during image assembly and must never contain secrets.
Runtime integration credentials are injected only into the created container.
Build logs are bounded/redacted. The first build can take several minutes and
requires registry/package-network access. See [the architecture](worker-image-builds.md)
for cache, context and lifecycle details.

## YAML editors and validation

**Worker behavior** is edited as YAML with `settings` and `tasks` mappings. The
image build section uses its own YAML mapping for the five build fields. Both
editors occupy the available panel width and highlight YAML syntax. They offer
**Validate** and **Format YAML**. Validate checks YAML syntax and
structure, then checks the complete profile against the pinned factory revision
using the same server rules as saving. It does not publish a revision or change
workers. Invalid input disables Save and Clone. Server validation errors appear
beside the relevant editor; edit the YAML to correct them.

Format YAML normalizes indentation while preserving comments and multiline
values in the current draft. It does not save or change workers. Historical
versions remain read-only, with formatting disabled. Saved profiles retain parsed
values, so original YAML comments and formatting are not restored on reload.
Shared factory configuration remains in its existing editor.

## Removing workers

Stop a running managed worker before choosing **Remove stopped runtime**.
Removal preserves execution history but hides the removed instance from the
active Workers list and overview count. Enable **Show removed workers** to inspect
retained history. Removed instances cannot be restarted; create a new worker.
Removal is safe to retry when the container is already absent. Failed instances
that never created a container can also be removed once their operation finishes.
Discovered Compose workers are read-only and must be removed through Compose;
they are distinct from managed instances. An `exited` Compose container is stopped.

Exited discovered Compose workers are hidden by default. Enable **Show exited
discovered workers** to inspect their stopped containers and logs. Running
discovered workers remain visible. This filter does not remove containers.

## Markdown worker prompts

The worker prompt input spans the panel width, uses a larger resizable monospace
editor, and shows a character count against the 16,000-character limit. Use
**Write** for Markdown source and **Preview** for headings, lists, emphasis and
code blocks. Switching views preserves the prompt text. Preview does not execute
HTML or load remote images. Saving preserves the original Markdown string;
workers receive that text as additional system guidance on their next restart.
Historical versions remain read-only.

## Workflow controls

Open **Workflows** to trigger, pause or resume registered factory workflows.
**Trigger run** accepts YAML input; replace required null placeholders before
submitting. Inputs are validated against the existing workflow models and factory
repository. Triggering a workflow requires a compatible worker to execute it.

**Pause** prevents new runs from starting while current runs continue. Cron and
scheduled triggers default to **Skip missed triggers**; choose **Queue for resume**
when missed work should be retained. Queue TTL defaults to 24 hours (maximum 30
days). Manual/event runs always queue during pause. **Resume** may release queued
runs, including model calls and GitHub updates. These controls affect the whole
workflow definition, not worker containers or a single task. Unsupported/unknown
native pause capability disables the pause control.

**Action history** shows local control receipts and links to submitted runs. A
completed trigger receipt means the run was submitted, not that it finished.
Network retries reuse the same idempotency key. An uncertain outcome is never
automatically retried: inspect action history and workflow runs before issuing a
new request. Operation IDs are attached to triggered runs as metadata for
correlation. Audit records contain identities and outcomes, not input bodies.
See [the architecture](control-plane-workflows.md) for durability and scope.


## Host database storage

By default PostgreSQL uses the Docker named volume `control-db`. To keep the
control database on the host, set `CONTROL_DB_DATA_PATH` to an absolute directory
in the private `.env.control-plane` file, for example:

```dotenv
CONTROL_DB_DATA_PATH=/absolute/path/to/factory-data/control-postgres
```

For a fresh installation, create that directory before starting the stack.
On Docker Desktop, ensure the directory is shared with Docker. Database files
contain sensitive data; restrict access and exclude them from Git and file sync.
Keep the storage on a local filesystem suitable for PostgreSQL.

For an existing installation, do not point PostgreSQL at an empty directory:
that initializes a separate database. Stop API, reconciler and PostgreSQL first,
then copy the entire PostgreSQL data directory from the stopped container using
`docker cp -a factory-control-postgres-1:/var/lib/postgresql/data/. <destination>`.
Verify the copy, including `PG_VERSION`, before changing the environment setting.
Use the same PostgreSQL major version. Recreate the services without rebuilding,
check database recovery and API health, and verify saved profiles and workers.
Retain the original named volume until the migration and a fresh backup have
been verified. Never use `docker compose down -v` during migration.

Worker build logs and snapshots already use the host directory
`data/control-plane`. This setting moves only the control PostgreSQL data;
SigNoz storage and Docker image layers have separate storage settings. Host
mapping does not remove the need to monitor and reclaim Docker image space.

## Project-scoped configuration APIs

The backend can store independent configurations and multiple repository connections
for each project. Use `/api/v1/projects` to list or create projects, then retain the
returned project UUID in every project URL. These endpoints use the same operator
session, CSRF protection and `Cache-Control: no-store` policy as the existing API.
Projects separate configuration and history; they do not introduce per-user roles.

| Project URL suffix | Purpose |
| --- | --- |
| `/repositories` | List/create project-local aliases such as `backend` and `frontend`. |
| `/repositories/{id}` | Read or update one connection. Updates require its current `parent_id` revision; stale edits return 409. |
| `/factory-config` | Read/publish the project's shared settings with an optimistic parent revision. |
| `/worker-profiles` | List/publish project-local profile revisions; parents and clone sources must belong to this project. |
| `/config/revisions/{id}` | Read a full configuration revision; append `/export` for YAML. |
| `/config/effective` | Preview resolved settings for a repository alias and optional workflow/stage overrides. |
| `/events`, `/usage`, `/model-calls/{id}/conversation` | Read only the project's captured execution history and usage. |
| `/workflow-controls` | Inspect the project's persisted control receipts; receipt lookup is ownership-checked. |

All suffixes above start with `/api/v1/projects/{project_id}`. Missing or foreign
resource UUIDs return the same 404 response. Repository aliases are unique within
a project, so two projects may each connect a `backend` repository. Connection
updates append immutable revision records; an older revision remains available to
execution snapshots. Workflow-control idempotency keys are unique within a project.

For example, after authenticating as shown in the
[API recipes](../website/src/content/docs/api/recipes.md), create a project:

```sh
curl --fail-with-body -b /tmp/factory-cookies.txt \
  -H 'Content-Type: application/json' \
  -H 'X-CSRF-Token: YOUR_CSRF_TOKEN' \
  --data '{"slug":"payments","name":"Payments"}' \
  http://localhost:3400/api/v1/projects
```

Create a repository connection by posting
`{"alias":"backend","owner":"example","name":"payments-api","overrides":{"openai_model":"gpt-4.1-mini"}}`
to `/api/v1/projects/PROJECT_UUID/repositories`. The API validates the fully resolved
configuration before committing; invalid limits or identity overrides are rejected.
Credential references remain unavailable until the project credential service is
installed; do not put credential values in configuration documents.

A POST to `/api/v1/projects/PROJECT_UUID/config/effective` accepts:

```json
{
  "repository": "backend",
  "stage_repository": "frontend",
  "profile": "simple",
  "workflow": {"openai_model": "gpt-4.1-mini"},
  "stage": {
    "tasks": {
      "triage_issue": {"additional_system_prompt": "Include migration considerations."}
    }
  }
}
```

`stage_repository` is optional and must resolve within the same project as the run's
`repository`. Effective behavior applies project configuration/profile defaults,
then repository overrides, workflow defaults and stage overrides. Existing Settings
and task-schema limits still apply; the resolver also accepts stricter deployment
or pool limits from trusted server code. Clients cannot supply those limits. Identity,
credential, adapter factory, worker registration and task role fields cannot be
changed by repository/workflow/stage behavior overrides.

Existing unscoped configuration, profile and history URLs always refer to the stable
**default project**, regardless of a client's selected project. Workers, targets and
future worker pools remain deployment infrastructure. This API increment does not
start shared workers or install the later dynamic workflow coordinator; legacy static
workflow controls and managed-worker lifecycle remain on their compatibility path.

### Project credential versions and action contexts

Project credentials are managed through authenticated operator routes:

| Route | Purpose |
| --- | --- |
| `GET /api/v1/projects/{project_id}/secrets` | List version references, scope, purpose and revocation metadata. Values and ciphertext are never returned. |
| `POST /api/v1/projects/{project_id}/secrets` | Create an encrypted version. Use `parent_id` to rotate an existing version without changing its scope or purpose. |
| `POST /api/v1/projects/{project_id}/secrets/{identifier}/revoke` | Revoke that exact version. Repeated requests retain the original revocation time. |

Example request body for creating a project credential:

```json
{
  "name": "github-development",
  "purpose": "github",
  "value": "<provide your token securely>"
}
```

`purpose` accepts `github`, `openai` or `anthropic`. Add `repository_id` to restrict a
credential version to one repository connection in the same project. Omit it for a
project-level credential that can be explicitly granted to that project's repositories.
For repository-specific credentials, first create the repository connection, then create
the scoped secret and update the connection with its returned `id` as `credential_id`.
A repository connection accepts only an active, correctly scoped `github` credential.
A foreign reference returns 404; an owned revoked or wrong-purpose version returns 409.

To rotate a credential, submit a new value with the previous version's `id` as
`parent_id`, retaining its `name`, `purpose` and `repository_id`. Update the repository
connection to use the new version for future actions. Rotation leaves old pins valid;
revocation makes future resolution of the old pin fail. Neither operation silently
switches an existing action to the newest credential. Revocation cannot retract a
credential already delivered to an executing process; revoke it at the provider too
when immediate invalidation is required.

The action-context service stores an encrypted snapshot of effective settings plus
exact factory, repository, workflow and credential-version references. Generic context
serialization and logs omit effective settings as well as credential values. Trusted runtime
code pins this once per execution, with the required credential purposes. Replaying
context resolution uses that snapshot rather than current configuration. GitHub grants
come from the pinned repository revision; provider grants are explicit version IDs.
Missing, excessive, foreign and wrong-purpose grants are rejected.

Each action gets fresh GitHub/model clients and an isolated temporary CLI home and
workspace. Credentials are explicit constructor arguments or child environment values;
the worker process environment is never changed. Action-scoped filters remove resolved
credentials from result content, history, structured logs and escaping error messages.
Only `ActionInvocation` identifiers belong in engine payloads; full contexts and
credential-bearing objects must stay inside the authorized action process.

The context service provides internal pinning/resolution and isolated service composition.
Shared workers use the assignment-authorized context delivery described below. Existing
legacy workers continue using their existing bootstrap path.

### Shared worker pools and assignment authorization

A pool describes infrastructure capacity and permitted capabilities. It contains no
repository selection, model keys, prompts or project task configuration. A project
must be explicitly authorized to use a pool. Existing workers retain `legacy`
protocol and default-project behavior, even when their images are rebuilt.

In a project workspace, open **Shared worker pools → Add pool**. Choose a unique
workspace-wide name and role, review the role-filtered action versions, slots and
Hatchet transport (prefilled from the saved profile), then **Create pool**.
Creation does not authorize the project or start workers: **Authorize project**
and workspace **Workers → Add shared worker** are separate steps. Pool ID and
transport are shown under **Advanced pool details**.

For automation, the equivalent endpoint is `POST /api/v1/worker-pools`:

```json
{
  "name": "shared-simple",
  "role": "simple",
  "normal_slots": 2,
  "durable_slots": 16,
  "executors": ["builtin@1"],
  "actions": ["core.echo@1"],
  "hatchet_tls_strategy": "tls"
}
```

| Value | Effect and example |
| --- | --- |
| `name` | Unique infrastructure name, such as `shared-simple` or `shared-developer`. |
| `role` | `simple` or `developer`; selects the standard worker image and required engine routing label. |
| `normal_slots` | Concurrent action slots **per worker**, from 1 to 8; default 2. This does not replace project/deployment admission limits. |
| `durable_slots` | Durable engine slots per worker, from 1 to 16; default 16. The coordinator adapter uses these slots; public launch/API integration remains a separate milestone. |
| `executors` | Explicit versions allowed by policy, such as `builtin@1`. A worker advertises only locally installed capabilities. |
| `actions` | Explicit action versions allowed by policy. Shared startup currently installs only `core.echo@1`; the nine-action catalog/composition is implemented in source, while authenticated business-action transport activation and the Docker executor remain tracked milestones. |
| `hatchet_tls_strategy` | `tls` (default), `mtls`, or `none` for a deliberately unsecured development engine. |

Pool profiles are immutable in this increment. Create a replacement pool and new
workers to change infrastructure settings. Project behavior remains independently
revisioned through project/repository configuration.

Authorize a project with
`PUT /api/v1/projects/{project_id}/worker-pools/{pool_id}` and body
`{"enabled": true}`. Set `enabled` to `false` to deny subsequent claims, renewals,
context delivery and completion requests through that membership. Removing membership
cannot retract credentials already delivered or undo external effects.

Create a shared worker with `POST /api/v1/worker-pools/{pool_id}/workers`, for example
`{"name":"shared-simple-1"}`. Reconciliation creates its runtime; use the existing
worker start/stop/restart/remove actions for lifecycle management. Shared workers
cannot be configured with legacy project behavior profiles. To transition a legacy
worker, stop it and explicitly create a shared worker; no existing worker is silently
promoted. Rebuild the standard images first: shared creation requires image label
`software-factory.shared-protocol=1`.

The shared Docker environment carries only Hatchet and Control Plane infrastructure
identity. It receives no GitHub/model keys, project snapshots, shared `/data` mount or
Docker socket. Its separate startup path fetches the pool profile, negotiates
`shared-v1`, registers required pool/role/protocol labels, and advertises installed
executor/action versions. It never registers legacy repository scans or event handlers.
The pool/role-qualified engine task accepts only invocation identifiers and returns a
bounded execution receipt; action content stays outside engine payloads.

The trusted scheduler creates an execution authorization record with its project,
repository, run/stage/execution IDs, pool, role and exact action/executor versions.
Workers cannot create this authority by submitting their own project/run labels.
Claim checks authenticate the worker bearer token, explicit protocol registration,
project membership, persisted requirements and available normal slots. PostgreSQL row
locks ensure a concurrent claim has one owner.

Worker endpoints use `X-Worker-Id` and the worker bearer token, independently of browser
operator sessions:

| Route | Behavior |
| --- | --- |
| `POST /api/v1/shared-worker/register` | Negotiate pool, protocol, role, installed versions and slot bounds. |
| `POST /api/v1/shared-worker/claim` | Claim an authorized invocation with a caller-generated `claim_key`; returns the encrypted-at-rest input and a private lease proof. |
| `POST /api/v1/shared-worker/assignments/{identifier}/renew` | Extend the current lease using its `lease_token`. |
| `POST /api/v1/shared-worker/assignments/{identifier}/context` | Deliver the pinned settings and minimum credentials only to the owning worker with an active eligible lease. |
| `POST /api/v1/shared-worker/assignments/{identifier}/complete` | Persist an encrypted result; identical completion retries are accepted, while a changed result conflicts. |

Leases last 120 seconds and the worker renews every 20 seconds. Wrong workers, proofs,
capabilities, run IDs and stale leases are rejected. An expired assignment retains its
slot until reconciled; it is never silently handed to another worker while external
effects may still be running. Durable recovery, fair project/deployment admission and
the coordinator integrate in the run-state milestone (#7/#8). Legacy history ingestion
is rejected for shared workers; assignment-derived history follows in #12.

## Workflow definition API

These authenticated project-scoped routes publish graphs without starting runs.
Write requests use the existing operator session and CSRF protections.

| Route | Behavior |
| --- | --- |
| `POST /api/v1/projects/{project_id}/workflow-definitions/validate` | Validate `{"document": {...}}`; returns normalized digest and stage count without writing. |
| `GET /api/v1/projects/{project_id}/workflow-definitions` | List definition metadata. |
| `POST /api/v1/projects/{project_id}/workflow-definitions` | Publish the first immutable revision; returns definition ID and revision ID. |
| `GET /api/v1/projects/{project_id}/workflow-definitions/{identifier}` | Read definition metadata and current revision ID. |
| `PUT /api/v1/projects/{project_id}/workflow-definitions/{identifier}` | Append with `document` and current `parent_id`; stale parent returns `409`. |
| `GET /api/v1/projects/{project_id}/workflow-definitions/{identifier}/revisions` | List revision metadata and parent chain. |
| `GET /api/v1/projects/{project_id}/workflow-definition-revisions/{identifier}` | Read the decrypted document for an owned revision. |

Foreign project IDs/revisions return `404`. Validation rejects unknown action versions,
invalid graphs and bindings, incompatible pools, unscoped secret grants and unsupported
container options. There is no public snapshot/credential-resolution endpoint here.
The trusted run service will use the internal launch pin API in the run-state milestone.
See [creating data-defined workflows](workflow-development.md#data-defined-workflow-revisions)
for field defaults, examples, limits and the execution integration status.

## Durable run state and admission

The internal `WorkflowState` service persists runs, logical stage attempts,
encrypted inputs/results, business state and canonical external-resource claims.
Project-scoped run APIs, assignment-authenticated builtin/job transport and the
`dynamic-runtime` control service connect this state to the durable coordinator.
Saving a definition in the current UI still does not launch a run. Existing worker
pools retain their action allowlists; activate catalog actions deliberately.
See the [runtime API guide](project-runtime-api.md) for seeding, launch, cancellation
and uncertain-effect reconciliation.
See [builtin action contracts](workflow-development.md#builtin-actions-and-pinned-project-follow-ups).

A launch validates the chosen definition revision once and stores its exact graph,
repository/configuration revisions, effective stage settings and secret-version
references. Repeating the same project request key with identical input/revision
returns the same run; different input or revision returns a conflict. JSON object
key order does not affect identity. Advancing or replaying a run reads its stored
snapshot, including when an operator has since changed a repository connection.

| Policy | Default | Effect |
| --- | --- | --- |
| Active runs per project | `2` | Additional runs remain queued in persisted launch order. |
| Admitted actions per project | `4` | Bounds a project's ready/running actions across its active runs. |
| Admitted actions per deployment | `8` | Bounds all ready/running assignments, including reserved capacity. |
| Shared worker slots | Pool's configured slots, default `2` | Admission also requires registered role/action/executor capacity in that pool. |
| Assignment/resource lease | `120` seconds | A valid owner can renew; shared workers renew every `20` seconds. |

Trusted deployment code may inject `AdmissionLimits`, bounded to 1–32 runs/project,
1–64 actions/project and 1–256 actions/deployment. These are service configuration,
not new browser form fields. Ready projects rotate after each admitted action;
one project's parallel branches cannot drain the deployment queue ahead of every
other ready project. Frontiers sort by stage ID, and replay returns existing
execution IDs rather than creating another attempt.

All state transitions use a short database write fence before worker/assignment
locks. Both SQLite and PostgreSQL enforce it; concurrency tests use separate
PostgreSQL sessions and a file-backed SQLite database. Provider calls and container
execution do not run inside this scheduler transaction. Legacy lifecycle Docker
observations also run outside the fence; worker revision, lifecycle-operation state
and repository mode are rechecked inside it before accepting the command. A stale
observation returns a conflict so the operator can retry. PostgreSQL is the shared
production store for this runtime; legacy workers continue using their existing
SQLite state and file locks.

A transport retry reattaches the same execution identity. Only a confirmed terminal
failure may allocate the next configured retry generation, after the stage's retry
delay. A worker timeout or expired lease alone does **not** prove external effects
stopped: its slot and resource claims remain held for reconciliation. Terminal
completion is idempotent by execution/result digest; a changed result conflicts.
Never retry arbitrary external effects solely because a response was lost.

A false condition skips its stage and dependent branches. Failed prerequisites
block their descendants. Invalid resolved inputs or revoked/missing required
grants fail admission before an executable assignment survives the transaction.
Revoking one project's pool membership prevents new admission there without
rolling back other projects' progress. Disabling membership after admission does
not release an uncertain in-flight assignment; it still requires reconciliation.

### Business state and external-resource ownership

Business state is encrypted and scoped by project and repository connection.
The new `ExecutionStateStore` takes an explicit delivered action context and an
assignment-authenticated transport; it never reads a legacy SQLite key as a
fallback. Worker state/claim transport endpoints enforce assignment proofs and pinned scope. Generic stage
bindings retain encrypted repository-origin metadata across relays, and affine
builtins reject foreign-origin data before client construction.
Origin identities are deduplicated while collecting ancestry, with a separate
256 KiB logical JSON budget per admitted action. Exceeding that budget rejects the
stage before dispatch; repeated fan-in cannot amplify tiny outputs into unbounded
lineage records.

External claims use canonical lowercase GitHub host/owner/repository identity,
independently of project or local alias. For example,
`github.com/acme/service/issue/42` serializes two projects acting on the same
external item. GitHub pull requests and issues share this numeric claim namespace.
A repository-wide claim conflicts with all its item claims. Another project cannot
claim an external repository absent from that execution's pinned repository context.
Claims remain through exceptions and lease expiry until a trusted terminal receipt
or reconciler confirms effects stopped. This does not provide exactly-once writes
to GitHub or arbitrary Docker programs.

### Transition from legacy workers

Dynamic mutating actions require an explicit canonical repository mode transition.
The trusted `enable_dynamic_repository` operation refuses it while managed legacy
workers for that repository are not stopped/removed or have lifecycle operations
in progress. Once enabled, legacy create/configure/start/restart requests for that
repository are rejected; the reconciler enforces the same fence. Other repositories
and deterministic echo validation are unaffected. The current public API does not
expose this internal transition yet.

Drain standalone workers outside this Control Plane separately before enabling the
new mode. Their old file locks cannot coordinate with database claims. Existing
SQLite files, keys, worker identities and snapshots are preserved; nothing silently
migrates their business state or restarts containers.

Migration `e201f1000006` adds the run/state/claim tables and ownership constraints.
Downgrade removes new run/state/claim/mode data while retaining older project,
workflow revision and snapshot data. Drain new executions and back up these tables
before a downgrade; removing a mode fence during live execution is unsafe.
