---
title: "Meet Flumic AI"
---

Flumic AI turns issue requirements into draft pull requests through explicit, observable stages. Python workers use Hatchet for durable execution and GitHub for collaboration. A local React/FastAPI control plane manages workers and configuration.

## Why use it?

- **Keep work structured:** planning produces sub-issues; coding and review operate in separate stages, with routing, retries and timeouts.
- **Choose your tools:** Codex, Claude Code or direct API coding, independently configurable planner/reviewer adapters and task model overrides.
- **Adapt instructions:** versioned profiles carry Markdown prompts, stage-specific guidance and reproducible settings.
- **Add capacity:** run multiple simple or developer workers and control workflow/task concurrency.
- **Understand execution:** connect a run to stage attempts, errors, worker identity, loaded configuration and recorded model exchanges.
- **Inspect AI spend:** drill into reported tokens and known estimated cost for a run, stage or attempt.

## What you run

| Component | Responsibility |
| --- | --- |
| Hatchet | Schedules, task routing, durable waits, retries and workflow status |
| Simple workers | Intake, planning, clarification, review and orchestration tasks |
| Developer workers | Coding, draft PR creation and PR-feedback implementation |
| Control API + reconciler | Authenticated operator actions, configuration and durable Docker lifecycle operations |
| PostgreSQL | Control-plane sessions, revisions, events, operations and encrypted conversations |
| Shared SQLite/files | Factory execution state and claims across workers on one host |
| Optional SigNoz | OpenTelemetry traces, metrics, logs and infrastructure dashboards |

## Scope today

The deployment is for a trusted local developer with Docker access. It does not provide multi-user roles, verified graceful drain, automatic rolling configuration migration or Kubernetes execution. Multiple workers must share execution state on one host. Humans review and merge PRs.

[Start locally](/flumic-software-factory/start/quick-start/) or [explore the architecture](/flumic-software-factory/handbook/architecture/).
