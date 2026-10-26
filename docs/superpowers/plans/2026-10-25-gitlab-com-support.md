# GitLab.com Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Native inline execution is preserved from the user's standing workflow; one fresh final reviewer, no implementer agents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver issue #16: GitLab.com repositories execute existing factory workflows with provider-correct identity, credentials, publication, feedback and checks.

**Architecture:** Extend the shared repository boundary with a GitLab REST reader/metadata adapter and an exact-head Git publication transport. Pin source-host identity and check policy in project context, and add durable authenticated project triggers. Preserve GitHub behavior and historical identifiers.

**Tech Stack:** Existing Python/unittest, FastAPI/SQLAlchemy/PostgreSQL/Alembic, React/TypeScript/Vitest, Hatchet and Git executable; no additional vendor SDK required.

**Spec:** `docs/designs/2026-10-25-gitlab-com-support.md` (approved by user: “sounds good”).

## Global Constraints

- Support exactly `github` / `github.com` and `gitlab` / `gitlab.com`; self-managed/Dedicated are excluded.
- Preserve GitHub defaults, serialized historical revisions, canonical claim strings, old workflow IDs and receipts; no automatic existing-worker restart.
- Managed actions use only project/repository-scoped pinned credentials, never global GitLab tokens. Keep model grants independent.
- GitLab numeric project identity is authoritative; issues and MRs have separate IID namespaces. Nested groups are valid; traversal/arbitrary hosts are not.
- Publication uses an explicit expected-old-OID lease and a commit whose parent is the pinned expected head. Never use unconditional force, rewrite ancestry, expose token argv/URLs, or silently rebase generated output.
- Pin required pipeline sources (default `push`) and required external status names. Unknown/absent required evidence cannot become success.
- Real GitLab.com writes require an explicitly designated disposable project and authorized mutation scope. Never request pasted credentials. No paid AI calls, existing user-worker restart or unrelated live mutations.
- Keep #16 and public GitLab support status planned until the real-host end-to-end gate passes. Do not close it merely because mocks or a plan pass.
- One issue/branch/PR; focused intermediate commits, one fresh read-only final reviewer, one grouped Important/Critical RED→GREEN fix pass, exact-head CI before merge. Preserve the managed worktree and prior ignored ledgers.

## Review Focus

- Human branch reset/advance between validation and publication: exact-old-OID guard rejects both and preserves human history (Task 2).
- GitLab numeric IID collisions and renamed/reused namespace paths: issue/MR claims remain separate, pinned numeric project cannot change silently (Tasks 1, 4).
- Lost mutation response followed by retry: compare full expected branch tree/commit marker and existing MR before retrying; never duplicate a known mutation (Tasks 2, 3).
- Duplicated/reordered forged webhook and concurrent poll delivery: authenticate repository binding, fresh-read eligibility and durably deduplicate acceptance (Task 5).
- Multiple pipelines, child evidence and missing/manual statuses: exact SHA/branch/pinned policy determines completion; unknown evidence blocks (Task 3).

## File responsibilities

- `worker/factory/domain/repositories.py`: validated provider identity and check-policy contracts.
- `worker/factory/adapters/gitlab.py`: GitLab REST mapping, bounded pagination, metadata and checks.
- `worker/factory/adapters/git_publish.py`: isolated Git subprocess/ref-lease publication and recovery evidence.
- Existing bootstrap/settings/action/context/state modules: compose and transport pinned identity/policy without source-host assumptions.
- `control-plane/backend/app/repository_validation.py`: remote read-only GitLab identity resolution and repository revision validation.
- `control-plane/backend/app/repository_triggers.py`: operator binding commands, webhook authentication, acceptance receipts and polling.
- `worker/factory/adapters/gitlab_events.py`: bounded wire-to-domain event normalization.
- Existing project UI/API: provider fields, validation, credential selection, trigger controls and source-host labels.
- Dedicated tests/fixtures/docs below own the behavior they introduce. Avoid unrelated service/adapter restructuring.

## Verification commands

Use the existing original-checkout virtualenv explicitly:
`/Users/hpradip/workspace/hatchet-test/.venv/bin/python` (abbreviated `$PY` below; set `PY` to this path in the executor).

Worker suite: `env -u PYTHONPATH "$PY" -m unittest discover -s tests -q`.
Backend suite: `PYTHONPATH=worker:control-plane/backend "$PY" -m unittest discover -s control-plane/backend/tests -q`.
Repeat backend with `CONTROL_TEST_DATABASE_URL` and `CONTROL_MIGRATION_TEST_URL` set to the dedicated disposable PostgreSQL test DB. Never use the live Control Pane DB for tests.
Frontend: `npm test --prefix control-plane/frontend -- --run --pool=threads --maxWorkers=1` then `npm run build --prefix control-plane/frontend` (local Node 26 requires thread pool; CI Node 22 uses repository defaults).
Docs: `npm run build --prefix website && npm run check --prefix website`.
Each command must exit 0; inspect actual output and record counts in this plan's ledger.

### Task 1: Provider identity and compatibility contracts

**Files:** Create `worker/factory/domain/repositories.py`, `tests/test_repository_identity.py`; modify `worker/factory/workflows/resources.py`, `worker/factory/workflows/contracts.py`, `worker/factory/settings.py`; add backend ownership cases in `control-plane/backend/tests/test_gitlab_repositories.py` as APIs arrive in Task 4.

**Interfaces:** Produce immutable `RepositoryIdentity(provider, host, owner, name, project_id=None)` with `path`, `canonical_key` and `credential_purpose`; `RepositoryCheckPolicy(required_pipeline_sources=('push',), required_status_names=())`. ActionContext carries a serialized validated repository identity and check policy, with old GitHub context defaults. Existing `canonical_repository` / `canonical_resource` remain backwards compatible; optional GitLab numeric project identity is mandatory for executable GitLab keys.

- [x] Write `test_github_canonical_bytes_unchanged`, `test_gitlab_nested_namespace_and_numeric_identity`, `test_issue_mr_iid_collision_is_not_same_claim`, `test_host_provider_mismatch_and_traversal_rejected`, `test_old_context_defaults_to_github`. Assert GitHub issue/pull #7 still share their old canonical key, GitLab issue #7/MR !7 differ, reused path/project ID differs, and secret-bearing URL inputs fail.
- [x] Run `env -u PYTHONPATH "$PY" -m unittest discover -s tests -p test_repository_identity.py -v`; expect missing contracts/new behavior failures.
- [x] Implement only these identity/check-policy/context contracts and compatibility helpers. Do not activate GitLab through configuration alone.
- [x] Rerun focused tests and full worker suite; expect all pass, existing serialized GitHub definitions unchanged.
- [x] Commit `feat: define provider-qualified repository identity (refs #16)`.

### Task 2: GitLab HTTP mapping and guarded publication transport

**Files:** Create `worker/factory/adapters/gitlab.py`, `worker/factory/adapters/git_publish.py`, `tests/test_gitlab_client.py`, `tests/test_git_publication.py`; modify worker image Git dependency declaration only if Git is absent.

**Interfaces:** `GitLabClient(repository: str, *, project_id: int, token: str, max_file_bytes: int, check_policy: RepositoryCheckPolicy)` satisfies `application.ports.Repository`; `GitPublisher.publish(*, remote, branch, expected_head, changes, marker, credentials) -> str` returns validated commit OID. GitLab REST transport may be injected in tests; production API root remains fixed `https://gitlab.com/api/v4`.

- [x] Add wire-contract tests for issues/notes/labels, inventory/files/archive pagination and bounds, nested path quoting, MR draft/state/diff normalization, related-plan-task markers, token-header redaction and rejected external redirects. Assert IID versus global ID mapping explicitly.
- [x] Add real local bare-Git tests `test_exact_head_lease_rejects_human_advance`, `test_exact_head_lease_rejects_human_reset`, `test_create_branch_race_preserves_other_commit`, `test_lost_push_response_recovers_only_full_expected_tree`, `test_credentials_not_in_argv_remote_or_errors`. Use deterministic local repos, no network/auth tokens.
- [x] Run each new test module via `unittest discover -s tests -p <filename> -v`; expect missing adapter/transport failures before implementation.
- [x] Implement bounded REST queries/metadata operations and isolated Git publication. Disable global Git config/hooks/credential helpers, arbitrary protocols and unsafe checkout side effects. Enforce allowed paths/content, expected parent/tree and explicit old-OID lease. Handle server ambiguity through evidence rather than unconditional replay.
- [x] Rerun focused tests plus `tests/test_github_client.py` and full worker suite; expect all pass. Verify no mutable global token/config.
- [x] Commit `feat: implement GitLab repository and guarded publication (refs #16)`.

### Task 3: Shared service composition, feedback, checks and legacy behavior

**Files:** Modify `worker/factory/bootstrap.py`, `worker/factory/settings.py`, `worker/factory/application/ports.py`, `worker/factory/application/service.py` only where contracts require; modify `worker/factory/workflows/actions.py`, action schemas/templates as needed; add `tests/test_gitlab_workflows.py`, `tests/test_gitlab_checks.py` and bootstrap tests.

**Interfaces:** Explicit repository factory selects installed GitHub/GitLab adapters from Task 1 identity; minimum source-host credential purpose comes from pinned identity. GitLab adapter implements feedback cursor/marker, draft publication/recovery and `verify_commit_checks(sha)` using Task 1 pinned policy. Preserve existing builtin public schemas/@1 semantics for old pins; version new schemas if compatibility cannot be retained.

- [x] Test issue→plan→draft MR→feedback commit→merge observation→checks through actual FactoryService and ActionCatalog with deterministic AI and GitLab HTTP/Git fixtures. Assert shared business policy, related plan tasks, closed-unmerged separation, feedback head invalidation and duplicate mutation recovery.
- [x] Test required `push` default, multiple sources/ref/SHA, latest pipeline/status selection, child/downstream evidence, manual/skipped/absent/inaccessible checks, optional jobs and failing external contexts. Only required qualifying exact-commit evidence succeeds.
- [x] Test explicit provider composition for managed and legacy bootstrap, GitHub defaults/names/IDs preserved, no GitLab fallback to environment grants, and wrong-host output URL rejected by action scope.
- [x] Run new tests; expect missing composition/policy behavior failures. Implement adapters/composition/capability translations, not a second workflow service.
- [x] Run focused tests and full worker suite; expect pass with no GitHub regression. Commit `feat: run shared workflows against GitLab (refs #16)`.

### Task 4: Project repository validation, scoped grants and durable ownership

**Files:** Create `control-plane/backend/app/repository_validation.py`; modify `app/projects.py`, `app/project_context.py`, `app/workflow_definitions.py`, `app/workflow_state.py`, `app/workflow_runs.py`, `app/job_broker.py` where source-host assumptions occur; extend `test_gitlab_repositories.py`, `test_project_context.py`, `test_workflow_definitions.py` and migration tests as required.

**Interfaces:** Project repository revision document contains Task 1 identity/check policy, including validated numeric `project_id` for GitLab. Add authenticated project-owned `POST /api/v1/projects/{project}/repositories/{repository}/validate` using saved credentials and parent revision. Validation publishes a new immutable resolved revision; stale parents return conflict. Launch/context pinning consumes this revision, never remotely re-resolves another project by path mid-run. `gitlab` secret purpose participates in existing rotation/revocation/replay/redaction rules.

- [x] Add HTTP/DB tests for nested GitLab namespace, validation parent race, wrong/revoked/foreign grants, numeric identity mismatch, renamed/reused paths, pin after edits, mixed aliases and issue/MR IID claim separation. Assert unresolved GitLab revisions cannot launch, old GitHub contexts still launch, Docker grants stay explicit.
- [x] Test legacy/dynamic takeover on provider-qualified identity without automatic stop/restart; preserve populated old IDs, claim strings, histories and revision documents.
- [x] Run focused backend tests with isolated SQLite fixture; expect provider/secret/identity rejection failures. Implement owned validation/pinning and context transport using Task 1 contracts. Any additive migration must preserve GitHub bytes/defaults and be reversible on disposable fixtures.
- [x] Run full backend SQLite and PostgreSQL ownership/migration/concurrency suites plus worker contracts; expect pass. Commit `feat: pin project-scoped GitLab repositories and grants (refs #16)`.

### Task 5: Authenticated events, durable trigger acceptance and polling

**Files:** Create `worker/factory/adapters/gitlab_events.py`, `control-plane/backend/app/repository_triggers.py`, new additive trigger migration under `control-plane/backend/migrations/versions/`; modify `app/database.py`, `app/main.py`, `app/dynamic_runtime.py`, `worker/factory/adapters/orchestration/hatchet.py` for explicit legacy GitLab registration; add `tests/test_gitlab_events.py`, `control-plane/backend/tests/test_repository_triggers.py`.

**Interfaces:** `RepositoryTrigger` stores project/repository/event kind/definition ID/enabled/readiness policy plus encrypted versioned webhook token separate from action secrets. `TriggerReceipt` stores dedup identity, accepted workflow revision/run pin and outcome. Operator endpoints under `/api/v1/projects/{project}/repository-triggers`; external webhook under `/api/v1/hooks/gitlab/{binding_id}`. `accept_event(binding_id, normalized_event) -> receipt` is shared by webhook and bounded polling; dispatch uses existing durable run intents.

- [x] Test constant-time token verification, bounded payload, numeric project mismatch, disabled/stale/foreign binding, safe errors/redaction, duplicate/reordered events, webhook/poll race and controller crash after acceptance. Assert one durable pinned launch, no arbitrary workflow selection, no grant forwarding and fresh-read eligibility.
- [x] Test missing webhook delivery recovers through polling and legacy GitLab event registration doesn't rename existing GitHub workflows. Trigger token rotation/revocation must be explicit and must not modify accepted run pins.
- [x] Run tests; expect absent trigger/event routes and tables. Implement minimal owned binding CRUD, token authentication, normalization, transactional receipts, dispatch/poll recovery and independently scoped cursors. Polling obtains credentials only within each repository scope.
- [x] Run worker/backend SQLite+PostgreSQL suites and serialization/migration checks; expect pass. Commit `feat: accept durable GitLab events and polling triggers (refs #16)`.

### Task 6: Control Pane configuration and operator documentation

**Files:** Modify `control-plane/frontend/src/api.ts`, `features/projects.tsx`, `projects.test.tsx`; create focused `features/repository-triggers.tsx` and `repository-triggers.test.tsx` if size warrants, integrate existing project navigation; create `docs/gitlab.md`, register guide in `website/scripts/prepare_content.py`; update configuration/API/architecture/README references accurately.

**Interfaces:** Typed owned repository validation/trigger endpoints from Tasks 4–5; project-qualified query keys and captured mutation scope. UI shows provider-correct namespace/project/credential labels, immutable resolved ID/check policy, trigger enabled state and token-rotation controls. Secret values never enter cached query responses; new webhook token displayed once through established credential patterns.

- [x] Test mixed GitHub/GitLab project connections, nested namespaces, secret-purpose filtering, validation/conflict errors, pin display and required CI policy. Test project switch/delayed mutations and trigger creation/rotation without stale token/data leakage.
- [x] Run new frontend tests; expect unavailable fields/routes/components. Implement these workflows using existing forms and conflict handling.
- [x] Document GitLab.com setup, exact token permissions, Git dependency, trigger/poll/webhook setup, examples, related-task semantics, CI policy, publication uncertainty and live test requirements. Keep status planned until Task 7 real-host gate passes; no speculative supported claims.
- [x] Run frontend tests/build and docs build/link/route coverage checks; expect pass. Browser smoke on isolated fixture data if UI changes need it; no real secret screenshots. Commit `feat: configure GitLab repositories and triggers in Control Pane (refs #16)`.

### Task 7: End-to-end release evidence, review and merge

**Files:** Create `tests/integration/test_gitlab_contract.py` and bounded fixture helper as needed; extend `.github/workflows/tests.yml` with an explicit opt-in gate and safe artifact allowlist; update `docs/gitlab.md`, README and roadmap only after behavior is verified.

**Interfaces:** `FLUMIC_LIVE_GITLAB=1`, privately configured test project ID/path/token and explicit write opt-in. Tests verify identity before modifying only uniquely marked owned test resources. No model credentials are required. Exact variable names and safe cleanup inventory must be documented alongside the harness.

- [ ] Add deterministic combined fixture through actual project API/PG/shared worker/service paths with GitLab protocol fixture and fake AI. Exercise mixed GitHub/GitLab isolation, grant rotation, lost publication/acceptance responses, branch races, feedback, trigger replay and checks. Prove redaction/known-effect dedup.
- [ ] Add live GitLab.com contract with explicit identity/mutation guard and best-effort per-resource cleanup. Missing opt-in skips; explicit opt-in without configuration fails clearly. Do not run writes until the designated disposable project and mutation scope are authorized; keep issue open if unavailable.
- [ ] Run all worker/backend SQLite+PostgreSQL/frontend/site gates, relevant existing real Hatchet/Docker release contracts, deterministic GitLab fixture and authorized real GitLab.com gate. Record dependency versions, command exit statuses and safe evidence; never upload grants/config/spools/raw logs.
- [ ] Dispatch one fresh read-only final reviewer with base/spec/plan/ledger/current head. Regrade by user effect; fix Important/Critical in one grouped RED→GREEN pass, defer Minor, no rereview. Preserve all rulings and limitations.
- [ ] Commit final qualification/docs `test: qualify GitLab workflow recovery and compatibility (refs #16)`, push/open/attach PR, await all exact-head CI. Merge after successful gates, then check acceptance/close #16 with evidence. If real-host gate is unavailable, report the precise remaining requirement and leave #16 open. Never start #17 automatically.

## Self-review

Coverage: identity/configuration/credentials map to Tasks 1/4/6; adapter/publication/recovery to 2/3; events/polling/trigger authorization to 5; legacy compatibility spans 1/3/4/5; operator docs and live release gate are 6/7. Every Review Focus condition has an owning task/test. Task 1 contracts feed 2–5 consistently; Task 4 produces immutable identity before Task 5 can accept launches. No plan step treats remote provider support or real-host publication as established by mocks. Existing user authorization preserves native execution and issue/PR tracking; live GitLab mutation authorization remains a separate explicit resource boundary.

## Handoff

Plan awaits user review as required by the planning skill. Preserve native inline execution. After review, initialize a separate ignored ledger for this plan and execute Task 1; do not reuse the completed project-runtime plan's workspace.
