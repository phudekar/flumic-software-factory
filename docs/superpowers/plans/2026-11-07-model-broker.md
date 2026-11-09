# Scoped model gateway sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native inline implementation, or superpowers:subagent-driven-development if explicitly selected. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide replaceable gateway inference with stage-scoped identities and enforceable request/token limits.

**Architecture:** Reuse the existing gateway backlog and transport design. A trusted model-session facade checks live assignment authority and calls an external gateway; direct gateway routing can ship before protected sandbox support.

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

## Dependency and interface contract

Depends on A1/A2. Reconcile existing issues #39, #41–#45 and the existing gateway plan before implementation; preserve its connection, token-counting, protocol and attribution requirements. Do not create duplicates.

`worker/factory/domain/broker.py` defines `CapabilityScope(project_id, run_id, execution_id, assignment_id, attempt, audience, operations, expires_at)` and `SessionLimits(max_requests, max_concurrent, max_input_tokens, max_output_tokens)` as strict frozen models. Audience is exactly `model`, `repository` or `mcp:<approved-server-id>`; an MCP identity can invoke only explicitly delegated repository/model operations, never become a generic model identity. The server stores hashes of opaque random bearer tokens, immutable scope, remaining allowance and revocation state. Session identifiers are public metadata; plaintext capabilities are excluded from snapshots/logs/history and delivered only over authenticated private channels. Proposed lifetime: at most five minutes and never beyond the assignment lease; renew only through the active trusted supervisor. Recheck authority transactionally before each request reservation.

### Task B1: Extract model transport and qualify gateway protocols

**Files:**
- Create: `worker/factory/application/model_sessions.py`, `worker/factory/adapters/gateway_inference.py`
- Modify: `worker/model_client.py`, `worker/factory/bootstrap.py`, `worker/factory/adapters/token_budget.py`
- Create: `tests/test_model_sessions.py`, `tests/integration/gateway_fixture.py`

**Interfaces:** Produce `ModelTransport.infer(request: ModelRequest) -> ModelResponse` and `assess_tokens(request: ModelRequest) -> TokenAssessment` in `model_sessions.py`; types include explicit protocol/model alias, structured schema, maximum output, request ID, usage source and count provenance. Direct and gateway adapters implement the same semantic suite.

- [x] **Step 1: Add failing behavior tests.** `test_direct_task_outputs_unchanged`, `test_unknown_alias_without_qualified_counter_denied`, `test_gateway_schema_failure_is_not_provider_fallback`, `test_redirect_and_unapproved_endpoint_denied`, `test_uncertain_timeout_not_retried` cover JSON/error/count compatibility. Use a real pinned LiteLLM container against a deterministic fake upstream; no paid calls.
- [x] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [x] **Step 3: Implement the owned behavior.** Separate prompt/schema/task behavior from transport and retry ownership. Reuse the existing gateway plan connection/descriptor model instead of introducing competing route records. Record exact image digest, protocol features and OSS capability report in fixture qualification output.
- [x] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests -p test_model_sessions.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [x] **Step 5: Document and review.** Update `docs/configuration-workers.md`, gateway route examples and the existing gateway capability matrix. Add an explicit opt-in real gateway fixture command. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `refactor: introduce qualified model session transports`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task B2: Transactional scoped capability and allowance ledger

**Files:**
- Create: `worker/factory/domain/broker.py`, `control-plane/backend/app/broker_capabilities.py`
- Modify: `control-plane/backend/app/database.py`, next additive migration, cancellation transitions in `workflow_runs.py`
- Test: `control-plane/backend/tests/test_broker_capabilities.py`

**Interfaces:** Produce `issue_capability(db, scope: CapabilityScope, limits: SessionLimits) -> str`, `reserve_request(db, bearer: str, audience: str, request_id: str, input_tokens: int, output_limit: int) -> Reservation`, `settle_request(db, reservation_id: str, actual_usage: dict | None, outcome: Literal["completed", "rejected", "uncertain"]) -> None`, `revoke_assignment(db, assignment_id: str) -> None`. Reservation records identify scope, logical request, attempt, reserved allowance and outcome.

- [ ] **Step 1: Add failing behavior tests.** `test_wrong_project_stage_attempt_audience_denied`, `test_expiry_cannot_exceed_lease`, `test_concurrent_reservations_cannot_overspend`, `test_revocation_fences_new_requests`, `test_duplicate_request_does_not_double_dispatch`, `test_unknown_outcome_retains_reservation` run against real disposable PostgreSQL with parallel sessions. Verify only token hashes persist.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Use transactional row locks/constraints for quota and idempotency, not process-local counters. Mark external dispatch before sending so response loss remains uncertain. Renew by issuing a new bounded capability only after active assignment checks; revoke old capability atomically and reconcile in-flight reservations independently.
- [ ] **Step 4: Verify.** Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_broker_capabilities.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Document five-minute maximum lifetime, live cancellation semantics and absence of guaranteed hard USD ceiling. Explain that stolen sandbox identities retain only permitted operations. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: add scoped broker capabilities and request reservations`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task B3: Trusted model-session facade and generic gateway management

**Files:**
- Create: `control-plane/backend/app/model_broker.py`, `gateway_connections.py`, `adapters/litellm_management.py`
- Modify: `app/main.py`, `credential_store.py`, `history.py` under `control-plane/backend/`
- Test: `control-plane/backend/tests/test_model_broker.py`

**Interfaces:** Consume B1/B2. Produce `execute_model_request(db, bearer: str, request: ModelRequest) -> ModelResponse` and application-owned `GatewayAdministration.verify(connection) -> ConnectionCapabilities`. Keep management and inference credentials distinct; gateway administration credentials never leave trusted services.

- [ ] **Step 1: Add failing behavior tests.** `test_gateway_admin_key_never_in_response`, `test_unapproved_model_or_route_denied`, `test_cancel_before_reservation_rejects`, `test_broker_outage_has_no_direct_fallback`, `test_wrong_token_audience_denied`, `test_max_output_and_body_size_enforced`, `test_model_reply_prompt_and_capability_redacted` exercise real ASGI handlers and fake gateway. Include 401/403/429/5xx, streaming cancellation and uncertain charged timeout semantics.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Resolve pinned connection bindings and authorized inference identity from OpenBao in trusted code. Enforce model aliases and bounded payloads before transport. Disable automatic upstream retries initially. Keep usage ingestion idempotent and parent/attempt observations distinct. Implement direct-mode gateway adoption without declaring protected support.
- [ ] **Step 4: Verify.** Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_model_broker.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Update `docs/openbao.md`, `docs/project-observability.md` and Compose examples, including gateway key custody/rotation and generic adapter limitations. Do not assume LiteLLM supports OpenBao integration without a qualified adapter. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: broker stage-scoped model inference through approved gateways`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task B4: Project routes, accounting and API-first gateway E2E

**Files:**
- Create: `control-plane/frontend/src/features/model-routes.tsx`, `model-routes.test.tsx`
- Modify: existing project config and model-call views; `docs/roadmap/2026-10-30-llm-gateway-test-strategy.md`
- Create: `tests/integration/test_gateway_workflow.py`; update `.github/workflows/tests.yml`

**Interfaces:** Consume pinned route descriptors from B1/B3 and B2 reservation outcomes. E2E joins browser-selected route, admission, worker request, real LiteLLM/fake upstream and canonical project/run/stage/attempt usage records.

- [ ] **Step 1: Add failing behavior tests.** `test_two_project_routes_isolate_credentials_and_usage`, `test_replayed_observation_not_double_counted`, `test_ui_shows_unknown_cost_and_uncertain_call`, `test_connection_rotation_preserves_pins_and_revocation`, `test_gateway_restart_reconciles_uncertain_attempt` run in disposable fixtures. Assert budgets are labeled according to actual guarantees.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Add guided route forms, administrator approval/import/verification and project bindings from the existing gateway plan. Gate relevant PRs on deterministic E2E; retain fixture logs containing metadata only. Compare existing direct mode outputs without broad unrelated refactors.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests/integration -p test_gateway_workflow.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Publish a direct-mode gateway walkthrough with real fixture screenshots, exact startup commands and supported capability matrix. Run frontend tests, `just ui-build` and `just docs-check`. Protected launch remains blocked until D2. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: qualify project gateway routes and usage accounting`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.


## Public-behavior test shape

Illustrative assertion shape for the owning tests; fixtures/errors are defined in that task, not existing implementation claims.

```python
with self.assertRaises(CapabilityDenied):
    reserve_request(db, repository_bearer, "model", "req-1", 10, 20)
assert db.query(ReservationRecord).count() == 0
```
