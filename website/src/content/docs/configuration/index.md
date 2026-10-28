---
title: "Configuration ownership and precedence"
---

## Choose the right configuration surface

| Scope | Authoritative surface | Application |
| --- | --- | --- |
| Credentials | Private `.env` / Hatchet profile / `.env.control-plane` | Runtime injection; never store in profile text |
| Project behavior | Project configuration and repository overrides | New runs pin their resolved values |
| Workflow graph and stage behavior | Published project workflow revision | New runs; admitted runs keep the original revision |
| Shared capacity | Pool membership/capabilities and worker startup settings | Authorize membership; explicitly recreate affected workers for image/startup changes |
| Runtime infrastructure | Operator target settings | Deliberate control-service recreation; existing target pins remain authoritative |
| Standalone worker behavior | Manifest plus `config/` includes | Loaded on process startup |
| Managed worker behavior | Saved worker profile | Loaded on next compatible startup/restart |
| Shared orchestration | Factory revision pinned by profiles | Adopt across participating workers consistently |
| Model prices | `observability/model-pricing.json` | Restart workers after updating rates |

The manifest includes runtime settings, models, workflow definitions and bootstrap profiles. Later files and inline values override earlier values. Mappings merge recursively; lists/scalars replace. Duplicate keys, cycles, missing files, unknown fields and includes outside the manifest directory fail validation. There is no environment interpolation or executable template language in these files.

Task settings resolve from code compatibility defaults → task defaults → named task template → task override. Model overrides at task level take precedence over backend defaults. Worker profiles override relevant shared defaults.

## Project behavior precedence

Project defaults → repository overrides → workflow values → stage values,
subject to trusted deployment limits. This is distinct from the legacy task
configuration precedence above. Editing a repository YAML file does not update a
saved project revision. A configuration save does not hot-patch an admitted run.

Use the [module map](/flumic-software-factory/start/modules/) to find the owner,
configuration guide and diagnostic view for each part of the system, and the
[project runtime guide](/flumic-software-factory/handbook/project-runtime-operations/)
for rollout and revision adoption.

## Field-by-field effects and examples

- [Worker execution settings](/flumic-software-factory/handbook/configuration-workers/): every worker setting, model/backend limits, logging, state, telemetry and image/startup controls.
- [Workflow, task and label settings](/flumic-software-factory/handbook/configuration-workflows/): every task option, scheduling/retry interactions, manifest precedence, lifecycle labels and price-file fields.
- [Control Plane and environment settings](/flumic-software-factory/handbook/configuration-control-plane/): database, authentication, paths, browser origin, worker connectivity, credentials and deployment controls.

Each guide distinguishes code defaults from deployment overrides, explains when an edit takes effect, and includes possible values. Use the saved effective configuration to establish what a particular worker actually loaded.

## Complete reference

- [Worker settings, task/label fields, exact defaults and validation](/flumic-software-factory/reference/configuration/)
- [All task defaults](/flumic-software-factory/reference/tasks/)
- [Manifest and configuration handbook](/flumic-software-factory/handbook/worker-configuration/)
- [Bootstrap profile schema](/flumic-software-factory/handbook/bootstrap-profiles/)
- [Saved profile operations](/flumic-software-factory/control-plane/profiles/)

Code defaults can differ from checked-in YAML defaults. Read both the field inventory and the effective configuration. A `control_revision` is runtime metadata; creation-time labels do not always represent loaded settings.
