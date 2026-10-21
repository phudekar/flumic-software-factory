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
  explicit start/stop/removal through durable, idempotent operations.
- Configuration and prompt validation, immutable revisions, optimistic edit
  conflicts, YAML export, and explicit application to stopped managed workers.
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

The app imports the current split YAML tree into an initial resolved revision.
Configuration edits use a resolved JSON editor, with YAML export for Git review.
The Prompts view edits task-specific `additional_system_prompt` values, showing
the current effective value for a selected worker profile. Task overrides replace
inherited template guidance. Built-in worker instructions are still supplied by
the image and are not editable through this field.

Saving creates an immutable revision without changing repository files or
running workers. In Workers, stop a managed instance, select its configuration
action, and choose a revision. The reconciler replaces its stopped runtime with a
new read-only snapshot and leaves it stopped. Selecting an older revision restores
that configuration. Automatic rolling deployment/drain and coordinated workflow
registration migration are not implemented. Changes to schedules, routing,
retries, or workflow concurrency require care because workers register shared
Hatchet definitions; use matching revisions across participating roles.

Each runtime carries factory ownership, instance, profile, and revision labels.
Snapshot directories include both instance and revision IDs. The backend maps
container paths to an explicit host-visible path; browser requests cannot choose
arbitrary host mounts, images, commands, or credential variables. Worker images
must have the control-protocol label, supplied by this migration's image build.

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
