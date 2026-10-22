---
title: "Resource and response contracts"
---

The [generated endpoint reference](/software-factory/reference/api/) covers every method/path, request model, query constraint, handler status and explicit HTTP errors. This guide explains the resulting resources. Adapter-owned execution data can vary by Hatchet/server capability; inspect warnings rather than inferring missing states.

## Workers and runtime

`GET /workers` returns `{managed, discovered, warnings, observed_at}`. Managed records contain their database identity, profile links, runtime observation, engine registration, build summary and latest operations. Discovered entries are read-only Docker observations.

Worker creation and actions return an **Operation** row: identity, worker ID, action, status, idempotency key, errors and timestamps. A 202 response means the reconciler has work, not that the image is built or container running. Observe subsequent `GET /workers` operations/build status. Create and worker actions require a nonempty `Idempotency-Key` of at most 100 characters. Reusing a key returns the existing receipt; use distinct keys for distinct intentions.

Build records expose state and bounded redacted build output. Runtime logs return `{source, content}` with up to the backend's last 200 lines. These are distinct from stage logs.

## Workflow actions

`GET /workflows` returns `{items, warnings}`. Each definition reports native control capabilities. Action history returns up to 25 receipts for a definition.

`POST /workflows/{identifier}/actions` accepts `trigger`, `pause`, or `resume`. Its request model includes `input`, `queue_ttl_seconds` (60–2,592,000; default 86,400), and `cron_behavior`/`scheduled_behavior` (`DROP` or `QUEUE`). It requires an 8–100 character `Idempotency-Key`, rejects actions above 64 KiB and recognized credentials in input, and checks native pause support. Reuse with different input returns 409.

The receipt includes `id`, workflow identity/name, `action`, `status`, `result`, `error`, `created_at`, `updated_at`. Completed means the engine call completed. Uncertain means inspect current state/runs before issuing another action. A receipt ID is attached to triggered run metadata for correlation.

## Runs, events and conversations

`GET /runs` returns `{items, source: "hatchet", warnings}` with offset and a limit from 1 to 100 (default 50). Detail IDs are UUID validated. Engine logs return `{items, source, note}` and only include logs explicitly sent to Hatchet.

Events are database rows with worker/run/attempt/stage identity, timestamps, kind and payload. `kind=attempt` includes attempt start and completion. Offset defaults to 0 and limit to 100, maximum 500. Run/stage/task/attempt/worker scopes are combined when supplied.

Conversation GET decrypts retained redacted JSON for an authenticated administrator and writes an audit record. It returns 404 if content was not captured or retention expired.

## Usage

Usage returns `calls`, `input_tokens`, `output_tokens`, `estimated_usd`, `reported_usd`, `unpriced_calls`, `missing_usage_calls`, `limit`, `truncated` and `note`. Unknown totals are null. It processes at most the latest 10,000 matching `model.completed` events and sums accounting leaves to avoid parent/child double counting. Estimated and reported USD are separate views of costs; do not add them together.

Supported scopes are `since` (nonnegative Unix seconds), `run_id`, `attempt_id`, `worker_id`, `engine_task_id`, and `stage`. See the [stage accounting guide](/software-factory/observability/stage-accounting/).

## Profiles and factory revisions

Profile records contain a revision ID, family ID, name, worker type, pinned factory revision, parent/clone ancestry, parsed document and timestamps. List routes return all revisions; clients choose latest per family. Save with `parent_id` appends to the same family; use `cloned_from` to create another. Both together are rejected. Stale parents return 409. Validate performs complete composition without publishing.

Factory GET returns `{id, document}`; POST validates against the current `parent_id` and creates a revision. Compatibility config-revision routes expose legacy complete documents, validation digest and YAML export. New clients should use scoped factory/profile routes.

## Worker bootstrap and ingestion

Bootstrap checks scoped credentials and the installed image's configuration, returns a resolved snapshot, and omits build-only settings. Build-affecting profile changes require lifecycle replacement before adoption.

Ingestion authenticates a worker, validates event kinds/identifiers/payload bounds (up to 50 events and 512 KiB per batch), redacts content, and returns `{accepted: count}` (the count is the submitted batch length, including already-seen event IDs). Spool delivery is best effort and can leave gaps; these records are not an invoice ledger. See the handler and `Incoming` model in the reference for the exact contract.
