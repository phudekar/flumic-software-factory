# Protected sandbox, CLI and MCP qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native inline implementation, or superpowers:subagent-driven-development if explicitly selected. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable protected execution only on runtimes proving credential, process and network isolation.

**Architecture:** Separate the trusted supervisor from disposable agent sandboxes. Start with a deliberately narrow Docker SDK target, then qualify CLI/MCP and Kubernetes independently; no shared direct/protected fleet or capability self-attestation.

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

## Dependencies, runtime choice and interfaces

Depends on A, B and C. Use existing job lifecycle, resource claims and runtime target fencing behind focused sandbox adapters; do not turn the agent into a job broker.

Create `worker/factory/domain/sandboxes.py` with `SandboxSpec(execution_pin, image_digest, workspace_artifact, limits, session_refs, tool_policy_revision_id)`, `SandboxHandle(id, target_id, qualification_id)` and `SandboxResult(state, output_artifact, usage_refs)`. `session_refs` are public handles; capabilities are handed over separately through private ephemeral channels and never persisted in public records. `SandboxRuntime.start(spec: SandboxSpec) -> SandboxHandle`, `.inspect(handle) -> SandboxResult | None`, `.cancel(handle) -> None`, `.collect(handle) -> SandboxResult` are application-owned ports.

**Initial Docker implementation choice:** `network_mode=none` for agent containers. Expose a dedicated per-action Unix socket for typed model/repository broker requests, with a small credential-free loopback relay inside the sandbox where SDK/CLI HTTP compatibility requires it. The relay has no arbitrary forwarding, CONNECT or destination parameter. The trusted broker owns provider connectivity; the agent has no IP route, DNS resolver access, host-gateway route or general internet access. The agent may invoke its socket directly, so broker authorization cannot trust the relay. Mount only that action's socket, not its parent service directory or control socket. Agent images and dependency artifacts must be prepared before launch; network-requiring build/install operations are unsupported until separately brokered and qualified.

The supervisor retains Hatchet/worker registration, assignment lease, completion and sandbox-management authority. It does not execute generated code or launch MCP in its own process context. Broker capabilities delivered into the sandbox are distinct from supervisor credentials and bounded by active assignment authority. Provider keys never enter `SandboxSpec`, agent environment, homes, files or logs.

Unix socket behavior across Docker Desktop/Colima host/VM boundaries must be proven on the target. A target that cannot transport the private socket safely stays unqualified; do not replace `network_mode=none` with a permissive bridge as a workaround. A broker/supervisor shared internal volume can hold the socket in the runtime VM without host socket forwarding. Use per-action socket directories/ownership and separate mount views. Keep trusted network-enabled broker processes outside sandbox PID/user namespaces.

Kubernetes is a separate qualified adapter: use restricted pods and enforcing network policy, default-deny egress with explicit broker-only access, no ambient service-account mount or host namespaces. Per-action private channel/relay implementation may differ but must pass the same negative suite; do not infer equivalence from the Docker test report.

### Task D1: Credential-free Docker sandbox adapter and adversarial qualification

**Files:**
- Create: `worker/factory/domain/sandboxes.py`, `control-plane/backend/app/sandbox_runtime.py`, `adapters/docker_sandboxes.py`
- Create: `worker/factory/adapters/broker_socket_relay.py`, `tests/test_broker_socket_relay.py`
- Create: `tests/integration/test_protected_docker_sandbox.py`
- Modify: runtime target qualification storage from A

**Interfaces:** Consume B/C broker operations and A TargetQualification. Produce SandboxRuntime plus `relay_request(session_ref: str, audience: str, payload: dict) -> dict`; relay supports only qualified typed operations and forwards no ambient auth. Produce a signed or administrator-registered qualification report tied to target, image and policy digests, with expiry and test evidence.

- [ ] **Step 1: Add failing behavior tests.** `test_no_provider_or_supervisor_secret_in_env_home_mount_or_process`, `test_sandbox_cannot_connect_provider_openbao_dns_metadata_or_host`, `test_cannot_open_other_action_socket_or_workspace`, `test_no_privilege_escalation_or_core_dump`, `test_relay_rejects_connect_redirects_and_destinations` use real containers and malicious fixture code. Probe IPv4, IPv6, /proc, subprocess inheritance and filesystem paths. Run an allowed model/repository operation through the private channel as the positive control.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Launch non-root bounded containers with no IP networking, dropped capabilities, no-new-privileges, read-only root plus scoped scratch/workspace, PID/memory/CPU/time limits and seccomp. Disable core dumps. Build the Unix-socket broker channel and a minimal SDK-compatible loopback relay without provider keys. Cleanup must reclaim sockets/workspaces and record orphaned runtime IDs when cleanup is uncertain. Do not enable qualification if any test is skipped or fails.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests/integration -p test_protected_docker_sandbox.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Publish exact supported host/runtime/image digests and network-none limitations in `docs/deployment-compose.md`; explain that arbitrary dependency downloads are unavailable in the initial protected target. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: qualify isolated Docker agent sandboxes`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task D2: Trusted supervisor and protected SDK vertical release

**Files:**
- Modify: `worker/factory/shared_worker.py`, `worker/factory/workflows/contracts.py`, `control-plane/backend/app/worker_pools.py`
- Modify: `control-plane/backend/app/runtime_router.py`, `job_broker.py`, `workflow_runs.py`
- Create: `tests/integration/test_protected_execution.py`
- Modify: `.github/workflows/tests.yml`

**Interfaces:** Consume SandboxRuntime and A/B/C contracts. Trusted supervisor obtains/renews broker sessions and manages lifecycle; sandbox receives only per-action scope. Protected context protocol is explicitly capability-gated. Admission readiness now requires current D1 qualification plus supported route, built-in operation catalog and all relevant policy revisions.

- [ ] **Step 1: Add failing behavior tests.** `test_two_projects_never_share_protected_processes_homes_or_tokens`, `test_complete_sdk_issue_workflow_has_zero_upstream_secret_exposure`, `test_direct_flow_still_runs`, `test_pool_or_image_change_invalidates_qualification`, `test_supervisor_crash_recovers_without_duplicate_provider_or_git_effect`, `test_cancel_or_lease_expiry_revokes_and_kills_sandbox`, `test_uncertain_cleanup_blocks_reuse` run real Compose/Hatchet/gateway/fake-provider/repository fixtures. Scan result/model history/artifacts/logs for seeded credentials.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Separate direct and protected-only pools/worker roles; prohibit a protected supervisor from claiming direct work or executing agent code inline. Handle durable startup/acknowledgment, timeout, cancellation, completion retry and cleanup through existing lease/idempotency contracts. Enable only the qualified SDK workflow matrix after all tests pass. Bound artifact collection and redact scoped capabilities before persistence.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests/integration -p test_protected_execution.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Add mode-aware onboarding and a successful protected-run screenshot showing stage timeline, logs and model usage. Protected target failure must show a blocker, never a direct-mode retry button that silently changes the run. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: enable qualified protected SDK workflow execution`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task D3: Qualify CLI sessions and isolated MCP tools independently

**Files:**
- Modify: `worker/cli_agent.py`; extract `worker/factory/adapters/cli_sessions.py`
- Create: `worker/factory/domain/tool_policy.py`, `control-plane/backend/app/tool_policy.py`, `adapters/mcp_sandboxes.py`
- Create: `tests/integration/test_protected_cli_mcp.py`, `control-plane/backend/tests/test_tool_policy.py`

**Interfaces:** Produce `ToolPolicyRevision(id, approved_servers, allowed_tools, allowed_operations)` and `launch_tool_sandbox(policy_revision_id: str, execution_pin: ExecutionPin) -> SandboxHandle`. Each approved MCP server receives a distinct delegated capability audience `mcp:<server-id>`; broker maps its fixed permitted downstream operations rather than accepting token passthrough. CLI adapters receive only scoped facade identities and qualified endpoint/protocol configuration.

- [ ] **Step 1: Add failing behavior tests.** `test_codex_and_claude_protocols_independently_qualified`, `test_cli_login_never_writes_upstream_keys`, `test_repository_mcp_config_cannot_enable_unapproved_server`, `test_mcp_cannot_inherit_agent_home_env_or_control_socket`, `test_mcp_wrong_tool_or_downstream_audience_denied`, `test_malicious_tool_cannot_exfiltrate_provider_keys`, `test_remote_mcp_redirect_and_token_passthrough_denied` use pinned real CLI binaries and deterministic responses. Unsupported CLI/MCP combinations fail readiness; mocks alone cannot enable capability flags.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Run each local MCP server in a separate fresh sandbox with an environment allowlist and no agent home/workspace unless its explicit tool policy grants a bounded artifact. Agent communicates through supervised approved tool channels; remote MCP connections originate in the trusted tool broker with destination/auth policy, not generic agent egress. Extract CLI session construction from existing workspace/change-collection logic. Keep direct CLI behavior intact and test version-specific sandbox/config flags.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests/integration -p test_protected_cli_mcp.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Document approval, tool scope, remote trust and data-exfiltration limits; publish the actual CLI/server capability matrix with fixture screenshots. Model compatibility and isolation are independent release requirements. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: qualify isolated CLI and MCP execution policies`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.

### Task D4: Kubernetes conformance, recovery and operator rollout

**Files:**
- Create: `control-plane/backend/app/adapters/kubernetes_sandboxes.py`, `tests/integration/test_protected_kubernetes.py`
- Modify: `deploy/kubernetes/`, `docs/kubernetes.md`, `docs/architecture.md`, `docs/openbao.md`
- Modify: `docs/user-walkthrough.md`, website source guides, `.github/workflows/tests.yml`

**Interfaces:** Implement SandboxRuntime semantics for native Kubernetes targets. Produce target-specific qualification evidence for admission; Docker evidence never qualifies Kubernetes. Production deployment policy controls which qualified targets/admin attestations are trusted.

- [ ] **Step 1: Add failing behavior tests.** `test_network_policy_enforced_not_just_present`, `test_no_ambient_service_account_or_host_namespace`, `test_wrong_pod_identity_cannot_use_another_session`, `test_node_or_broker_loss_preserves_uncertain_receipts`, `test_namespace_cleanup_and_orphan_recovery`, `test_kubernetes_mode_replay_and_followup_pins` run on an independently owned disposable cluster with enforcing CNI and real restricted pods. Add operator browser flow selecting target/mode and inspecting human-readable activity timestamps.
- [ ] **Step 2: Run the focused test command below and confirm the new assertions fail against the current behavior.** A missing dependency is not the expected red result.
- [ ] **Step 3: Implement the owned behavior.** Reuse native worker/job adapters and ownership labels without introducing an operator. Apply scoped RBAC and admission prerequisites; fail qualification on non-enforcing CNI or unsupported restrictions. Roll out only to an explicitly selected project after backup, readiness checks and smoke qualification. Rollback stops new protected admissions, drains/cancels active work and retains additive history; never convert accepted protected runs to direct.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m unittest discover -s tests/integration -p test_protected_kubernetes.py -v`. Expect zero failures and no unexpected skips. Include the relevant existing direct-mode regression suite; do not claim runtime qualification from unit tests.
- [ ] **Step 5: Document and review.** Record accepted ADRs only when implementation is merged; update layered architecture and credential-boundary diagrams, configuration field references, quick start and security limitations. Capture safe real UI screenshots, run `just docs-check`, and provide separate local-stack rollout instructions without executing them implicitly. Review the diff against the approved design, inspect changed security branches and resolve important findings.
- [ ] **Step 6: Commit and integrate.** Commit message: `feat: qualify protected Kubernetes execution and operator rollout`. Open one PR linked to the reconciled backlog, monitor required CI, merge only after checks pass, and close only the issue actually delivered. Keep subsequent capabilities unavailable until their release gates pass.


## Public-behavior test shape

Illustrative assertion shape for the owning tests; fixtures/errors are defined in that task, not existing implementation claims.

```python
result = malicious_probe(runtime.start(spec))
assert result.provider_credentials_found == []
assert result.supervisor_credentials_found == []
assert result.direct_network_connections_succeeded == []
assert result.permitted_broker_operation_succeeded is True
```
