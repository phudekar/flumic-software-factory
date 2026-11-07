# OpenBao credential storage

Flumic can store project provider credentials in an operator-managed OpenBao KV v2
mount. PostgreSQL stores credential IDs, ownership, purpose, revision lineage,
revocation, and the pinned KV version. OpenBao paths are derived from the mount,
project UUID, and credential UUID. Keep the configured OpenBao instance and mount
stable for a database.

## Configure trusted services and workers

Enable a dedicated KV v2 mount before using Flumic. Set these values in the private
Control Pane deployment environment.

| Variable | Purpose |
| --- | --- |
| `CONTROL_OPENBAO_ADDR` | Trusted HTTPS origin reachable from the Control Pane and shared workers |
| `CONTROL_OPENBAO_MOUNT` | Dedicated mount, default `flumic` |
| `CONTROL_OPENBAO_TOKEN_FILE` | Path to a private token sink inside trusted Control Pane containers |
| `CONTROL_OPENBAO_CA_FILE` | Optional CA bundle path inside trusted Control Pane containers |

Mount the service token sink and optional CA bundle into the API, reconciler,
and dynamic runtime with a private Compose override. An operator-managed auth
agent can refresh the token file. Flumic reads it when making requests. Scope the
service policy to create and read on the dedicated credential data paths. The
service does not need list, update, destroy, or policy-management privileges.
Keep the OpenBao root token and recovery material in operator custody.

Build and replace shared worker images before migrating existing credentials.
Docker and Kubernetes workers receive the OpenBao origin from the Control Pane
configuration. Their images must trust the endpoint certificate. For privately
managed workers, `FACTORY_OPENBAO_CA_FILE` can name an already mounted CA bundle.
Qualify network egress and DNS resolution from each worker target.

At built-in action startup, a lease-authorized context request returns one
short-lived wrapping token per OpenBao-backed grant. The worker checks the
wrapping origin and calls the OpenBao unwrap API over TLS. It keeps the resulting
provider value in memory for that action. The worker acknowledges start after retrieving grants,
so credential delivery does not consume the stage execution timeout. Older workers are rejected for
OpenBao-backed grants. Trusted server consumers such as repository validation
and Job brokerage read the pinned KV version using the service identity.

## Migrate an existing installation

Use a trusted maintenance environment with the same database, encryption key,
OpenBao origin, mount, token sink and CA configuration. Run the current schema
migration first. Restore-test PostgreSQL and OpenBao backups together before
cutover. Pause admissions and credential writes and drain all assignments.
Stop control services before applying the copy or clear step.

```sh
python scripts/migrate-openbao-credentials.py inspect
python scripts/migrate-openbao-credentials.py copy \
  --expected-count <count> --fingerprint <inspection-fingerprint> \
  --verified-backup-and-offline
```

Copy reads each existing value into memory, creates its OpenBao path with CAS 0,
and verifies the pinned version by read-back. The PostgreSQL transaction switches
the verified rows to OpenBao references together. Existing ciphertext is retained
for the rollback window. An interrupted copy can leave a create-only path in
OpenBao. The next copy checks the value at that path before adopting it.

Resume qualified workers and verify repository access and normal project
execution. When the rollback window has closed, quiesce services again, take
fresh backups and repeat inspection. Run `clear` with the new inventory count
and fingerprint plus `--verified-backup-and-offline`. Clear verifies values again
before removing legacy ciphertext. Historical database backups and WAL follow
their retention policy. Keep the root encryption key for existing encrypted
contexts. Restore OpenBao data and PostgreSQL references to compatible points
in time.

## Failure and revocation

OpenBao-backed records fail closed when the store cannot serve the exact
version. Revocation blocks new wrapping requests. An issued wrap can remain
valid until its short expiry, and the provider must invalidate a key already
retrieved by a worker. An ambiguous unwrap requires a newly authorized context
request before provider execution. A credential write with an ambiguous response
can be retried with the same idempotency key and payload. The credential form
retains its key for an unchanged retry. Inspection lists pending credential UUIDs
and project IDs. If a pending submission cannot be retried, revoke that specific
version with the authenticated project secret revocation API before migration.
An abandoned OpenBao path requires separate operator reconciliation.

If OpenBao cannot supply redaction values when an action reports completion,
the controller records a cancelled terminal receipt with output withheld.
This blocks automatic replay of provider effects. History ingestion can retry
after the store recovers.

Qualify the OpenBao transport against a disposable TLS server with the optional
`OPENBAO_TEST_BIN` test described by `tests/test_openbao_live.py`.

## Architecture and decision record

The [credential sequence](architecture.md#openbao-credential-authority-and-delivery)
shows storage, authorization, worker unwrap and start acknowledgement.
[ADR 0008](adr/0008-openbao-credential-authority.md) records the alternatives,
operational costs and migration guarantees.
