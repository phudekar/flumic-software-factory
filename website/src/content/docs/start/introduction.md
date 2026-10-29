---
title: "Meet Flumic AI"
---

Flumic AI turns issue requirements into draft pull requests through explicit, observable stages. Python workers use Hatchet for durable execution and GitHub or GitLab.com for collaboration. A local React/FastAPI control plane manages workers and configuration.

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
| Control API + reconciler | Authenticated operator actions, configuration and durable Docker/Kubernetes lifecycle operations |
| PostgreSQL | Project execution state and claims, sessions, revisions, events, lifecycle operations and encrypted credentials/conversations |
| Shared SQLite/files | Legacy static workflow execution state and claims on one host |
| Optional SigNoz | OpenTelemetry traces, metrics, logs and infrastructure dashboards |

## Source hosts and runtime options

GitLab.com issue-to-merge-request workflows are available; see [GitLab setup](/flumic-software-factory/handbook/gitlab/).

- **Kubernetes workflow and worker orchestration:** native Deployments and Jobs with private worker history volumes, pinned runtime authority and scoped cleanup. See [Kubernetes setup](/flumic-software-factory/operations/kubernetes/).

GitHub and GitLab.com work with Hatchet orchestration and Docker or a qualified Kubernetes target. Remote function execution remains planned.

## Scope today

The Control Pane is for a trusted administrator. Project workflows use PostgreSQL execution state and claims across shared workers, including Kubernetes nodes. Legacy static workers retain shared-host SQLite/files. Multi-user roles, verified graceful drain and automatic rolling configuration migration remain future work. Humans review and merge PRs.

[Start with projects](/flumic-software-factory/start/project-onboarding/) · [Understand each module](/flumic-software-factory/start/modules/) · [Start locally](/flumic-software-factory/start/quick-start/) or [explore the architecture](/flumic-software-factory/handbook/architecture/).

[Follow the complete walkthrough: project creation to monitoring](/flumic-software-factory/start/walkthrough/).
