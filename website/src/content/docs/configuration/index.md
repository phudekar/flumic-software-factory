---
title: "Configuration ownership and precedence"
---

## Choose the right configuration surface

| Scope | Authoritative surface | Application |
| --- | --- | --- |
| Credentials | Private `.env` / Hatchet profile / `.env.control-plane` | Runtime injection; never store in profile text |
| Standalone worker behavior | Manifest plus `config/` includes | Loaded on process startup |
| Managed worker behavior | Saved worker profile | Loaded on next compatible startup/restart |
| Shared orchestration | Factory revision pinned by profiles | Adopt across participating workers consistently |
| Model prices | `observability/model-pricing.json` | Restart workers after updating rates |

The manifest includes runtime settings, models, workflow definitions and bootstrap profiles. Later files and inline values override earlier values. Mappings merge recursively; lists/scalars replace. Duplicate keys, cycles, missing files, unknown fields and includes outside the manifest directory fail validation. There is no environment interpolation or executable template language in these files.

Task settings resolve from code compatibility defaults → task defaults → named task template → task override. Model overrides at task level take precedence over backend defaults. Worker profiles override relevant shared defaults.

## Complete reference

- [Worker settings, task/label fields, exact defaults and validation](/flumic-software-factory/reference/configuration/)
- [All task defaults](/flumic-software-factory/reference/tasks/)
- [Manifest and configuration handbook](/flumic-software-factory/handbook/worker-configuration/)
- [Bootstrap profile schema](/flumic-software-factory/handbook/bootstrap-profiles/)
- [Saved profile operations](/flumic-software-factory/control-plane/profiles/)

Code defaults can differ from checked-in YAML defaults. Read both the field inventory and the effective configuration. A `control_revision` is runtime metadata; creation-time labels do not always represent loaded settings.
