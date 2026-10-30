# Test strategy: LiteLLM and replaceable integrations

Status: proposed acceptance criteria, not evidence that these tests already exist.
Companion: [integration and architecture plan](2026-10-30-litellm-integration-plan.md).

## 1. What “all code covered” means

Every owned production module must have an identified responsibility, appropriate unit or contract coverage, and a mapping into the relevant end-to-end journeys. E2E tests verify journeys across modules; they should not duplicate a unit test for every helper. Generated assets, vendor code, type-only declarations and declarative configuration need explicit ownership/exclusion rules or compilation/manifest tests rather than invented unit behavior.

The [owned-code inventory](2026-10-30-owned-code-test-inventory.csv) enumerates tracked production code/configuration candidates. Its status is deliberately **not measured**. It is an accountability list, not a coverage report or an assertion that absent tests have been found by static filename matching. Review its classification in slice 0 and attach measured evidence before marking rows covered. Include omitted owned modules discovered during that audit; exclusions require a reason and reviewer.

Inventory method: tracked Python, frontend TypeScript/CSS, website executable tooling/templates, shell scripts, YAML/CI/Compose files, Dockerfiles and `justfile`. It excludes test sources and image assets. `observability/pours/` is provisionally treated as vendored integration material; verify that ownership in slice 0 and include any maintained local modifications. JSON manifests, extensionless executables and other missed owned inputs must be added during the ownership audit; this candidate list is not claimed exhaustive before that review.

Baseline evidence at `ee9b861`:

| Area | Existing default evidence | Gap |
| --- | --- | --- |
| Worker | 396 unittest cases, 15 skipped | No measured module/branch coverage report |
| Backend | 427 unittest cases, 13 skipped | Real gateway, governance and reconciliation do not exist yet |
| Frontend | 116 Vitest/Testing Library cases; TypeScript/Vite build | jsdom component tests are not browser E2E |
| Documentation | 63 HTML pages, links/assets/search, 108 API routes, 31 guides validated | Future gateway examples and operator journeys need their own checks |
| Runtime contracts | Docker/Hatchet/GitLab/Kubernetes opt-in suites exist | A skipped live job is not evidence of gateway or full-stack compatibility |

Do not translate these test counts into a coverage percentage. First instrument the owned code and establish the baseline; then close gaps using behavior-focused tests.

## 2. Coverage and quality gates

- Produce Python line/branch coverage for worker, backend and maintained Python scripts; TypeScript branch/function coverage for frontend logic; browser trace/artifact coverage for UI journeys; executable fixture tests for shell/build/deployment scripts.
- For new/changed executable modules, target at least **90% branch coverage**, with **all enumerated authorization, revocation, pinning, deadline, retry, deduplication and cleanup decisions exercised**. A percentage cannot waive an uncovered critical branch. Any exception names the unreachable/platform-specific branch, rationale, owner and removal date.
- For existing modules below the target, record measured gaps and create a scoped remediation list. Do not lower thresholds, blanket-exclude legacy files or claim the whole codebase is covered. Gateway general availability requires completing the owned-module verification mapping and the relevant critical journeys.
- Add representative mutation checks for policy decisions: remove a project check, permit an unsupported model, count duplicate usage, retry a budget rejection or bypass a cleanup guard. The relevant tests must fail. Avoid snapshot/constant tests that pass while the behavior is wrong.
- Test public application interfaces and real adapters. Mock network/process/time at the lowest necessary seam; do not replace the behavior under test or assert that a fake was merely called.
- Assert outputs and meaningful effects: canonical state, persisted pins, approved route, exact bounded request/attempt counts, independent project credentials, repository artifacts and recorded usage. Expectations use hand-checked values, not the production serializer/resolver being tested.
- Unexpected warnings/errors, leaked children/sockets, unhandled async failures, credentials in artifacts and unexpected E2E skips fail the job. Record intentional skips separately with capability/platform reasons.
- Use readiness/observed conditions, deterministic clocks and bounded deadlines. Avoid arbitrary sleeps. Fault injection belongs at the transport, process, datastore or clock seam.

### Release boundaries

| Milestone | Required test/coverage closure | What may remain |
| --- | --- | --- |
| API preview in disposable environment | Measured baseline, owned-module mapping, changed model/config/grant/accounting critical branches, direct compatibility, adapter fixture and API real-stack vertical journey | Existing unrelated low-risk coverage debt remains explicitly tracked; no production or full-platform coverage claim |
| Governed API release | All modules affected by the API path meet the new/changed branch gate; E01–E09 and E12–E15 pass; key, counting, lease, persistence and reconciliation profiles are qualified | CLI features remain unavailable until their gate; unrelated runtime replacement refactors stay separate |
| Each CLI release | All API gates plus its real binary/protocol/authorization path, E10 or E11, secret isolation and timeout/restart cases | The other CLI may remain unavailable; no mocked process can waive the actual-binary gate |
| Kubernetes gateway release | Relevant API/CLI gates plus E16 on disposable native Kubernetes | No claim of Docker-identical filesystem/logging/network behavior |
| Whole-platform quality completion | Every owned inventory row has reviewed measured verification evidence, all critical journeys and identified existing gaps are closed, and exceptions are explicit | Only justified generated/vendor/platform exclusions; do not equate API release with this milestone |

Unavailable real engine/bootstrap or CLI binaries block the affected release. They do not block unit refactoring, but a fake dispatcher/process cannot turn an incomplete release gate green. Slice 0 produces an itemized coverage backlog with owners and estimates so whole-platform improvement is bounded and trackable rather than an implicit rewrite prerequisite. Completing that backlog remains part of this overall quality plan.

## 3. Unit and component matrix

Proposed files are deliverables for implementation slices; current filenames listed as extensions already exist.

| Responsibility | Tests to add/extend | Required cases |
| --- | --- | --- |
| Task prompts/results | Extend `tests/test_model_client.py` | Existing triage/plan/patch/review schemas; task prompt/model precedence; safety/path limits; refusals, invalid/empty/oversized output; no raw SDK object enters application values |
| Route resolution | New `tests/test_model_routes.py`; backend configuration/project tests | Direct compatibility; each inheritance level; conflicting legacy/new fields; unsupported operation; immutable accepted route; disabled connection; alias and model-override rejection |
| Installed descriptors | New `tests/test_model_adapter_registry.py`; existing bootstrap/action tests | Two real descriptors; unknown version/factory rejected; lazy optional dependencies; one session per operation; minimum grants cannot be weakened by custom data |
| Chat/direct/gateway protocol | New `tests/test_model_transport_contract.py` | Required JSON/output limits; approved URL/TLS; correct authentication; request/response IDs; normalized usage/error mapping; no endpoint/header overrides through workflow input |
| Token assessment | Extend `tests/test_token_budget.py`; new route-specific cases | Exact request counting including guidance; aliases; unsupported counters; invalid/negative/bool counts; context/output reserve; fallback capability mismatch; no generation after rejection |
| Credential policy | Extend `control-plane/backend/tests/test_project_secrets.py` and action context tests | Wrong project/repository/connection/purpose; admin-only management; inference vs master key; revoked pinned version; normal and emergency rotation; redaction |
| Retry/deadline policy | New `tests/test_model_retry_policy.py` | 401/403 and budget rejection never retried; bounded rate-limit wait; 5xx/connection-before-send; timeout-after-send uncertainty; no SDK/gateway retry multiplication; cancellation before/after send |
| CLI session construction | Extend `tests/test_cli_agent.py`; new `tests/test_cli_sessions.py` | Codex config round trip; Claude environment; two project homes concurrently; no inherited login/provider credentials; model alias escaping; cleanup on success/error/timeout; existing archive/change safety preserved |
| Usage normalization | Extend `tests/test_telemetry.py`, `tests/test_project_telemetry.py` | Null vs zero; cached/reasoning subsets; requested/resolved identity; different providers; failed attempts with known/unknown usage; no source/prompt in normal logs |
| Reconciliation | New backend `test_llm_accounting.py` plus existing project usage tests | Same observation twice; out-of-order observations; worker/CLI/gateway duplication; parent/leaf overlap; distinct paid fallbacks; currency/price revision; missing IDs remain unreconciled; cross-project queries |
| Runtime/session lifetime | Extend `test_runtime_client_lifecycle.py` and model session tests | Close once on every exit; no stale cached clients/keys; explicit clock; expired lease/claim; concurrent sessions; existing Docker/Kubernetes authority fences |
| Control Pane forms | New gateway connection/binding/route component tests | Allowed selection, async defaults preserve manual edits, loading/error/empty states, conflict recovery, Cancel, token visibility/redaction, role/capability display, no launch on save |
| History/usage UI | Extend project model-call/workflow UI tests | Logical call vs attempt, requested vs resolved model, known estimates vs reports, capture/coverage gaps, record caps, duplicates, stage/attempt scoping, URL navigation |
| Scripts/configuration | Fixture tests for new Compose/CLI/scripts and current uncovered wrappers | Correct arguments/exit codes, quoting, missing secrets, bounded defaults, manifest render, image digest/TLS settings, teardown guards; no source-text substring assertions |

Use existing direct adapter tests as compatibility tests instead of rewriting them into mocks that bypass token limits, schemas or source safety. Extend them through the new interface; remove redundant implementation-coupled tests after equivalent behavior coverage exists.

## 4. Shared adapter conformance suites

Run the same semantic tests with an adapter supplied as a parameter. Keep vendor-specific assertions inside that adapter's own tests.

| Seam | Implementations | Shared contract / explicit difference |
| --- | --- | --- |
| Model transport | Direct OpenAI protocol, real LiteLLM, alternative compatible gateway | JSON/output limits, token capability, errors, deadlines, usage, identities; no claim every vendor/model supports all capabilities |
| Gateway administration | LiteLLM management, second qualified candidate | Scope verification, import/provision/revoke semantics, unauthorized writes, secret ownership; unsupported management features must be reported |
| Source host | GitHub, GitLab.com | Issue/pull/check semantics, bounds, idempotency and safe mutation; retain provider-specific feedback/check tests |
| Execution runtime | Docker, Kubernetes | Job lifecycle, capture/cleanup and ownership/fences; preserve documented log/filesystem/network differences |
| Workflow dispatch | Hatchet, local adapter | Supported enqueue/wait behavior; local synchronous execution does not qualify durable replay/waits |
| Telemetry | Collecting fake, real OTLP collector, optional SigNoz | Correlation/redaction/coverage; application behavior must not require a particular observation backend |

Deterministic HTTP fixtures must exercise the real wire adapter, with bounded requests/responses and fault injection. A fake application port is useful for unit tests but cannot qualify LiteLLM, OpenAI wire behavior or a CLI binary.

## 5. Three integration environments

### A. Mandatory deterministic adapter environment

- Pinned real LiteLLM container, isolated PostgreSQL for its governance, optional cache when that deployment needs it, deterministic fake upstream provider, OTLP collector and test-only management/inference credentials.
- Use an internal test network with no route to paid providers. Upstream keys are synthetic; the fixture checks that no real provider credentials were inherited. Repository access is also a fixture.
- Prove real gateway protocol translation, JSON/count capability, key/model restrictions, non-retryable policy rejection, retry/fallback observations, database/cache outages and capture behavior.
- Execute it on every PR touching a model/gateway contract, configuration/grant code, CLI integration or accounting, and on dependency upgrades. Require it before merging the first external-adapter slice.

### B. Mandatory real-stack E2E for changed critical journeys

Browser → production frontend → real FastAPI routes → real Flumic PostgreSQL → actual Hatchet dispatcher/coordinator → real shared worker process/container → real LiteLLM → deterministic upstream HTTP fixture → source-host HTTP fixture → history/accounting API → browser.

Use a separate disposable engine tenant and gateway persistence. Slice 0 must first prove unattended bootstrap of the pinned local Hatchet build. If that is unavailable, provision a dedicated CI test tenant through its supported bootstrap path and make the full-stack job a required configured gate. Do not silently substitute a fake dispatcher and call the result E2E.

Run a real browser in CI with version-pinned tooling and readiness checks. Trace/screenshot/video artifacts must be redacted and access-controlled. This is test infrastructure, not automation of the operator's live browser or Control Pane.

### C. Explicitly opt-in qualification

- Real upstream models and real CLI/provider combinations: bounded calls/spend, named disposable projects and approved secrets.
- Native Kubernetes deployment: disposable k3d cluster and dedicated namespace; reuse existing guarded cleanup/UID contract tests.
- Actual GitHub/GitLab writes: approved disposable repositories only, with marked fixtures and guarded cleanup. Do not use production repositories for an E2E default.
- Alternative gateway/edition/load testing with pinned manifests, resource measurements and a published capability report.

Synthetic local tests do not establish model quality, paid provider reliability, invoice equivalence or an exact budget cap. They must be followed by the relevant opt-in qualification before a combination is advertised as supported.

## 6. Required end-to-end journeys

| ID | Journey and assertion |
| --- | --- |
| E01 | Administrator creates approved connection; a project selects its binding; issue/workflow input cannot replace origin, TLS policy or credential purpose |
| E02 | Existing direct-mode deterministic workflow retains artifacts, prompts, model selection and usage semantics after migration |
| E03 | Gateway API plan → triage → implementation → review → record completes against fixture source host; artifacts satisfy acceptance criteria; no extra repository effects |
| E04 | Two projects execute concurrently on the same shared workers with different aliases/keys/prompts; no cross-project secret, route, source or model-history leakage |
| E05 | Change connection/default/model settings after admission; accepted work retains its snapshot while new work adopts the revision; live revocation still blocks later requests |
| E06 | Invalid/revoked key, denied alias and budget exhaustion stop generation; no direct-provider fallback and no subsequent repository mutation |
| E07 | Rate limiting, gateway outage and timeout-after-send produce bounded attempts, visible uncertainty and correct terminal/wait behavior; cancellation does not invent zero charge |
| E08 | Permitted gateway retry/fallback has separate attempt identity and one reconciled cost per actual attempt; duplicate/delayed SDK/gateway records do not increase totals |
| E09 | Gateway database/cache restart/outage and multiple replicas preserve the qualified restriction/budget guarantees; an unsupported/fail-open deployment cannot pass budget-required enablement |
| E10 | Pinned Codex binary routes Responses through the gateway, produces the bounded metadata completion and fixture workspace changes, and never uses personal auth or another project's home |
| E11 | Pinned Claude binary routes Messages/count traffic through the gateway with its allowed alias; independent concurrent session and cleanup behavior match E10 |
| E12 | Control Pane exposes readable stage result/log/model/configuration views, connection provenance and accounting gaps; refreshing/history filters do not lose stage/attempt scope |
| E13 | Prompt/source capture remains disabled when policy says so in Flumic, gateway DB, logs and collector payloads; retained approved capture expires/redacts correctly |
| E14 | Restart worker/coordinator during accepted work, replay settlement and delayed telemetry; preserve authority, avoid duplicate source effects and keep reconciliation idempotent |
| E15 | Disable gateway for new admissions and roll future work back to explicitly selected direct mode; existing pinned work is drained/cancelled rather than silently rerouted |
| E16 | Docker and Kubernetes deployment qualification exercise equivalent gateway journeys while preserving each runtime's documented isolation/capture differences |

Keep the fake upstream deterministic: scripted valid/invalid JSON, refusals, token-count responses, fallback responses, delayed chunks where supported, header-only failure, truncated bodies and connection reset. No network interception of a real provider is needed for mandatory CI.

### Additional policy oracles from the builder check

- Assert 30-second authorization leases expire closed during control-plane/gateway partition, revocation remains pending until confirmed, and a 60-second confirmation breach raises visible failure without falsely claiming that an already sent call stopped.
- Reject imported keys lacking authoritative ownership/model/route/budget/expiry evidence or carrying management privileges. Manual setup must pass the same verification.
- Reject unknown counting accuracy and unversioned prompt transformations. CLI tests inspect actual internal protocol envelopes at the gateway, not just Flumic's outer prompt.
- Crash before/after attempt reservation and dispatch; verify call/retry-group IDs and the original deadline survive. Stage retries cannot reset group ceilings, and unknown-after-send does not automatically regenerate.
- Feed conflicting SDK/gateway/CLI reports with the same attempt IDs and source revisions; verify the plan's precedence, conflict display and separate monetary projections. Add missing retry/cached/late-correction cases; an unknown attempt prevents a complete-total label.
- Verify adapters qualify only their declared profiles. Missing strict CLI authorization or attempt IDs is reported as unsupported/partial capability, never silently labelled a complete drop-in replacement.

## 7. CI and commands

Existing: `just test-worker`, `just test-backend`, `just test-ui`, `just ui-build`, `just docs-check`. Use the documented `FLUMIC_PYTHON` override when an isolated checkout has no local virtual environment.

Proposed additions (these commands **do not exist yet**):

| Command | CI purpose |
| --- | --- |
| `just test-coverage` | Locked Python/Vitest coverage tools, branch reports, module inventory reconciliation and changed-code gates |
| `just test-gateway-contracts` | Real pinned gateway + fake upstream, no paid network paths, shared adapter contracts |
| `just test-gateway-e2e` | Browser/API/engine/worker/gateway/source fixture journeys; deterministic CI bootstrap |
| `just qualify-gateway <candidate>` | Opt-in pinned candidate/version/edition report, requiring explicit environment/credential selection |

Add separate CI jobs for unit/coverage, protocol contracts, real-stack E2E and optional qualification. PRs affecting model routing, policies or accounting require the first three; documentation-only PRs need documentation/example checks. Nightly full matrix runs supplement, not replace, change-triggered gates.

Pin images, CLI/tool versions, dependencies and fixture checksums. Upload JUnit/coverage reports, sanitized route/attempt/accounting evidence and failed E2E traces. Never upload `.env`, CLI homes, secrets, raw unapproved prompts or database dumps. Inspect artifact content with seeded canaries before upload. Required qualification jobs cannot pass because credentials are absent and the whole job skipped.

The coverage gate must include subprocess workers and reconnect/restart scenarios where relevant; collecting coverage only in the parent API process misses most executed workflow code. Keep all instrumentation in test images/fixtures and private artifact paths, not the operator's running containers.

## 8. Completion checklist

- [ ] Instrument and review the current owned-module inventory; publish the measured baseline and explicit exclusions.
- [ ] Give each module/behavior family a test owner and evidence path; close current critical gaps rather than merely counting new tests.
- [ ] Capture a failing test for each implemented policy or regression before changing its implementation.
- [ ] Pass the same adapter contracts with direct and gateway implementations.
- [ ] Pass E01–E15 where their feature is enabled; require E10/E11 before advertising each CLI path; require E16 for Kubernetes support.
- [ ] Publish exact gateway/CLI/protocol/edition support and unsupported combinations, with release evidence.
- [ ] Complete independent standards, design-conformance and authorization/accounting review.
- [ ] Validate migration/rollback on disposable persisted state, not just fresh installation.
- [ ] Run all required CI gates without unexpected skips; link evidence to #41–#45.
- [ ] Update architecture/ADR/configuration/onboarding docs with current safe screenshots; no diagrams implying unimplemented support.
