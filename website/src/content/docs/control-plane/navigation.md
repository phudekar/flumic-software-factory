---
title: "Navigate the Control Pane"
---

Open `http://localhost:3400` and sign in using the private administrator password
from `.env.control-plane`. Sessions expire after eight hours.

## Start with a project

The landing page is **Projects**. Choose a project to scope its navigation and
history. **All projects** returns to the workspace. The workspace infrastructure
menu exposes **Shared workers** and **Runtime targets**.

![Current project landing page](/flumic-software-factory/images/control-plane-projects.png)

| Screen | Scope and purpose |
| --- | --- |
| Overview | Selected project's setup, recent runs and known usage |
| Workflows | Versioned project definitions, stage links and explicit launch |
| Workflow runs | Project executions, attempts, outputs, logs and model calls |
| Model history | Retained project request/response exchanges and capture gaps |
| Usage & cost | Known project tokens, reported costs and estimates |
| Repositories, credentials, triggers | Project connection and execution setup |
| Configuration | Project defaults, models, prompts and limits |
| Shared worker pools | This project's authorization to deployment-wide pools |
| Shared workers | Workspace-wide provisioning and runtime lifecycle |
| Runtime targets | Operator-approved Docker/Kubernetes destinations |

The duplicate legacy sidebar and saved-worker-profile screens are removed. Old
unscoped project URLs ask you to select a project; they do not translate old run IDs
or submit work. Project configuration changes affect new admissions, not existing
run pins. Worker restarts change infrastructure, not historical execution settings.

The UI polls bounded views. **Refresh all data** or **Refresh project data** requests
fresh observations. Missing usage is unknown, not zero; a running container is not
proof of Hatchet registration.

[Project walkthrough](/flumic-software-factory/start/walkthrough/) ·
[Operator handbook](/flumic-software-factory/handbook/control-plane/)
