# Software factory control plane

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
single administrator session model. Multi-user roles, OIDC, remote target
credentials, and Kubernetes execution are future work. Do not publish it as a
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
- Separate ports for runtime and workflow adapters. Only the `local-docker`
  target is enabled; Kubernetes is not implemented.

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
more scalable paginated cost rollups, and a Kubernetes runtime adapter.

Stage logs currently combine the instrumented worker event stream with Hatchet
logs when available. SigNoz continues receiving standard OpenTelemetry signals,
but its query adapter is not yet implemented in the control plane. The run view
shows stage cards, not a reconstructed DAG with full dependency edges. Every
adapter returns unavailable/unknown states instead of fabricated monitoring data.

A PostgreSQL control-plane database does not remove the factory's single-host
SQLite/file-lock constraint. A shared execution-state/claim adapter is required
before workers can safely execute across Kubernetes nodes or multiple hosts.

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
