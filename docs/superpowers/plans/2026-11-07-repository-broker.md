# Repository operation broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native inline implementation, or superpowers:subagent-driven-development if explicitly selected. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run built-in repository operations without giving protected agents source-provider credentials.

**Architecture:** A trusted broker resolves provider credentials and enforces typed, execution-owned operations. GitHub/GitLab adapters retain their resource-claim and replay semantics; no generic authenticated HTTP/Git tunnel is exposed.

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

## Dependency and authority contract

Depends on A and B2. Reuse `CapabilityScope` and the capability ledger; do not build a second token service. A repository capability has audience `repository`, a pinned provider/repository revision, allowed operation set and claimed resource/ref constraints. Public requests never accept auth headers, arbitrary remote URLs or a credential ID chosen by the sandbox. Requests reference a server-pinned session and use unique `request_id` values.

Create `worker/factory/domain/repository_operations.py` with strict request/result types:
`RepositoryReadRequest(request_id, session_id, operation, resource_id, pinned_commit)`;
`PreparedPatchRequest(request_id, session_id, base_commit, files, expected_head)`;
`ReviewSubmissionRequest(request_id, session_id, resource_id, findings, expected_head)`;
`RepositoryOperationResult(operation_id, state, artifact_refs, resource_identity)`.
Use canonical existing repository identities and prepared-change types rather than duplicating provider URL parsing. Allowed operations are compiled from built-in needs, not supplied freely by callers. The initial catalog includes issue/PR/MR context, source snapshot/read, prepared patch publication, review/comment submission and authorized PR/MR create/update. Workflow label/status/check/merge operations remain trusted coordinator operations and use the same authorization/recovery rules; the agent cannot enable them by naming an operation.

### Task C1: Broker pinned repository reads

**Files:**
- Create: `worker/factory/domain/repository_operations.py`, `control-plane/backend/app/repository_broker.py`
- Modify: `worker/factory/application/ports.py`, `control-plane/backend/app/main.py`
- Test: `control-plane/backend/tests/test_repository_broker.py`

**Interfaces:** Produce `execute_repository_read(db, bearer: str, request: RepositoryReadRequest) -> RepositoryOperationResult`; consume B2 capability verification. Results contain bounded source artifacts/context, never provider token, secret-bearing URL or credential-helper configuration.

- [ ] **Step 1: Add failing behavior tests.** `test_cross_project_or_unpinned_repository_denied`, `test_source_read_uses_exact_commit`, `test_symlink_traversal_and_oversized_archive_denied`, `test_remote_url_and_auth_header_injection_rejected`, `test_wrong_audience_and_revoked_grant_denied` run through actual handlers with GitHub/GitLab fixture adapters. Scan responses/artifacts for distinctive fixture credentials.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Inventory existing built-in repository operations and map each to a typed command. Resolve the pinned source credential only inside the broker. Reuse existing archive/path validation, provider identities and context grants; stream artifacts with bounded size, ownership and retention. No credential-bearing clone URL leaves the service.
- [ ] **Step 4: Verify.** Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_repository_broker.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Document the operation catalog and artifact limits in `docs/project-runtime-api.md`; add the repository boundary to `docs/openbao.md`. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: broker pinned repository reads without credential delivery`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task C2: Publish changes and reviews through durable claimed operations

**Files:**
- Create: `control-plane/backend/app/repository_publication.py`
- Modify: `app/runtime_claims.py`, `app/workflow_state.py`, `app/database.py` under `control-plane/backend/` as required
- Modify: existing GitHub/GitLab application adapters without relocating unrelated code
- Test: `control-plane/backend/tests/test_repository_publication.py`

**Interfaces:** Produce `publish_prepared_patch(db, bearer: str, request: PreparedPatchRequest) -> RepositoryOperationResult` and `submit_repository_review(db, bearer: str, request: ReviewSubmissionRequest) -> RepositoryOperationResult`. Operation IDs and request idempotency use existing canonical resource claims; unresolved remote writes remain reconcilable.

- [ ] **Step 1: Add failing behavior tests.** `test_wrong_ref_and_force_update_denied`, `test_changed_base_or_pr_head_rejected`, `test_lost_publish_response_reconciles_without_duplicate_commit`, `test_concurrent_project_claim_prevents_overlap`, `test_cancel_before_publish_denied`, `test_cancel_after_remote_acceptance_preserves_uncertain_receipt` run for both providers using deterministic failure injection. Assert safe patch paths and bounded comment/review content.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Retain prepared plan/cursor persistence and non-forced writes. Check active authority before the externally effective operation, fence stale attempts and record the uncertainty boundary. Reconcile remote markers/heads before retry. Do not hold global execution locks across network I/O or assume a transaction makes external writes atomic.
- [ ] **Step 4: Verify.** Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_repository_publication.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Update `docs/project-runtime-operations.md` with uncertain-operation reconciliation and cancellation limits; preserve existing issue/PR/MR safety descriptions. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: broker replay-safe repository publication and reviews`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task C3: Wire built-in stages and complete repository broker qualification

**Files:**
- Modify: `worker/factory/workflows/actions.py`, `worker/factory/bootstrap.py`, built-in application operation constructors
- Create: `worker/factory/adapters/repository_broker_client.py`, `tests/test_repository_broker_client.py`
- Modify: `control-plane/backend/tests/test_project_release.py`, `test_gitlab_project_release.py`
- Create: `tests/integration/test_brokered_repository_workflow.py`

**Interfaces:** Produce `RepositoryBrokerClient(endpoint, capability_provider).read(request)`, `.publish(request)` and `.review(request)` implementing the typed application port. Direct mode retains existing provider adapters; protected construction refuses a provider credential argument and cannot select them.

- [ ] **Step 1: Add failing behavior tests.** `test_protected_stage_constructs_only_broker_adapter`, `test_generated_code_cannot_select_source_credentials`, `test_hello_world_issue_to_patch_to_review_without_source_token`, `test_gitlab_equivalent_brokered_flow`, `test_followup_revalidates_current_minimum` cover complete application flows. Real fixture repository services verify publication, label/status changes and replay; default CI never mutates a user repository.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Inject repository operations through the existing bootstrap seam; remove protected code paths that expect secret values, not direct functionality. Preserve readable stage outputs and artifact links. Finish each built-in operation catalog entry before qualifying its workflow template; unsupported templates remain unavailable in protected mode.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests/integration -p test_brokered_repository_workflow.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Add a workflow compatibility matrix and an operator walkthrough distinguishing source data access from provider-token access. Attach repository-provider fixture evidence; live smoke tests remain separately authorized. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: route protected built-in stages through repository broker`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.


## Public-behavior test shape

Illustrative assertion shape for the owning tests; fixtures/errors are defined in that task, not existing implementation claims.

```python
with self.assertRaises(RepositoryOperationDenied):
    execute_repository_read(db, project_a_bearer, project_b_request)
provider.read.assert_not_called()
```
