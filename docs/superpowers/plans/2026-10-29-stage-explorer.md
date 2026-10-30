# Inspect Run stage explorer implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement the approved stage explorer with readable, correctly scoped execution evidence.

**Architecture:** Extract run inspection from project-runs into a focused feature, reusing the model-call, activity, accounting and timestamp components. URL state controls view, stage, attempt and detail tabs; existing project APIs remain unchanged.

**Tech Stack:** React 19, TypeScript, React Router, TanStack Query, Vitest/Testing Library, existing CSS.

**Spec:** docs/superpowers/specs/2026-10-29-stage-explorer-design.md

## Global Constraints

- Frontend only; no workflow launches, worker restarts, credential/configuration changes or database migrations.
- No new libraries or backend contracts.
- Use the existing locale/timezone formatter; preserve unknown values and distinct accounting sources.
- Preserve project-scoped run/stage/attempt links, cancellation receipts and canonical refresh behavior.
- Do not label model responses as canonical action output or infer success from dispatch.

## Review Focus

- Mismatched stage and execution IDs must not expose another stage's evidence.
- Run-wide activity/model views must not inherit selected-stage filtering.
- Older retries must remain selectable and default stage selection must choose the latest attempt.
- Missing/error/loading evidence must not masquerade as complete empty results.
- Long captured messages and narrow viewports must remain readable without page-wide overflow.

### Task 1: Stage selection and scoped evidence

**Files:** Modify src/workflow-ui.test.tsx and src/features/project-runs.tsx; create src/features/project-run-inspector.tsx and src/features/project-run-evidence.tsx (under control-plane/frontend).

**Interfaces:** Consumes ProjectRun, PinnedStage, Execution, FactoryEvent, existing ProjectActivity/ProjectModelCalls/ProjectAccounting. Produces ProjectRunInspector({ id: string }) and RunEvidence({ runId: string; stage?: string; execution?: string; mode: 'result' | 'logs' | 'models' }).

- [x] Add integration tests for one selected stage, latest/older attempt selection, deep-link validation, correct evidence query scope and run-wide scope.
- [x] Run targeted tests; confirm missing explorer behavior fails.
- [x] Implement feature extraction, URL navigation, result evidence, flat detail views, stage configuration, run overview and existing cancellation behavior.
- [x] Update obsolete all-stages test to assert the approved one-stage inspector behavior.
- [x] Run the complete frontend tests and production build; expected exit 0.

### Task 2: Responsive presentation, guides and local verification

**Files:** Create control-plane/frontend/src/features/project-run-inspector.css; update relevant docs/images and guide descriptions.

**Interfaces:** Consumes Task 1 inspector class names and existing guide references. Produces responsive stage explorer with updated screenshots.

- [x] Style the stage rail, detail tabs, readable evidence, metadata and responsive horizontal stage selector; retain visible focus and selection.
- [x] Build the frontend and update only the running API's frontend static assets; retain a backup and leave backend/worker processes unchanged.
- [x] Inspect the real clarification run, older attempt/deep links and evidence views through the browser; capture actual updated screenshots.
- [x] Validate frontend tests/build, guide links/assets and git diff whitespace; expected exit 0.
- [x] Perform an independent final review, resolve substantive findings, commit and push the approved changes.
