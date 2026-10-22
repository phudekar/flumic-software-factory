---
title: "Profiles, prompts and factory settings"
---

## Saved profiles

**Configuration → Worker profiles** lists each profile at its latest revision. Edit a profile, validate, and **Save revision** to append an immutable version. **Version history** opens read-only historical versions; return to latest to edit or clone the historical version.

**Clone as new profile** creates another identity. Several workers can share a profile; saving affects the settings they load on their next start/restart. It does not hot-reload their running process. Concurrent stale edits return a conflict so another revision is not overwritten.

![Saved profile selector, history and worker behavior YAML](/flumic-software-factory/images/control-plane-configuration.png)

## Factory settings

Shared settings own repository/policy, execution state, labels, routing, scan cron, workflow concurrency and engine retry/timeout definitions. Profiles pin a factory revision. To adopt a new shared revision, choose it in each participating profile, save, then restart the relevant workers with consistent definitions. Automated coordinated rollout/drain is not implemented.

## Bootstrap versus managed configuration

On a fresh profile store, repository YAML seeds the initial profiles. Later edits to bootstrap YAML do not overwrite database-owned profiles. UI edits create database revisions and do not change Git files. Standalone Compose workers continue reading the mounted manifest and files.

## YAML and Markdown editors

![Worker prompt editor with profile revision, workflow task and Markdown guidance](/flumic-software-factory/images/control-plane-prompts.png)

The selected profile's planning guidance is shown in Write mode. Selecting a task or previewing Markdown does not save a revision.

Validate checks syntax and complete server-side rules without creating a revision. Invalid YAML disables Save/Clone. Format adjusts the current draft; comments survive draft formatting but not parsed-value storage. Image build and worker behavior have separate YAML mappings. Worker prompts provide a Markdown Write/Preview interface and character limit.

[Bootstrap profile format](/flumic-software-factory/handbook/bootstrap-profiles/) · [Prompt customization](/flumic-software-factory/workflows/models-prompts/)
