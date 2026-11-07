# Store provider credentials in OpenBao with assignment-authorized delivery

Status: accepted. Recorded 2026-11-06 from the approved OpenBao plan and the
implementation on `codex/openbao-credentials`. Adoption requires operator setup
and explicit migration.

## Context

Shared workers serve multiple projects. Credential versions pinned by workflows
must retain project ownership, purpose, repository scope and revocation behavior
while credential values move out of the application database.

## Decision

Use an operator-managed OpenBao KV v2 mount for provider values. Keep version
references and authorization metadata in PostgreSQL. Derive paths from project
and credential UUIDs, create with CAS 0, and pin the returned KV version. New
versions preserve lineage through the existing credential reference contract.

The Control Pane validates assignment authority and obtains wrapped reads from
OpenBao. Shared workers validate context, unwrap directly over TLS and acknowledge
action start before provider execution. Trusted server integrations resolve
credentials through one store module. Their service token comes from a private,
operator-managed sink. Worker capacity remains shared across authorized projects.

## Alternatives

The earlier salt-based database encryption proposal would retain credential
values in PostgreSQL. The selected OpenBao approach provides external secret
storage and delivery controls. Pool-wide OpenBao read identities would broaden
a worker's access across concurrent projects. Single-use wrapping keeps delivery
scoped to authorized assignment grants. Provider-specific credential issuance
can be evaluated separately from storing existing keys in KV v2.

## Consequences

OpenBao availability, TLS trust, policy, audit, backup and recovery become operator
responsibilities. Workers need network access to the trusted endpoint. Wrapping
and start requests have bounded retries. Revocation blocks fresh delivery, with
TTL and in-memory limits on values already issued. Provider invalidation is
required for immediate upstream key revocation.

Encrypted database credentials remain readable during migration. Copy verifies
each value before committing references, and a separate explicit clear removes
old ciphertext after the rollback window. Existing execution contexts retain
their encryption contract. An outage that prevents result redaction withholds
output and cancels the terminal stage to avoid replaying provider effects.

See [architecture and sequence](../architecture.md#openbao-credential-authority-and-delivery),
[operator instructions](../openbao.md) and [implementation plan](../roadmap/2026-11-05-openbao-credential-storage.md).
