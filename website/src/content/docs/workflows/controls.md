---
title: "Trigger, pause and resume"
---

Open **Workflows** to inspect registered factory definitions and their supported actions.

![Registered workflow definitions with status and trigger, pause and action-history controls](/software-factory/images/control-plane-workflows.png)

The local factory currently lists 11 registered definitions. This view shows their active status and available controls before any action is submitted.

## Trigger

Choose **Trigger run**, enter YAML input matching the displayed model, replace required null placeholders, and submit. Triggering requires an available compatible worker. The resulting receipt links to the run when the engine returns it. A completed receipt means submission succeeded, not that the workflow finished.

## Pause and resume

Pause prevents new runs starting while existing runs continue. Cron/scheduled triggers default to **Skip missed triggers**; choose **Queue for resume** to retain them. Manual/event triggers queue while paused. Queue TTL defaults to 24 hours and supports up to 30 days. Resume may release queued runs and produce paid calls and GitHub updates.

If native pause capability is unknown or unsupported, the control is disabled. Do not assume a paused workflow stops running tasks or its worker containers.

## Action history and uncertain results

Inspect the latest local action receipts before repeating an action. Transport retries reuse the same idempotency key. An uncertain engine outcome is deliberately not retried automatically because the engine might already have accepted it. Check current state and runs before submitting a fresh action.

[API workflow contracts](/software-factory/api/contracts/#workflow-actions) · [Durability architecture](/software-factory/handbook/control-plane-workflows/)
