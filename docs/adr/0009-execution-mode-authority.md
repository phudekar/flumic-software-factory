# ADR 0009: Version execution policy and pin each logical run

- Status: Accepted; policy foundation merged in PR #69. Protected broker/sandbox execution remains unavailable.
- Date: 2026-11-07
- Related: [ADR 0003](0003-postgresql-runtime-authority.md), [ADR 0008](0008-openbao-credential-authority.md)

## Context

OpenBao protects credential storage and one-use delivery, but a direct worker still
unwraps provider values in its execution process. Supporting stronger isolation
must remain a deliberate choice without breaking direct SDK/CLI integrations.
Current configuration and historical run intent must not be conflated when policy
changes, requests are retried, or follow-up work is created.

## Decision

Keep explicit `direct` and `protected` modes. Projects have versioned default and
minimum policies owned by that project, updated by the authenticated deployment
administrator using an expected revision. A stable implicit `direct-v1` policy
preserves projects without explicit history. A separate owned head table advances
atomically and avoids a foreign-key cycle on the projects table.

Resolve new logical admissions against current policy and pin their effective
mode and authority in an encrypted launch snapshot. Existing accepted runs keep
their pins, including queued work and later stages. Repeated acknowledgments are
not new admissions. A new logical replay retains its original mode but must pass
current minimum policy; a new follow-up also inherits at least its parent's
protected minimum. Shared template or historical snapshots must be cloned rather
than rewritten for a new admission. Old snapshots without mode metadata describe
direct execution.

The launch snapshot is authority. A nullable run-mode projection supports efficient
operator counts; historical NULL values mean direct without rewriting ciphertext.
This projection does not authorize credential delivery. Worker context identity
and pinned mode must be checked before a credential store or wrapping callback is
invoked. Direct-mode context serialization keeps the old worker wire shape.

A saved policy, a gateway endpoint or a target attestation alone does not enable
protected execution. Unsupported protected admissions and delivery fail closed.
Target evidence must bind the runtime configuration, capabilities and expiry;
actual broker/supervisor/sandbox support must be separately installed and qualified.
Current policy APIs report protected execution unavailable.

## Alternatives

Requiring protected execution everywhere would remove supported direct paths.
Automatically falling back to direct would silently defeat the selected boundary.
Reusing a mutable project default at each stage would change the meaning of
already accepted work. These alternatives are rejected.

## Consequences

Policy changes apply to new admissions, not automatic termination of existing
runs. Emergency cancellation/revocation is an explicit action, and cannot retract
an already accepted provider request. Administrator policy changes and immutable
revisions provide evidence of intent; they do not prove process isolation.

Downgrade must not erase historical protected policies or runs. Changing a current
policy back to direct does not delete that history. Operators need verified backup
and rollback procedures. Brokered model/repository access and isolated MCP/runtime
qualification remain separate implementation slices; direct workers retain the
memory-exposure limits documented in the [OpenBao guide](../openbao.md#security-boundaries-and-compromised-workers).
