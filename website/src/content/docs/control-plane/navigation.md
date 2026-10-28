---
title: "Navigate the control plane"
---

Open `http://localhost:3400` and sign in with the administrator password from `.env.control-plane`. The private launcher-generated file is ignored by Git. Sessions expire after eight hours.

## Overview

Overview summarizes managed workers, the latest page of workflow runs, recorded tokens and known estimated spend. Treat these as bounded views of retained records. A missing price or absent usage is not zero spend.

![Overview with recent runs, worker totals and usage estimates](/flumic-software-factory/images/control-plane-overview.png)

## Project work and global infrastructure

Start in **Projects** for project-scoped setup and execution. Selecting a project
switches the sidebar to its Overview, Workflows, Runs, Model history, Usage and
setup pages. Use **All projects** to return to workspace navigation. **Workers**
and **Runtime targets** describe shared infrastructure outside that project menu. The remaining legacy views
retain static workflow IDs and history; selecting a project does not convert
legacy executions into project workflows.

Follow [First project and workflow](/flumic-software-factory/start/project-onboarding/)
for the setup sequence.

## Where to go

| Screen | Use it for |
| --- | --- |
| Projects | Select a project; manage workflows/runs, repositories, triggers, credentials, project configuration and pool membership |
| Runtime targets | Inspect operator-approved destinations and runtime availability |
| Workers | Create instances, inspect registration, start/stop/restart and read build/startup logs |
| Workflows | Trigger definitions, pause/resume supported workflows, inspect action history |
| Workflow runs | Follow stages, attempts, status and scoped logs/model calls |
| Model history | Find captured model exchanges; open redacted content when retained |
| Usage & cost | Aggregate known token/cost records and see accounting gaps |
| Configuration | Worker profiles, image builds and shared factory settings |
| Worker prompts | Stage-specific Markdown guidance and history |

The UI refreshes through bounded polling. **Refresh all data** requests fresh views; integration warnings indicate unknown state rather than an inferred healthy/idle system. Use the [full operator handbook](/flumic-software-factory/handbook/control-plane/) for field-level behavior and limits.

## Guided setup

See [Projects, repositories and credentials](/flumic-software-factory/control-plane/project-setup/) for the list-first screens, Add/Edit/Cancel forms, workflow cards and stage summaries.
