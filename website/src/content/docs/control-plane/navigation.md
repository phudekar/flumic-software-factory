---
title: "Navigate the control plane"
---

Open `http://localhost:3400` and sign in with the administrator password from `.env.control-plane`. The private launcher-generated file is ignored by Git. Sessions expire after eight hours.

## Overview

Overview summarizes managed workers, the latest page of workflow runs, recorded tokens and known estimated spend. Treat these as bounded views of retained records. A missing price or absent usage is not zero spend.

![Overview with recent runs, worker totals and usage estimates](/flumic-software-factory/images/control-plane-overview.png)

## Where to go

| Screen | Use it for |
| --- | --- |
| Workers | Create instances, inspect registration, start/stop/restart and read build/startup logs |
| Workflows | Trigger definitions, pause/resume supported workflows, inspect action history |
| Workflow runs | Follow stages, attempts, status and scoped logs/model calls |
| Model history | Find captured model exchanges; open redacted content when retained |
| Usage & cost | Aggregate known token/cost records and see accounting gaps |
| Configuration | Worker profiles, image builds and shared factory settings |
| Worker prompts | Stage-specific Markdown guidance and history |

The UI refreshes through bounded polling. **Refresh all data** requests fresh views; integration warnings indicate unknown state rather than an inferred healthy/idle system. Use the [full operator handbook](/flumic-software-factory/handbook/control-plane/) for field-level behavior and limits.
