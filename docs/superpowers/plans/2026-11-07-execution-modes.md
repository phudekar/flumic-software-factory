# Execution modes and admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native inline implementation, or superpowers:subagent-driven-development if explicitly selected. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist explicit direct/protected policy and enforce immutable, capability-gated launch semantics.

**Architecture:** Centralize mode resolution and target qualification. Existing direct wire behavior stays compatible; protected admission remains disabled until the later runtime gate is qualified.

**Tech Stack:** Python, FastAPI, SQLAlchemy/PostgreSQL, Pydantic, React/Vitest, Docker Compose; Kubernetes where qualified.

**Spec:** [Approved direct/protected design](../../roadmap/2026-11-07-protected-execution-design.md). Read the [delivery index](2026-11-07-protected-execution.md) first.

**Status:** Plan only. No tasks completed. Proposed interfaces below are new unless identified as existing.

## Global Constraints

- Existing projects migrate to `direct`. No live stack changes or credential migration during implementation without explicit rollout authorization.
- Protected execution never receives upstream provider keys, OpenBao wrapping tokens for those keys, worker registration tokens or controller credentials.
- No automatic direct fallback; unsupported routes/targets fail admission. No per-stage downgrade in version one.
- Pin effective mode/policy/routes/target/tool policy. Ordinary policy changes affect new admissions; explicit emergency cancellation revokes outstanding authority.
- Bound requests, concurrency and qualified input/output tokens; do not promise a hard per-stage USD ceiling. Cancellation cannot undo an accepted upstream request.
- Preserve GitHub/GitLab resource claims, non-forced writes, replay recovery and existing run history.
- Native inline execution is the recommended default. One reviewed PR per task, wait for required CI, then merge and close its linked issue; do not close issues on commit alone.
- Use existing Python unittest, FastAPI/SQLAlchemy/Pydantic, React/Vitest and Docker/Kubernetes adapters. Qualify exact dependency/image/CLI versions; no invented supported-version promises.

## Review Focus

1. Old clients/workers reject extra wire fields: test omitted-mode compatibility and explicit protocol/capability gating.
2. Concurrent cancellation/revocation and in-flight requests: test fencing, repeat acknowledgments and no reuse of expired capabilities.
3. Cross-project, audience, repository and stage substitution: test ownership at each broker boundary.
4. Lost responses and retries may have external effects/costs: retain uncertain reservations and reconcile, never retry blindly.
5. Hidden environment/home/network inheritance defeats credential isolation: test real subprocesses and runtimes, not only mocks.

## Shared types and persistence

Create `worker/factory/domain/execution.py` with strict frozen Pydantic types:
`ExecutionMode = Literal["direct", "protected"]`;
`ExecutionPolicy(revision_id: str, default_mode: ExecutionMode, minimum_mode: ExecutionMode)`;
`ExecutionPin(mode: ExecutionMode, policy_revision_id: str, route_revision_id: str | None, target_qualification_id: str | None, tool_policy_revision_id: str | None)`;
`TargetQualification(id: str, target_id: str, runtime_digest: str, capabilities: frozenset[str], expires_at: float)`.
All fields needed by protected mode must be nonempty before admission. Qualification is trusted operator/test attestation, not an arbitrary worker self-report. Register attestations through deployment-admin authorization; bind them to image/runtime/policy digests and recheck at dispatch.

Use append-only `ExecutionPolicyRevision` project-owned rows and a current-policy reference; persist `ExecutionPin` in the existing immutable launch snapshot. Migration defaults old projects/runs to direct and does not rewrite historical encrypted snapshots. Old worker contexts omit new fields for direct mode. Never allocate a fixed migration number before rebasing onto the current head.

### Task A1: Version project execution policy

**Files:**
- Create: `worker/factory/domain/execution.py`, `control-plane/backend/app/execution_policy.py`
- Modify: `control-plane/backend/app/database.py`, `control-plane/backend/app/projects.py`
- Create: next Alembic migration in `control-plane/backend/migrations/versions/`
- Test: `control-plane/backend/tests/test_execution_policy.py`

**Interfaces:** Produce `resolve_execution_pin(policy: ExecutionPolicy, requested: ExecutionMode | None, parent_minimum: ExecutionMode | None) -> ExecutionPin` (unresolved connection/target fields are None until admission fills them) and `update_execution_policy(db, scope, expected_revision: str, changes: dict) -> ExecutionPolicy`. Deployment-admin authorization is enforced at the HTTP handler before policy writes.

- [x] **Step 1: Add failing behavior tests.** `test_existing_project_defaults_direct` asserts old rows resolve direct; `test_minimum_protected_rejects_direct`, `test_default_cannot_weaken_minimum`, `test_policy_conflict_preserves_new_head`, and `test_non_admin_cannot_weaken_policy` assert denial without mutation. Test migration on real disposable PostgreSQL and legacy direct snapshots.
- [x] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [x] **Step 3: Implement the owned behavior.** Implement additive revision storage, optimistic head updates and audited project-policy endpoints using the actual existing authenticated administrator mechanism; inspect `app/main.py` before wiring authorization. Return counts of admitted direct runs that will remain authorized when policy tightens; never cancel them implicitly.
- [x] **Step 4: Verify.** Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_execution_policy.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [x] **Step 5: Document and review.** Update `docs/project-runtime-api.md` and `docs/configuration-control-plane.md`; explain admission-only policy updates versus explicit emergency cancellation. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: add versioned execution mode policy`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task A2: Pin policy across every launch and fence protected delivery

**Files:**
- Modify: `app/workflow_runs.py`, `app/workflow_definitions.py`, `app/trigger_runtime.py`, `app/followups.py` under `control-plane/backend/`
- Modify: `control-plane/backend/app/project_context.py`, `worker_pools.py`, `runtime_router.py`
- Modify: `worker/factory/workflows/contracts.py`
- Create: `control-plane/backend/app/execution_admission.py`
- Test: `control-plane/backend/tests/test_execution_admission.py`

**Interfaces:** Consume A1 types. Produce `admit_execution(db, scope, launch, requested_mode: ExecutionMode | None, parent_pin: ExecutionPin | None) -> ExecutionPin` and `require_target_qualification(pin: ExecutionPin, target: TargetQualification, now: float) -> None`. Use existing transition locks/ownership contracts; protected-capability readiness is false until Plan D.

- [ ] **Step 1: Add failing behavior tests.** `test_manual_trigger_and_followup_share_policy`, `test_replay_does_not_downgrade`, `test_queued_direct_run_keeps_pin_after_policy_change`, `test_followup_applies_current_minimum`, `test_tampered_or_stale_qualification_denied` cover all entry points. `test_protected_context_never_wraps_or_reads_provider_values` spies on the real resolver and asserts no secret-store calls, plaintext or wrapping tokens; old direct context serialization remains unchanged.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Pin effective policy into admission/outbox/snapshot before dispatch; maintain project ownership. Protected context returns only public configuration and a supervisor-owned broker session reference, never a serializable provider credential grant. Protected callers on the old shared-worker context protocol are rejected; do not simply add a field old clients reject. Explicit cancellation invalidates broker-session authority once B2 exists; until then protected runs cannot dispatch.
- [ ] **Step 4: Verify.** Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_execution_admission.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Document unsupported-capability error codes, direct compatibility and mode visibility in run inspection. Preserve existing default-project runs unchanged. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: enforce execution mode admission and delivery boundaries`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task A3: Guided mode configuration and readiness UX

**Files:**
- Modify: `control-plane/frontend/src/features/project-config-editor.tsx`, `workflow-definitions.tsx`, `project-run-inspector.tsx`, `pool-create-form.tsx`
- Create: `control-plane/frontend/src/features/execution-mode-form.tsx`, `execution-mode-form.test.tsx`
- Modify: `control-plane/frontend/src/workflow-api.ts`

**Interfaces:** Consume A1 policy read/update responses and A2 readiness/error responses. Produce a guided mode selector showing resolved mode, minimum, required route/target and qualification failures; launch sends optional `execution_mode` and preserves omitted-mode direct compatibility.

- [ ] **Step 1: Add failing behavior tests.** `test_explicit_direct_and_protected_choices`, `test_unready_protected_mode_explains_blocker`, `test_minimum_prevents_downgrade`, `test_conflict_keeps_unsaved_edit`, `test_policy_change_shows_existing_run_count` assert real API response handling; run view shows the pinned mode and user-readable timestamps, not current project defaults.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Add grouped controls and route-readiness summaries; no YAML-only security switch. Require an explicit administrator action for emergency cancellation, with scope/count preview. UI disabling is advisory; server tests remain authoritative.
- [ ] **Step 4: Verify.** Run `npm test --prefix control-plane/frontend -- execution-mode-form.test.tsx`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Add screenshots from deterministic fixtures to `docs/user-walkthrough.md`; label protected execution unavailable until the release gates actually pass. Run `just ui-build` and `just docs-check`. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: guide execution mode selection and readiness`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.


## Public-behavior test shape

Illustrative assertion shape for the owning tests; fixtures/errors are defined in that task, not existing implementation claims.

```python
pin = resolve_execution_pin(policy=direct_policy, requested=None, parent_minimum=None)
assert pin.mode == "direct"
with self.assertRaises(ExecutionPolicyError):
    resolve_execution_pin(protected_minimum_policy, "direct", None)
```
