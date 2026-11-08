# Protected Execution Delivery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native inline work, or superpowers:subagent-driven-development if explicitly chosen. Track tasks with checkboxes.

**Goal:** Add protected execution alongside direct execution without exposing upstream credentials to agent/tool sandboxes.

**Architecture:** Pin an explicit mode at admission. Trusted gateway/repository brokers authorize scoped requests; protected supervisors manage isolated sandboxes and keep provider/control credentials outside them.

**Tech Stack:** Existing Python/FastAPI/SQLAlchemy, React/Vitest, OpenBao, external LiteLLM-first generic gateway, Docker and separately qualified native Kubernetes adapters.

**Spec:** [Approved design](../../roadmap/2026-11-07-protected-execution-design.md).

Date: 2026-11-07. **Implementation in progress.** The user approved the design and implementation plan. Native inline execution is underway. OpenBao prerequisite PR #68 and A1 PR #69 (#58) are merged; A2 (#59) is active. Gateway work reuses #39/#41–#45.

## Global Constraints

All four plans carry the same constraints. Preserve direct behavior and existing data. Protected execution requires qualified capabilities, dedicated capacity, upstream-credential-free agent/tool environments and no direct fallback. No live local rollout, provider-key migration, paid call or user-repository test is part of planning. Keep the approved policy-change and cancellation semantics.

## Review Focus

Cross-plan ownership and token audiences; direct/old-worker wire compatibility; uncertain external effects and cost; real sandbox/network isolation; policy/qualification changes after admission. Each is assigned a negative/lifecycle test in its owning plan.

## Scope and order

| Order | Plan | Tasks | Independently useful result |
| --- | --- | --- | --- |
| 1 | [Execution modes](2026-11-07-execution-modes.md) | A1–A3 | Versioned policy, immutable pins and guided readiness; protected unavailable until qualified |
| 2 | [Model broker](2026-11-07-model-broker.md) | B1–B4 | Generic gateway sessions, scoped identities, request/token limits and accounting; direct-mode gateway adoption can ship |
| 3 | [Repository broker](2026-11-07-repository-broker.md) | C1–C3 | Credential-free source operations with existing publication/replay safeguards |
| 4 | [Sandbox and MCP](2026-11-07-sandbox-mcp.md) | D1–D4 | Qualified protected SDK release, then independently qualified CLI/MCP and Kubernetes |

Fourteen reviewable tasks, one PR at a time. Avoid implementing B2 and a parallel capability mechanism in C/D. D2 is the first protected SDK release gate; D3 and D4 enable only the adapter/target combinations they independently prove. Protected-capability flags must never be placeholders.

## Preparation and backlog

- [x] Reconcile the current branch, origin/main, open PRs and existing gateway issues #39/#41–#45. Existing design baseline references are historical, not an instruction to resurrect old branches.
- [x] Land/rebase the existing OpenBao implementation and local-bootstrap changes in separate reviewable PRs before depending on them. Verify schema, context protocol and direct regression baseline; do not silently mix unrelated uncommitted files into new PRs.
- [x] Create or update issues only after backlog reconciliation, using A1–D4 identifiers and their acceptance tests. Do not duplicate the existing gateway epic; link repository/sandbox security work as dependencies.
- [ ] Record current unittest/frontend results and changed-module coverage. Pin deterministic fixture dependencies and confirm isolated Docker/cluster ownership before starting any test workloads.

## Cross-plan contract and coverage map

| Requirement | Owner / evidence |
| --- | --- |
| Project default/minimum, admin policy updates | A1 persistence/auth/conflict tests |
| Manual/trigger/replay/follow-up pins; no stage downgrade | A2 entry-point and cancellation tests |
| Guided mode/readiness, human-readable status | A3 UI and D2/D4 walkthrough tests |
| Generic replaceable gateway transport/count semantics | B1 adapter conformance; B4 real gateway/fake upstream |
| Audience/lease/attempt scope, revocation and atomic limits | B2 PostgreSQL race tests; B3 handler denial tests |
| Canonical usage, uncertainty and cost coverage | B3/B4 request/attempt/observation tests |
| Pinned source, claimed non-forced publication, provider parity | C1/C2 provider fixtures; C3 workflow E2E |
| No provider/control keys in agent or tools | D1 malicious real-container inspection; D2 complete protected workflow |
| Dedicated fleet, target digest/expiry and lifecycle recovery | A2 qualification guards; D1/D2 crash/cancel tests |
| CLI protocol/auth compatibility; isolated local/remote MCP | D3 real binary/server fixtures |
| Enforcing Kubernetes controls and recovery | D4 independently owned real cluster qualification |
| Limits/diagrams/configuration/screenshots/rollback | Documentation acceptance in every task; final D4 operator gate |

## Verification and integration discipline

Commands in each plan assume a checked-out `.venv/bin/python`; set `FLUMIC_PYTHON` for `just` if using another environment. These are commands to run during implementation, not evidence that unimplemented tests exist or pass today.

For each task: add the behavior test, observe red, implement the narrow change, verify green, inspect coverage and direct regressions, review, commit, open PR, monitor CI, fix failures, merge, then close the linked issue. Broaden tests when touched seams or failures justify it; do not write tests that only mirror internal function layout.

Mandatory deterministic CI should cover unit/handler/UI suites plus real gateway/fake upstream and malicious sandbox fixtures on qualified runners. Real provider/repository smoke tests remain opt-in with bounded cost and separate authorization. An unavailable runtime is a visible unsupported qualification state, not a silent success/skip.

Coverage reports must include changed security branches and an explicit uncovered-branch review. No blanket “all code covered” claim from green suites or a single percentage. Test actual source/control-token absence using distinctive seeded fixture values across environments, /proc, files, output capture, history and tool inheritance.

## Self-review record

- Requirements mapped above; no protected release before B/C/D prerequisites.
- A defines mode/qualification types; B owns shared identity/reservation types; C owns repository requests; D owns sandbox lifecycle. Later plans consume these names rather than redefining them.
- Ordinary policy tightening preserves admitted pins; emergency cancellation is separate. Follow-ups and replay attempts resolve current minimums before admission.
- Gateway-dollar enforcement is not claimed; stage request/concurrency/token reservations are hard limits only on qualified protocols.
- Chosen Docker implementation uses network-none plus private broker socket, avoiding an unproven bridge egress rule. Dependency-download/tool limitations remain explicit.
- A1 policy/guard tests and PostgreSQL migration/concurrency tests have run; later tasks remain unimplemented. No live service or credential changed by this plan.
