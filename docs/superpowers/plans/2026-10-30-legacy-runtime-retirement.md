# Legacy Runtime Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove static execution and its history while preserving the working Default project.

**Architecture:** Shared workers and project-owned versioned actions become the only execution path. A separate guarded maintenance command removes positively identified retired records before a forward schema migration. Project settings compilation and immutable historical pins remain supported.

**Tech Stack:** Python, SQLAlchemy, PostgreSQL, Alembic, FastAPI, React, Vitest, Docker Compose, Hatchet, Kubernetes and Astro.

**Spec:** ../specs/2026-10-30-legacy-runtime-retirement-design.md

## Global Constraints

- Preserve the Default project, current configuration, credentials and four completed project runs.
- Never delete events by project ID, age, workflow name or missing attribution.
- Keep existing Alembic history for upgrade and fresh-install paths.
- Cleanup is an explicit operator task, not a startup hook.
- No paid model calls, repository writes, volume pruning or deletion of shared Hatchet/SigNoz stores.
- Retain private backups and rollback images. Never recursively delete data or shared.
- Historical immutable documents retain their original ciphertext and digest.

## Review Focus

- Mixed current and retired events in one project: delete only verified retired worker provenance (task 1).
- Changed targets between inspect and apply: abort without partial deletion (task 1).
- Serialized references inside encrypted pins: retain referenced snapshots and reject unsafe pruning (tasks 1, 3).
- Shared imports from static modules: retain client construction, model recording and state fencing (task 2).
- Old startup against a partially upgraded database: require an offline rollout and reject retired records before migration (tasks 3, 8).

### Task 1: Guarded database cleanup

**Files:** Create `control-plane/backend/app/legacy_retirement.py`, `scripts/retire-legacy-runtime.py`, and `control-plane/backend/tests/test_legacy_retirement.py`.

**Interfaces:** Expose inspect_retirement(db, settings) -> dict and apply_retirement(db, settings, expected_manifest: dict, backup_manifest: dict) -> dict. The CLI defaults to inspect, requires explicit apply and verified backup, and never logs ciphertext.

- [ ] Write failing tests: Seed shared and retired workers within Default. Assert preserved row hashes, two-worker current history retention, exact retired event/conversation joins, transaction rollback, idempotence, manifest drift refusal, active-operation refusal, attributed-event refusal, retained assignment/binding refusal and serialized-pin reference refusal.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_legacy_retirement.py -v`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 2: Shared-only worker composition

**Files:** Modify `worker/worker.py`, `worker/factory/bootstrap.py`, `worker/factory/adapters/state.py`, `worker/factory/adapters/control.py`, and `worker/factory/shared_worker.py`; create `worker/factory/adapters/hatchet_client.py`; remove static orchestration and repository-global state implementations after import migration.

**Interfaces:** Keep action_service and run_builtin interfaces. Move create_client(settings) without changing transport behavior; move reusable history recording out of legacy publisher plumbing. Reject explicit legacy/unknown protocols, and default only to shared execution.

- [ ] Write failing tests: Test missing/legacy/unknown protocols, clean imports, registration, action execution, credential isolation, history retries and ExecutionStateStore fencing. Rewrite useful static-runner tests through current action interfaces before removing obsolete tests.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `.venv/bin/python -m unittest discover -s tests -q`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 3: Configuration and schema separation

**Files:** Modify `control-plane/backend/app/{database,projects,services,worker_configs,main}.py`, `worker/factory/{configuration,settings}.py`, config defaults and Alembic versions; create a forward retirement migration and migration tests.

**Interfaces:** Keep current project revision routes and configuration compiler. Stop bootstrapping saved static worker profiles. Remove retired profile CRUD and tables only after cleanup; migration refuses populated retired tables and permits fresh databases. Preserve immutable pins through a narrow reader normalization where required.

- [ ] Write failing tests: Test fresh upgrade, refusal before cleanup, cleanup then upgrade, migration rerun, retained factory ancestry, original run pin digests, saved effective prompts and startup not recreating profiles. Exercise PostgreSQL foreign keys and transaction behavior.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -q`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 4: Remove backend static execution interfaces

**Files:** Modify `control-plane/backend/app/{main,reconciler,workflow_state,runtime_targets,ports}.py` and runtime adapters; remove static workflow_controls implementation and associated engine-only paths.

**Interfaces:** Retain shared lifecycle actions and pool provisioning, project launch, resource claims, activation, validation and follow-ups. Retire legacy creation/bootstrap/ingest and unscoped project endpoints with 404/410; no silent Default fallback or write redirection.

- [ ] Write failing tests: Test retired endpoints cannot mutate, workspace interfaces still work, project paths require ownership, shared restart/remove operation fencing, Docker and Kubernetes dispatch, repository onboarding without legacy takeover and disabled retired writers.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -q`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 5: Project-only operator navigation

**Files:** Modify `control-plane/frontend/src/app/App.tsx`, `features/{projects,workers,runtime-targets}.tsx` and repository handover UI; remove unused static pages after shared component extraction.

**Interfaces:** Projects is the landing page; workspace infrastructure exposes shared workers, pools and targets. Keep all project run, usage, model and configuration views. Old browser links show retirement guidance without translating engine run IDs.

- [ ] Write failing tests: Test landing and navigation, pool-based provisioning, absent legacy forms and links, cross-project isolation, old URL guidance and readable run/model timestamps. Preserve existing run inspector tests.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `npm test --prefix control-plane/frontend && npm run build --prefix control-plane/frontend`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 6: Remove static deployment and update onboarding

**Files:** Remove static compose.yaml, compose.workers-observability.yaml, compatibility alias and static launch/event helpers; update justfile, Dockerfile COPY commands, tests/test_compose_deployment.py, README, canonical guides, generated API reference and architecture ADR.

**Interfaces:** Keep worker image builds, control/database/gateway/monitoring/metrics stacks and worker-network. Replace supported static examples with project pool instructions. Keep historical ADR/spec documents clearly marked historical.

- [ ] Write failing tests: Test rendered Compose and shell fixtures have no deleted path dependencies. Build both worker image variants. Validate docs generation, links and API route reference; capture changed navigation/configuration screenshots from the qualified UI without secrets.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `.venv/bin/python -m unittest discover -s tests -p test_compose_deployment.py -v && npm run build --prefix website && npm run check --prefix website`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 7: Guarded filesystem and container retirement

**Files:** Extend retirement maintenance tooling with a separately reviewed resource manifest and tests; document recovery in docs.

**Interfaces:** Inventory exact retired identities and paths. Verify stopped containers and ownership, current mounts, symlink containment, backup and absence of retained references. Archive before removing retired snapshots, spools and SQLite sidecars; never prune shared roots or infrastructure.

- [ ] Write failing tests: Test swapped paths, symlinks escaping approved roots, current shared-history mount overlap, changed container IDs/state, already-absent resources and partial resource cleanup recovery. No Docker deletion before these checks pass.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -p test_legacy_retirement.py -v`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

### Task 8: Qualify, clean local data and publish reviewed changes

**Files:** Update retirement runbook and private backup/preservation manifests; deliver implementation branch and PR.

**Interfaces:** Pause admission and writers; drain shared assignments, take final quiesced backup and restore-test it in an isolated temporary database. Capture exact preservation hashes. Apply database cleanup, migrate and deploy, then apply the exact resource manifest. Resume current shared workers, retaining their identities. Roll back on any preservation mismatch.

- [ ] Write failing tests: Run complete worker/backend/frontend/Compose/docs checks and PostgreSQL contracts. Qualify one core.echo@1 project workflow without paid inference, inspect its history, and compare retained four runs, configuration, secrets, memberships and original shared event IDs. Check health and restart does not recreate legacy records. Review the branch, fix findings, open PR and wait for required CI before merge.
- [ ] Run the targeted tests and confirm the new assertions fail for the intended reason.
- [ ] Implement the interfaces above, preserving the global constraints.
- [ ] Run `.venv/bin/python -m unittest discover -s tests -q && PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -q && npm test --prefix control-plane/frontend && npm run build --prefix control-plane/frontend && npm run build --prefix website && npm run check --prefix website`; require exit 0. Run opt-in PostgreSQL/runtime contracts with isolated fixtures where applicable and record skips explicitly.
- [ ] Inspect the diff and commit this task with its tests and documentation.

## Execution and completion

Implement inline in the existing isolated `codex/retire-legacy-runtime` worktree; the live planning worktree remains unchanged until rollout. Tasks are sequential because worker, configuration, schema and UI changes share contracts. Perform a whole-branch independent review before publishing. Do not claim retirement complete until the live preservation comparison, restart check and qualification pass. A failed or skipped required check leaves the corresponding checkbox open.
