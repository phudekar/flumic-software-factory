# Project history, traces and AI accounting

The project runtime attributes execution history to the stored assignment, rather than trusting project labels supplied by a worker. Several projects may share a worker while keeping events, model exchanges and usage separate. The project UI exposes scoped run/attempt inspection, model history and usage; the scoped operator APIs below provide the same project authority for integrations.

For activation and run lifecycle, see the [runtime API guide](project-runtime-api.md). Existing default-project static workers and their history remain compatible.

## Roll out the history transport

Rebuild the Control Plane and the images used by shared pools from this revision, apply the additive database migration through the one-time `migrate` service during normal control startup, and recreate shared workers through the managed lifecycle when ready. Updating saved configuration does not replace code in an already-running image. No existing live worker or database was restarted during the implementation tests.

The worker needs writable private spool storage. The managed root worker takes ownership of its dedicated mount before writing; arbitrary action/CLI users do not receive that infrastructure directory. The operator-provided price table is mounted read-only. These mounts contain no global business-state database or worker control socket.

## Where attribution comes from

The server checks the registered worker and its original assignment proof, then loads the owned logical attempt and immutable run snapshot. It derives project, repository, definition, revision, run, stage, execution, executor and job identities. Contradictory caller labels are rejected. Engine identifiers supplied by a worker are not accepted as history authority; the run API exposes the server's engine-dispatch receipt.

| Identity | Event/API field | Builtin span attribute |
| --- | --- | --- |
| Project | `project_id` | `factory.project_id` |
| Repository connection | `repository_id` | `factory.repository_id` |
| Workflow definition family | `definition_id` | `factory.definition_id` |
| Published workflow revision | `workflow_revision_id` | `factory.workflow_revision_id` |
| Invocation | `run_id` | `factory.run_id` |
| Logical graph stage | `stage` | `factory.stage_id` |
| Logical attempt/execution | `execution_id` and `attempt_id` | `factory.execution_id`; `factory.attempt` is its generation number |
| Assignment | `assignment_id` | `factory.assignment_id` |
| Assigned shared worker | `worker_id` | `factory.worker_id` |
| Versioned executor | `executor` | `factory.executor`, such as `builtin@1` |
| One-time Docker job | `job_id` | Available to custom job instrumentation through its context file |

A stage's application operation name can differ from its graph stage ID. For example, graph stage `plan` may execute operation `plan_issue`. New shared action metadata keeps the logical stage/attempt identities stable across the operation, model calls and logs. The existing run detail endpoint also lists attempt generations.

Settings, prompts and credential values are not attribution fields. Old records without reliable new dimensions retain `null` attribution; migration does not guess a repository or execution for historical data. Assignments pinned before this upgrade receive derived definition/executor/worker fields from their original stored identities, without repinning business configuration.

## Query events and usage

Operator endpoints require an authenticated administrator session. Replace placeholders with IDs returned by your deployment. The unscoped `/api/v1/events`, `/api/v1/usage` and conversation routes remain explicit default-project aliases.

~~~sh
CONTROL=http://localhost:3400
PROJECT=<project-id>

curl -b cookies.txt --get \
  --data-urlencode 'repository_id=<connection-id>' \
  --data-urlencode 'definition_id=<definition-id>' \
  --data-urlencode 'stage=plan' \
  --data-urlencode 'executor=builtin@1' \
  "$CONTROL/api/v1/projects/$PROJECT/usage"
~~~

Both scoped event and usage queries support `repository_id`, `definition_id`, `workflow_revision_id`, `run_id`, `stage`, `execution_id`, `attempt_id`, `worker_id`, `executor`, `job_id`, `provider` and `model`. Usage also accepts `since` as a Unix timestamp. Event queries accept `kind`, `offset` and `limit` (1–500). Existing legacy engine filters remain available for legacy history; use canonical invocation/execution IDs for the dynamic runtime.

Filters apply before aggregation and row limits. A foreign repository, worker or execution filter cannot escape the selected project. Fetch model exchanges with:

~~~text
GET /api/v1/projects/{project_id}/events?kind=model.completed
GET /api/v1/projects/{project_id}/model-calls/{event_id}/conversation
~~~

Conversation reads check ownership and retention, and create the existing read audit entry. An event identifier from another project returns `404`. An expired or uncaptured exchange also returns `404`.

## Understand the totals

| Result | Interpretation |
| --- | --- |
| `input_tokens: 0` | Known reported zero; not missing usage. |
| `input_tokens: null` | No known input-token value in the selected records. |
| `estimated_usd` | Known subtotal of list-price or explicitly supplied estimates. |
| `reported_usd` | Separate known subtotal of provider/CLI/action-reported USD. Do not add it to estimated USD. |
| `unpriced_calls` | Leaf calls without a usable estimate, even if reported USD exists. |
| `missing_usage_calls` | Leaf calls lacking input or output token counts. |
| `truncated: true` | The 10,000 candidate-call limit was reached; narrow the filters/time window. |

Totals are known subtotals, not invoices. Mixed known and unknown records retain the known subtotal and expose the missing/unpriced counts. No model calls means token/cost totals are `null`, not inferred zero. Cache/reasoning details and pricing metadata remain available on individual model events; reasoning is not added to output tokens a second time.

Nested instrumented parent/child calls retain their trace relationship, but only accounting leaves contribute to cost/token totals and model-usage metrics. A CLI aggregate is a leaf when no nested model call was observed. Provider/model prices come from the configured price table and the recorded estimate context; changing a table does not rewrite existing event totals.

Worker delivery and model-call identifiers are treated as source data. The server derives public event and call IDs using keyed, assignment-scoped digests; preserve the original source packet for delivery retries and use returned event IDs for conversation reads. Worker spooling also hashes call/parent IDs and redacts unknown payload keys, so arbitrary strings cannot bypass secret filtering. Only allowlisted protocol names and verified assignment metadata remain exempt.

Event IDs identify delivery. Model call IDs identify accounting within a logical execution. Retrying delivery must preserve the original ID, timestamp, call ID and payload. Reordered JSON object keys do not create another charge. An identical call replay with another delivery ID is deduplicated; changed content for a stored identity returns `409`. A new logical retry attempt remains a separate execution and can incur separate actual usage.

## Capture policy and redaction

The immutable action settings select `conversation_capture`:

| Value | Control Pane history behavior |
| --- | --- |
| `redacted` | Retain model metadata/usage and bounded redacted exchanges. |
| `metadata_only` | Retain model metadata/usage, remove exchanges. |
| `disabled` | Do not retain model-call history or its usage events. Operation/log history remains separate. |

The server enforces this policy from the pinned context, even if a worker submits a more permissive payload. A later configuration edit does not change an existing invocation's pin. OpenTelemetry export is configured separately; disabling Control Pane model history does not disable an installed OTEL observer.

Workers redact with action-local grants without mutating process environment. Spool payloads are redacted before writing. Ingest rechecks bounded finite JSON and redacts against the assignment's pinned grants, including revoked versions used solely for historical redaction. This does not grant new execution access. Protocol field names and canonical identifiers remain stable when a grant happens to match a field name.

Conversation content is encrypted at rest and receives the existing seven-day expiration on ingestion. Redaction and bounded capture do not classify every piece of confidential source content; choose capture mode for your deployment's data policy.

## Durable, bounded worker delivery

Shared startup uses `FACTORY_SHARED_HISTORY_ROOT`, default `/data/control-spool`. The managed Docker runtime mounts only that worker's private host directory from `data/control-plane/shared-history/{worker_id}`, plus the read-only price table. It does not mount global business state or pass project provider tokens through container environment.

The spool stores the original assignment proof and already-redacted event payload. Directories are private (`0700`), files are `0600`; treat this as private worker state. It survives a managed container replacement because the host directory is retained. Model content can still be sensitive according to capture policy.

Delivery runs separately from business execution, normally waking every two seconds, with up to 25 records per batch of sends. The cursor rotates over pending files so a repeatedly failing project's records do not indefinitely exclude another project's delivery. Infrastructure heartbeats are attempted every 15 seconds and update `last_heartbeat_at` on the worker; they do not create project events or AI usage. OTEL worker heartbeat/resource metrics likewise remain infrastructure-scoped.

At most 500 queued records are retained, with a 500,000-byte per-record budget after redaction. Oversized conversation capture is dropped while bounded usage metadata is retained where possible. Older records are evicted when the queue fills. History is best effort and bounded; the scheduler's canonical run state does not depend on successful history delivery.

Worker OTEL gauges have a separate worker-lifetime provider and aggregate concurrent handlers across projects. Closing an action observer does not close that provider: the total returns to zero when idle and heartbeats continue until worker shutdown. Action model/spans keep their own pinned observer lifecycle and actual shared worker identity.

Set `FACTORY_SHARED_TELEMETRY_ENDPOINT` to choose the infrastructure metric collector explicitly. Empty/unset activates gauges lazily on the first OTEL action's endpoint, retaining that endpoint for the worker lifetime. Other projects still use their own pinned action exporters. `FACTORY_SHARED_DEPLOYMENT_ENVIRONMENT` labels this infrastructure provider (default `development`), independently of project configuration. Recreate the shared worker when changing these environment settings; the base Control Plane Compose manifest forwards them through the reconciler to newly created runtimes.

An authentic original proof can deliver delayed history after lease expiry or terminal completion. It cannot renew execution, fetch fresh credentials or begin new effects. API batches are bounded to 50 events and 512 KiB, including post-redaction accounting. Retrying a malformed/conflicting record cannot change stored accounting. Inspect redacted worker warnings and the private spool when records remain queued.

## Custom Docker actions

New jobs receive a read-only `/flumic/context.json` in addition to `input.json` and separately granted `credentials.json`. Context includes server-derived project/repository/definition/revision/run/stage/attempt/worker/executor/job IDs and, when available, the W3C trace carrier. It contains neither effective prompts/settings nor provider/control credentials. Older already-pinned jobs may lack this optional file.

Custom code can use this file to label its own instrumentation. This does not automatically install an exporter, allow network egress or create a trusted worker ingest credential. Network defaults to `none`. Server-owned accounting derives identity from the broker receipt instead of trusting labels written by the job.

Declare actual AI usage in `/flumic/output/result.json`:

~~~json
{
  "output": {"tests_passed": true},
  "usage": [
    {
      "id": "model-call-1",
      "provider": "my-provider",
      "model": "my-model",
      "input_tokens": 0,
      "output_tokens": 12,
      "estimated_usd": null,
      "reported_cost_usd": 0.002
    }
  ]
}
~~~

Each `id` must be unique within the result and stable for the same actual call. Report leaf calls, not an aggregate plus the same underlying calls. Token counts are nonnegative integers or `null`; monetary values are finite, nonnegative USD or `null`. Provider/model strings are bounded metadata, not executable provider selection. Duplicate usage identities make the result contract invalid.

The broker-confirmed result is attributed and recorded in the same transaction as stage settlement. Reconciliation repeats cannot double count it. Empty/missing usage, CPU/memory measurements, stdout text or a zero exit code do not imply zero AI cost. The producer must explicitly report supported usage. The pinned model-history capture policy still applies; declared usage also remains part of the encrypted business result.

See the [Docker action contract](docker-action-jobs.md) for lifecycle, result size, cancellation and sandbox limits, and the [SigNoz guide](observability.md) for collector setup and existing dashboard examples.
