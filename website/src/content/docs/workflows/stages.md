---
title: "Manage stages and execution"
---

## The issue-to-PR path

The main registered DAG loads an issue, plans work and creates sub-issues, triages, generates a draft PR, reviews it, and records the workflow result. Durable tasks handle discussion, review and merge waits. Feedback scanning implements actionable requests on the existing PR branch.

| Stage or activity | Role | Developer control |
| --- | --- | --- |
| Load and triage | Simple | Repository policy, context selection, timeouts and triage guidance |
| Planning / sub-issues | Simple | Planning model, prompt, concurrency and retries |
| Code / draft PR | Developer | Coding adapter/model, prompt, iterations, file limits and timeout |
| Review | Simple | Reviewer adapter/model, actionable findings guidance and timeout |
| Feedback implementation | Developer | Coding guidance, iterations and feedback concurrency |
| Discussion / review waits | Simple | Poll interval and durable timeout; human labels/review remain authoritative |
| Merge verification / recording | Simple | Verification retries and timeouts; deterministic stages do not call a model |

The [task inventory](/flumic-software-factory/reference/tasks/) lists all code defaults. The [workflow configuration](/flumic-software-factory/handbook/worker-configuration/) explains repository overrides.

## Configure task behavior

In repository YAML, `task_defaults` merge with a named template and the task override. Omitted models fall back to the backend's model; `tasks.<name>.model` takes precedence when set. Profile values supplement shared factory definitions.

```yaml
tasks:
  generate_and_open_draft_pr:
    worker_type: developer
    timeout_seconds: 3600
    retries: 1
    additional_system_prompt: |
      Implement the acceptance criteria with focused changes.
      Run relevant checks when tools are available; report actual results.
```

In the UI use **Configuration → Factory settings** for shared routing, schedule, engine retries/timeouts and concurrency. Use **Worker profiles** for worker-local models, provider retries and prompts. Pin the intended factory revision in each participating profile before restarting its workers. Keep shared definitions consistent across the fleet.

## Inspect a stage

Open **Workflow runs**, select a run and a stage, then use **View logs**. Inspect attempt status, worker identity, failure type/message, accounting and recorded exchanges. Selecting an attempt isolates a retry. URL filters survive reloads and can be shared with an authenticated operator.

## What controls apply where?

**Trigger, Pause and Resume** act on a workflow definition. They are not per-stage pause or replay actions. Configuration changes affect future loaded settings; they do not rewrite an in-flight stage. Stopping a worker can interrupt work, which Hatchet may retry.

Read [workflow controls](/flumic-software-factory/workflows/controls/) and the [full lifecycle](/flumic-software-factory/handbook/issue-lifecycle/).
