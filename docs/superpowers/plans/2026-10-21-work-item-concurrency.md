# Work-item concurrency implementation plan

> **For agentic workers:** Execute inline with superpowers:executing-plans, preserving existing uncommitted token-counting and CLI collection fixes.

**Goal:** Avoid duplicate active issue workflows and classify work-item lock contention as waiting while preserving exclusivity and restart recovery.

**Architecture:** Hatchet owns active-run idempotency and queueing by repository + issue; a shared named concurrency strategy covers issue workflows and standalone feedback/merge tasks. The application keeps its provider-neutral state claim and retries acquisition for a bounded configurable interval. Observers classify an exhausted acquisition as waiting and rethrow for engine retries; real errors remain failures.

**Tech stack:** Existing Python, Hatchet SDK status-based idempotency/concurrency, SQLite/flock state, OpenTelemetry, control history.

## Tasks

- [x] Add regression tests for workflow registrations, duplicate dispatch acceptance and unrelated dispatch failures. Register per-issue shared concurrency before the existing global limit; use status-based issue workflow idempotency. Treat SDK idempotency collisions as an already-active dispatch, without swallowing integration errors.
- [x] Move OperationBusy to the domain (re-export from state for compatibility). Add task lock_wait_seconds default30, bounded by task timeout. Retry only claim acquisition; never retry the operation body inside the decorator. Test contention/release/exhaustion/body exceptions.
- [x] Add control-history/logging/OTel waiting outcomes and INFO messages for exhausted contention; preserve failure classification for real exceptions. Test these boundaries.
- [ ] Document defaults, dedup fallback TTL, remaining retry/exhaustion behavior, and single-host SQLite limits. Run worker and relevant backend tests, serialization checks and lint.
- [ ] Obtain one independent review of the completed change. Apply fixes and repeat relevant checks.
- [ ] Rebuild both managed workers and verify native registration/read-only engine metadata and heartbeats. Do not trigger or replay live model/GitHub workflows for verification.

## Decisions and limits

- Active issue-run identity excludes factory_run_id so cron, webhook, wait and manual dispatch converge on repository + issue.
- Idempotency fallback TTL defaults24h and is configurable; the per-issue concurrency limit remains the safety net if TTL expires before a run terminates.
- Feedback is queued/serialized, not deduplicated by issue, so new review events are retained.
- Busy waits can consume a worker slot within the configured bound. If contention exceeds all engine retries, Hatchet may still report its exhausted task as failed; factory telemetry records contention separately and never returns fake success.
- Kernel lock release on process exit and durable operation receipts remain unchanged. No stale-lock deletion.


## Review rulings

- Preserve discussion continuations: enqueue_issue returns false for an active
  predecessor and the durable waiter retries; accepting a duplicate is not the
  same as successfully scheduling its continuation.
- Manual triggers use a local SDK Workflow.run handle for typed collisions,
  without registering a definition. Unknown transport failures remain uncertain.
- Explicit schedule_timeout_seconds defaults24h, because the SDK's5m scheduling
  default is unsuitable for queues behind1h coding stages. Execution timeout and
  queue timeout remain separate settings.
