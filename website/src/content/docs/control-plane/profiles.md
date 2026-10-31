---
title: "Configure project behavior"
---

## Models, limits and prompts

Choose a project and open **Configuration**. Use the grouped editor for models,
prompts and execution limits, or advanced document editing for supported fields.
Save deliberately: the server creates a revision and detects stale-parent conflicts.

![Project model configuration](/flumic-software-factory/images/project-configuration-models.png)

Repository overrides, workflow settings and stage overrides refine project defaults.
New run admissions pin their effective settings. Existing runs retain their original
configuration; restarting a worker does not change a run's models or prompts.

## Bootstrap defaults and installed code

Repository YAML supplies initial defaults. Editing bootstrap files does not
replace existing database-owned project configuration. Credentials belong in
project credential versions, never prompt/configuration text.

Shared pools control worker role, actions and capacity. Build images to change
installed code, tools or dependencies, then replace affected runtimes through
lifecycle controls. Project configuration is not an image installation mechanism.

The old per-worker profile selector, cloning UI and **Worker prompts** screen are
retired. Some backend profile/build records remain during staged cleanup; use
project configuration for supported current usage.

[Full settings reference](/flumic-software-factory/handbook/configuration-workers/) ·
[Prompt customization](/flumic-software-factory/workflows/models-prompts/)
