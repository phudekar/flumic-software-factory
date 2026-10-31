---
title: "Troubleshooting and FAQ"
---

## Issue is not picked up

Verify the repository in factory settings, fine-grained token permissions, registered workers for both roles, scan interval and issue labels. Respond to clarification in GitHub and add `ready-for-agent`. Use the project workflow launch form to submit a specific issue. Check runs before repeated submissions.

## Container is running but coding is queued

Check developer-role Hatchet registration and task labels. Inspect build/startup logs for authentication, startup script or registration errors. Increasing simple-worker count cannot satisfy developer-only tasks.

## Profile save had no effect

Running processes retain loaded settings. Managed profiles require next compatible startup/restart; build-affecting changes require image replacement. Standalone Compose workers use repository YAML. Bootstrap files do not overwrite existing saved profiles. Shared settings also require each profile to pin the new factory revision.

## Logs, history or cost are empty

History is not retroactive. Check capture policy, ingestion/export connectivity and time/environment filters. Large/expired conversation bodies can be absent. CLI exchanges are partial by design. Empty cost panels can mean no matching price or missing accounting. Check unpriced/missing counters and truncation.

## Docker or Hatchet appears unavailable

List APIs may return warnings and unknown state. Check Docker connectivity, the development Hatchet profile and worker startup logs. The diagnostic command below performs a real, billable OpenAI completion as well as Hatchet/GitHub connectivity checks:

```sh
./scripts/check-hatchet-connectivity.sh
```

## API returns 401, 403 or 409

401: renew the administrator session or check worker credentials. 403: check CSRF and supplied Origin. 409: reload a stale configuration or inspect an active operation/idempotency conflict. Do not automatically repeat an uncertain workflow action with a fresh key.

## Where are generated PR tests run?

In the target repository's GitHub CI. Configure required checks/branch protection there. Coding tools may run commands in their workspaces; the factory does not run project tests directly in its credential-bearing worker process. A human reviews, approves and merges.

[Full operator guide](/flumic-software-factory/handbook/control-plane/) · [Security and privacy](/flumic-software-factory/operations/security/)
