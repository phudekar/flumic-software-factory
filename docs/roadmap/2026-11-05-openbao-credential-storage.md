# OpenBao credential storage and worker delivery

**Status:** Proposed for review. This plan supersedes the proposed salted database
encryption for provider credentials once OpenBao migration is approved.

## Current behavior and target

PostgreSQL stores encrypted provider values in `secret_versions.ciphertext`.
The Control Pane checks project ownership, repository scope, purpose and revocation,
then resolves the pinned credential version when an assignment requests context.
Built-in workers receive the resolved value in the context response today.
Repository validation, trigger polling, follow-ups and the job broker also consume
credentials, including direct ciphertext reads.

Keep credential identity, project and repository scope, purpose, version lineage,
revocation and audit metadata in PostgreSQL. Move provider values to a dedicated
OpenBao KV v2 mount. Each existing credential-version UUID maps to a server-derived
path such as `flumic/data/projects/<project-uuid>/credentials/<version-uuid>`.
Persist the exact KV version returned by OpenBao and pin reads to it. Create each
path with CAS 0. Rotation creates a new credential version and path. Saved workflows
and runs continue to reference their original UUIDs.

## Worker delivery

Recommended first release uses [OpenBao response wrapping](https://openbao.org/docs/concepts/response-wrapping/).
The worker retrieves the provider value directly from OpenBao through its
[unwrap API](https://openbao.org/docs/api/system/wrapping-unwrap/).
The Control Pane remains the authority for assignment access.

1. The worker claims an assignment and validates the pinned context.
2. Immediately before an action needs a credential, trusted worker code asks the
   Control Pane for delivery by purpose using its current assignment lease.
3. The Control Pane checks the worker, lease, pinned grant and credential status.
   It performs a version-specific KV read with a short wrapping TTL, initially
   capped at 30 seconds and further bounded by remaining assignment lease time.
   It sends the worker the single-use wrapping token and expected path/version.
4. The worker calls the configured OpenBao endpoint over verified TLS, checks the
   wrapping origin and validates the returned version and identity. It unwraps into
   memory and supplies the value to the existing repository or model adapter.
5. Scope in-memory reuse to the action attempt. Keep values and wrapping tokens
   out of durable context, logs, telemetry, spools and assignment results.
   Treat tokens as secrets. Release references at action teardown.

Use an operator-configured endpoint and CA, with redirects disabled. Workers get
no mount-wide token, list access or policy administration. Provider subprocesses
receive only the credential their adapter needs. Arbitrary container actions
retain the existing broker boundary. The trusted broker reads its explicitly
granted credentials, while untrusted action images receive only their existing
allowed inputs. Direct OpenBao access from arbitrary images is a separate design.

A worker-owned OpenBao identity issuing raw KV GETs is an alternative if direct
path reads are a deployment requirement. It needs assignment-specific policies,
token lifecycle and revocation enforcement. A pool-wide project read policy is
insufficient because shared workers serve different projects over time.

KV v2 delivers stored provider keys on demand. Issuing new provider credentials
per execution would require a provider-specific issuer and a separate design.
See [KV v2](https://openbao.org/docs/secrets/kv/kv-v2/).

## Failure, revocation and operations

- On sealed, unreachable or denied OpenBao, surface a redacted credential delivery
  failure and apply the existing retry policy before provider side effects.
  OpenBao-backed rows never fall back to database ciphertext.
- Expired or consumed wrapping tokens cannot be replayed. Allow bounded reissue
  after checking the active lease and grant again, before provider work starts.
  Audit unexpected consumption and lost unwrap responses without logging tokens.
- Revocation blocks fresh delivery. Already issued wraps can survive until their
  TTL, and an already retrieved provider key can survive in memory. Revoke or
  rotate the key at the provider for immediate upstream invalidation.
- Start with operator-managed OpenBao and a dedicated mount, verified TLS, audit
  and backup configuration. Use narrowly scoped Control Pane service identities.
  Qualify short-lived service authentication for Docker and Kubernetes before
  rollout. Keep root and recovery credentials outside Flumic.
- Record only safe operational identifiers and outcomes. Review request logging,
  OpenBao audit settings, model tracing and CLI subprocess environment filtering.

## Implementation sequence

- [ ] **Storage adapter.** Introduce create/read/wrap operations behind one
  credential store interface. Use KV v2 create-only writes and exact-version
  reads. Add nullable backend and locator metadata alongside existing ciphertext.
  Validate that persisted references match project and credential UUIDs.
- [ ] **Control Pane paths.** Route creation, validation, trigger polling,
  follow-ups and Job brokerage through that interface. Use a pending/ready
  protocol for writes across PostgreSQL and OpenBao. On retries, reconcile an
  ambiguous OpenBao write by comparing the intended value in memory. Never
  overwrite a conflicting path or grant a pending credential.
- [ ] **Worker protocol.** Add explicit wrapped-delivery capability negotiation.
  Deploy compatible readers and workers before enabling wrapped delivery.
  Authorize each purpose on demand and test Docker and Kubernetes egress.
  Existing database-backed credentials remain readable during migration.
- [ ] **Migration.** Take and restore-test a PostgreSQL backup with its matching
  encryption key, and qualify OpenBao backup and recovery. Pause credential
  writers and drain active assignments. Dry-run inventory by UUID and status.
  Copy encrypted database rows one at a time through in-memory decryption,
  create-only OpenBao writes and exact-version read-back verification. Record
  verified references without changing credential IDs, pins or revocation state.
  Include revoked versions and keep them ineligible for delivery.
- [ ] **Cutover and cleanup.** Switch to reference reads and OpenBao-only writes,
  verify workflow launches and server-side consumers, then clear old ciphertext
  in a separate explicit step. Before new OpenBao-only writes, rollback can use
  the verified pre-cutover store. Afterward, use a compatible build or reconcile
  newer credential versions before restoring older data. Remove the ciphertext
  column only after the rollback window and dependency checks. Manage historical
  database backups and WAL under retention policy.
- [ ] **Qualification.** Run real OpenBao integration tests for KV versions,
  wrapping origin, expiry and replay. Exercise wrong project, purpose, worker
  and lease, rotation during a pinned run, revocation, sealed/unavailable server,
  concurrent writes, interrupted migration, and paired recovery of database
  references and OpenBao values. Run a fake-provider workflow through each
  supported runtime and verify no plaintext or wrapping tokens reach browser
  read responses, persisted execution data or logs.

The first release covers project credential versions. Inventory webhook signing
secrets, worker authentication and other encrypted payloads separately. Keep
`CONTROL_ENCRYPTION_KEY` available for existing encrypted contexts and migration.
