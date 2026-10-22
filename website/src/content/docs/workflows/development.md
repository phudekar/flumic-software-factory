---
title: "Build workflows and link stages"
description: "Developer tutorial for implementing and registering new Flumic AI stages, dependency graphs and cross-workflow dispatch."
---

New stages and workflows are Python extensions. The Control Plane edits configuration for known stages; it does not provide a visual workflow designer or create a stage from an arbitrary YAML key.

![Illustrative stage fan-out/fan-in using parents and separate-workflow dispatch using typed commands](/flumic-software-factory/images/workflow-linking.svg)

## Follow the developer tutorial

[Create a stage, register a workflow and link executions](/flumic-software-factory/handbook/workflow-development/) walks through:

- The difference between a task key, an engine stage name and a workflow name.
- Adding typed inputs/results and application operations.
- Registering task options and linking stages with `parents` and `ctx.task_output`.
- Fan-out/fan-in, conditional outcomes and local-runner parity.
- Starting separate workflow runs through dispatch, with correlation and recovery.
- Cron/event triggers, worker routing and Control Plane visibility/input validation.
- Configuration adoption, worker image rollout and tests before a live trigger.

## Choose configuration or implementation

| Desired change | Starting point |
| --- | --- |
| Tune an existing stage's model/prompt | [Saved profiles and worker prompts](/flumic-software-factory/control-plane/profiles/) |
| Tune existing retries, routing, timeouts or concurrency | [Workflow settings reference](/flumic-software-factory/handbook/configuration-workflows/) |
| Add an operation or dependency | [Developer tutorial](/flumic-software-factory/handbook/workflow-development/) |
| Inspect the current graph and task options | [Task inventory](/flumic-software-factory/reference/tasks/) |

Adding a stage changes executable code. Rebuild the affected worker images and deploy compatible definitions across participating workers; changing only a saved profile cannot install that code.
