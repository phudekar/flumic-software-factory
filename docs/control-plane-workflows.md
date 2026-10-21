# Workflow controls

The Workflows page lists only factory-owned workflow definitions through the
workflow-engine adapter. Trigger creates a run; Pause and Resume affect a
workflow definition across its workers, not individual active tasks or Docker
containers. The local Hatchet server reports native pause state for all eleven
registered factory definitions. No live mutations are performed during testing.

Pause leaves in-flight runs executing. Cron and scheduled triggers default to
DROP while paused, avoiding periodic scanner backlogs; operators can select
QUEUE instead and set a bounded queue TTL. Manual/event runs queue while paused.
Resume releases non-expired queued runs. Pausing a child workflow may leave
existing parents waiting for that child. This is not a factory-wide emergency stop.

API actions require an authenticated admin session, CSRF token and idempotency
key. Each action is recorded before the engine call, with a request digest,
workflow identity and status. Input bodies are validated but never persisted in
control-action/audit records. A repeated key returns its recorded outcome and
never replays a trigger. Reusing a key with different content is rejected. An
interrupted or timed-out engine call is marked uncertain (or remains running if
the process dies); operators must inspect workflow runs before explicitly issuing
another trigger. There is no automatic retry of uncertain actions and no false
claim of exactly-once delivery across the DB/engine boundary.

The adapter re-fetches the definition and verifies its factory allowlist for each
mutation. It validates inputs using the factory's existing Pydantic input models
and checks the configured repository. Native pause controls are disabled when
the server does not report pause state. Engine responses are reduced to
allowlisted metadata; vendor exceptions, workflow input and output are not
returned. Audit entries identify actions, not model conversations or credentials.

Implementation: extend the engine adapter with definition list/detail and control
methods; add a workflow control operation table and Alembic migration; install
scoped API routes; add navigation, definition cards/table, YAML run input, pause
options and recent action outcomes. Preserve worker configuration and runtime
behavior. Test adapter scoping, pause semantics, input validation, idempotency,
uncertain responses, audit and UI controls with mocks; read-only production
checks confirm the list and capability status. Back up the DB before deploying
the additive migration. No paid workflows are launched as deployment validation.
