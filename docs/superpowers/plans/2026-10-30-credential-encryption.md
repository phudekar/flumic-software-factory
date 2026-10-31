# Salted Credential Encryption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task after review. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** proposed plan only. No encryption code, credentials, environment keys or live ciphertext have changed.

**Goal:** Accept a deployment salt from Control Pane environment configuration and use a separate derived key for reversible credential encryption, with safe migration and recovery.

**Architecture:** Keep the existing random `CONTROL_ENCRYPTION_KEY` as secret root material. Derive a purpose-separated credential key with HKDF-SHA256 using an environment-provided salt, and retain authenticated Fernet encryption. A small credential cipher owns versioned envelopes and row binding; existing generic context/conversation encryption stays unchanged.

**Tech Stack:** Python, existing cryptography==46.0.7, FastAPI, SQLAlchemy/PostgreSQL, Docker Compose, unittest and fixture-based end-to-end tests. No new crypto package or custom encryption primitive.

**Spec:** Proposed design contract in this document; implementation touchpoints are `control-plane/backend/app/{project_context,repository_validation,settings}.py`.

## What already exists

- `SecretVersion.ciphertext` already holds Fernet-encrypted JSON using `CONTROL_ENCRYPTION_KEY`; this is not a plaintext database today.
- Generic `encrypt`/`decrypt` in `app/project_context.py` also protect immutable action/workflow contexts. Do not change their format as a side effect of credential encryption.
- `resolve_secret` enforces ownership, purpose, repository scope and revocation before returning a credential to trusted execution.
- `repository_validation.py` directly decrypts a secret for GitLab identity validation and must use the new credential reader too.
- The same root setting also participates in worker bearer credential derivation. Replacing it would invalidate more than stored credential ciphertext.

## Proposed design contract

A salt is not an encryption key and need not be secret. Hashing credentials is also not a substitute: workers need the original provider token for authenticated requests. Encryption still requires secret, randomly generated key material. This proposal assumes machine-generated root key material, not a human password.

### Approaches and recommendation

1. **Recommended:** existing random root key + environment salt + HKDF domain separation + Fernet. Reuses installed crypto, minimizes deployment changes and isolates credential encryption from context encryption.
2. Password + salt + a memory-hard KDF would require a new secret/password policy, cost tuning and a different recovery contract. It is unnecessary while the installation already has a strong random root key; HKDF must not be used to stretch a weak password.
3. External KMS/envelope encryption can later implement the same narrow cipher interface, but introduces an external availability/permissions dependency. It is outside this first implementation.

These are planning recommendations, not claims that implementation or a new security design has already been approved.

### Environment configuration

| Setting | Proposed contract |
| --- | --- |
| `CONTROL_ENCRYPTION_KEY` | Existing secret URL-safe base64 encoding of 32 random bytes. Preserve exactly. Never substitute a salt or administrator password. |
| `CONTROL_CREDENTIAL_SALT` | Required URL-safe base64 encoding of 32 random bytes for the active credential key. Deployment-specific, stable across restarts; outside source control. |
| `CONTROL_CREDENTIAL_KEY_ID` | Non-secret active key identifier; default `credentials-v1`; slug of 1–64 lowercase letters/digits/hyphens beginning with a letter. New salt rotations require a new identifier. |
| `CONTROL_CREDENTIAL_OLD_SALTS` | Optional private environment JSON object mapping previous key identifiers to their base64 salts for decryption only. No duplicate identifiers, no active identifier in this map, at most 8 historical entries. Same root key only. |

Decode base64 strictly and require exact lengths. Invalid/missing settings fail startup with redacted configuration errors, never silently default to a constant, regenerate a salt, or fall back for new writes. Supply consistent configuration to API, reconciler, dynamic runtime and maintenance commands. Shared action workers never receive these settings.

For a fresh installation, the launcher generates the root key and salt once using secure randomness in a mode-0600 environment file. For an existing installation, explicit setup adds only the missing salt/key ID without replacing any existing credential, root key, database password or salt. No generated secrets are printed. Docker environment/host administrators remain trusted: this proposal protects database-at-rest data, not a compromised Control Pane process.

### Derivation, format and identity binding

Derive 32 bytes using HKDF-SHA256 with decoded root key as input, configured salt, and fixed `info=b"flumic/credentials/fernet/v1"`. URL-safe-base64 encode the result for Fernet. HKDF is for high-entropy input; it does not make weak passwords safe.

Store a bounded, versioned envelope in the existing Text ciphertext field:

```text
flumic-credential:v1:<key-id>:<fernet-token>
```

The authenticated plaintext contains only the value and its binding: version,
project ID, credential version ID, purpose, repository ID (including explicit null)
and key ID. Generate the credential version UUID before encrypting, then verify all
binding fields against the persisted row after decrypting. This rejects copying
ciphertext across projects, rows, purposes or repository scopes. Header metadata
is routing information, not trusted authority; the encrypted key ID must match.

Legacy bare Fernet tokens are read with the original root key only during migration.
A recognized new envelope never falls back to legacy parsing after failure.
Unknown formats, malformed envelopes, unknown key IDs, tampering and binding
mismatches produce the same redacted unavailable-credential error. Never expose
ciphertext, decoded keys, supplied salt values or plaintext in responses/logs.
Fernet remains randomized: repeated encryption yields different tokens.

Expose one focused module:

- `CredentialBinding(project_id, credential_id, purpose, repository_id)` frozen dataclass.
- `CredentialCipher(settings)` validates and derives the active/read-only keys.
- `seal(value: str, binding: CredentialBinding) -> str` writes active envelopes only.
- `open(ciphertext: str, binding: CredentialBinding) -> str` verifies authenticated identity and supports legacy read during rollout.
- `inspect(ciphertext: str) -> EnvelopeMetadata` returns format/key ID only; never treats this metadata as proof of integrity.

Keep project ownership/revocation checks outside the cipher and before decrypt.
A successful cryptographic operation must not authorize a revoked or foreign secret.
All credential consumers use one trusted helper; grep for direct decrypts of
`SecretVersion.ciphertext` before declaring integration complete.

### Startup, rotation and recovery

Validate settings at process initialization. Before reporting ready, verify at least
one stored envelope for every represented key ID, and reject envelopes requiring
unconfigured keys. This detects incorrect salt/root material for stored key IDs.
A changed salt under an existing key ID must not silently start writing a new key.
An empty database has no existing ciphertext against which to check configuration.

Rotating the salt requires a fresh key ID, the previous salt in the read-only map,
and explicit verified re-encryption. Never reinterpret an existing ID with new
material. Root-key rotation is a separate project because existing contexts,
conversations and worker credentials also depend on it.

Back up the database, root key, active salt, historical salts and key IDs together.
After the first new-format write, old application versions cannot read it. Code-only
rollback to such a version is unsafe; either keep a reader-compatible release or
restore the pre-migration database plus its matching private environment backup.
Never remove an old salt while any live record or required backup still needs it.

## Global Constraints

- Preserve credential version IDs, project/repository ownership, purposes, revocation state and pinned references.
- Do not rewrite immutable action/workflow pins or conversation ciphertext.
- Do not rotate CONTROL_ENCRYPTION_KEY or invalidate existing worker authentication.
- No plaintext in database columns, API read responses, logs, telemetry, audit records or migration manifests.
- Salt alone is never accepted as secret encryption material.
- No paid model calls or live GitHub/GitLab writes in default tests.
- No automatic re-encryption on startup or GET; migration is an explicit operator operation.

## Review Focus

- Wrong or missing salt after restart: fail closed with a recoverable redacted error (tasks 1, 2, 5).
- Ciphertext copied across rows/projects/scopes: reject binding mismatch despite valid authentication (tasks 2, 3).
- Interrupted/concurrent migration: preserve original records and references; rerun safely (task 4).
- Active/revoked credentials pinned by existing runs: preserve identity and enforce revocation before decrypt (tasks 3, 4).
- Old-code rollback/new-format writes: test documented restore/reader compatibility, never silently downgrade data (tasks 4, 5).

## File structure

- New `app/credential_crypto.py`: configuration-derived cipher, versioned format and authenticated row binding.
- Existing `app/settings.py`: validated environment configuration only; no request-local derivation.
- Existing `app/project_context.py` and `app/repository_validation.py`: credential creation/read integration while generic context crypto remains unchanged.
- New `app/credential_migration.py` and `scripts/reencrypt-credentials.py`: operator preflight, manifests and transactional migration.
- Existing Compose/launcher/docs files: explicit deployment settings, recovery instructions, examples with placeholders only.

### Task 1: Validate environment configuration

**Files:** `app/settings.py`, new `tests/test_credential_crypto.py`, settings fixture helpers.

**Interfaces:** add credential salt, active key ID and read-only salt map to AppSettings without changing encryption_key semantics. Tests construct explicit safe fixture settings.

- [ ] Write tests for valid values, missing/empty salt, invalid/noncanonical base64, wrong byte lengths, duplicate JSON keys, unknown/malformed IDs, active/old collisions and >8 historical entries. Assert errors omit inputs.
- [ ] Run the focused tests; observe failure for missing behavior.
- [ ] Implement startup validation with stable exact environment names above.
- [ ] Run focused tests and existing settings/runtime contracts; require green.
- [ ] Commit the configuration contract and tests.

### Task 2: Implement the credential cipher

**Files:** new `app/credential_crypto.py`, `tests/test_credential_crypto.py`.

**Interfaces:** CredentialBinding, CredentialCipher.seal/open/inspect as defined above.

- [ ] Write known-answer HKDF tests using an independently fixed expected key; round-trip Unicode/empty-string bounds, probabilistic tokens, wrong salt/root, tampered headers/token, unknown formats and identity binding rejection tests.
- [ ] Add legacy bare-token read tests and assert malformed new envelopes never invoke the legacy reader.
- [ ] Observe red, implement with installed HKDF/Fernet primitives, then require all tests green. No home-grown cipher or dependency upgrade.
- [ ] Confirm serialized results contain no plaintext or derived key material; commit.

### Task 3: Integrate every credential consumer

**Files:** `app/project_context.py`, `app/repository_validation.py`, `tests/test_project_secrets.py`, repository validation/assignment tests.

**Interfaces:** preserve resolve_secret authorization contract; route only credential storage through CredentialCipher. Allocate SecretVersion.id before seal. Keep generic encrypt/decrypt unchanged.

- [ ] Add API tests for encrypted new saves, metadata-only responses, validation/use of old and new credentials, rotation and revocation, foreign project/repository denial and ciphertext swapping.
- [ ] Assert immutable existing context/run ciphertext hashes and IDs remain unchanged.
- [ ] Observe red; implement creation and every read path, then pass tests and scan all SecretVersion ciphertext consumers.
- [ ] Commit integration and tests.

### Task 4: Add explicit re-encryption and salt rotation

**Files:** new `app/credential_migration.py`, `scripts/reencrypt-credentials.py`, `tests/test_credential_migration.py`.

**Interfaces:** inspect returns identities/formats/key IDs/counts and source fingerprints, never values. Apply requires an unchanged manifest and verified backup; uses active key while retaining IDs and metadata.

- [ ] Test mixed legacy/current/previous-key records, revoked versions, unknown key IDs, missing keys, concurrent row changes, failure on a middle record, successful rerun and already-migrated idempotence using PostgreSQL.
- [ ] Observe red, then implement offline transactional migration with row locking and count checks. Migration covers all credential versions, including revoked versions needed for historical integrity; it does not re-authorize them.
- [ ] Require quiescent writers and a restore-tested backup before apply. Verify every candidate decrypts before mutation; abort the transaction if any decrypt/binding check fails. Add redacted audit counts.
- [ ] Compare preservation metadata and credential usability without printing plaintext. Document reader-compatible rollback versus full backup restore; commit.

### Task 5: Wire deployment, readiness and recovery

**Files:** `compose.control-plane.yaml`, `scripts/start-control-plane.sh`, deployment shell fixture tests, `app/main.py` readiness/lifespan, operator docs.

**Interfaces:** all trusted control services receive identical required settings; workers receive none. Initialization writes missing salt once only for an explicitly authorized setup operation.

- [ ] Test fresh environment creation, existing root-key preservation, salt/key-ID persistence across recreation, invalid/missing settings, stale key IDs and unknown stored keys. Assert secrets are absent from output.
- [ ] Add readiness tests for wrong active salt under a used ID and old-key availability; implement startup verification using stored binding-aware samples.
- [ ] Test active key-ID rotation with old salts, restart and decrypt after restore; test rejection when old keys are prematurely removed.
- [ ] Update backup/restore instructions and the configuration table; commit after shell/Compose/API contracts pass.

### Task 6: End-to-end qualification and review

**Files:** existing project release fixtures plus new credential migration E2E scenario; operator runbook.

- [ ] In isolated PostgreSQL, create a project/repository credential through the API, verify database ciphertext has no token, use a fake provider adapter through normal assignment delivery, then restart services and repeat.
- [ ] Seed a legacy credential pinned by a run; migrate, rotate the salt/key ID, restart and prove the same credential version is usable. Revoke it and prove denial. Preserve project/run/secret IDs and unrelated ciphertext hashes.
- [ ] Verify browser/API/model-history/log/audit outputs omit all fixture secrets and salt/key values. The cipher does not remove the need for existing transcript redaction.
- [ ] Run backend unit tests, PostgreSQL contracts, Compose tests and docs build/check. Run relevant project execution E2E fixtures without external writes or paid inference.
- [ ] Obtain an independent security/code review, fix findings with failing regressions, then open an implementation PR. Do not apply to the live stack until backup, restore and rollback checks are concrete and verified.

## Sources and review handoff

- [cryptography HKDF documentation](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf): keyed derivation from strong material, salt and purpose information; not password storage.
- [cryptography Fernet documentation](https://cryptography.io/en/latest/fernet/): authenticated reversible encryption, secret 32-byte key and rotation support.

This plan intentionally leaves live configuration and credentials untouched. Review
the proposed environment contract and credential-only scope before implementation.
It does not propose exposing any encryption setting in the browser or asking users
to resubmit existing credentials.
