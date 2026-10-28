---
title: "First project and workflow"
description: "Set up project ownership, shared capacity and a first observable execution."
---

Use this path for a new installation with multiple projects, repository connections
and data-defined workflows. The [profile-based quick start](/flumic-software-factory/start/quick-start/)
remains available for legacy static issue-to-PR workflows.

## 1. Bring up the control services

Clone the repository and prepare private credentials using the prerequisites in
[Quick start](/flumic-software-factory/start/quick-start/#1-prepare-your-environment).
Then run:

```sh
./scripts/start-control-plane.sh api reconciler dynamic-runtime
```

Open `http://localhost:3400` and sign in using `.env.control-plane`. The API stores
configuration and accepts commands; the reconciler manages infrastructure; the
dynamic runtime registers the fixed Hatchet handlers. A working UI alone does
not prove Hatchet connectivity or worker registration. This command also builds
worker images; existing workers need an explicit lifecycle action to adopt them.

## 2. Create a project and connect repositories

Open **Projects**, create/select a project, and use its **Credentials** and
**Repositories** tabs. Add a credential version and validate each repository
connection. Use aliases such as `backend` and `frontend`; workflow documents refer
to those aliases rather than borrowing another project's credentials.

GitHub and GitLab.com have different credential and collaboration requirements.
Follow [GitLab setup](/flumic-software-factory/handbook/gitlab/) for GitLab.com,
or the [runtime API recipes](/flumic-software-factory/handbook/project-runtime-api/)
for the project contracts. Keep secrets out of workflow YAML and screenshots.

## 3. Set behavior and authorize shared capacity

Use **Project configuration** for behavior defaults and **Shared worker pools**
for authorized project membership and installed capabilities. Use global
**Workers** to create/start compatible shared workers and inspect registration.
Simple workers and developer workers provide different action roles. Pool
membership grants access to capacity; it does not install an action or image.

Start with Docker unless you have qualified a Kubernetes target using the
[Kubernetes operator guide](/flumic-software-factory/operations/kubernetes/).
A runtime target supplies operator infrastructure policy; project configuration
cannot supply arbitrary controller credentials or Pod specifications.

See the [configuration ownership map](/flumic-software-factory/configuration/)
for precedence and when edits take effect.

## 4. Publish a small workflow

In the project's **Workflows** tab, start with the examples and action schemas in
[workflow authoring](/flumic-software-factory/handbook/workflow-development/#data-defined-workflow-revisions).
Use real repository aliases and pool IDs from your project. Validate before
publishing. Link stages through `after`, then bind inputs from declared parent
outputs. A dependency orders work; an input binding passes data.

![Project workflow editor showing repository aliases, stage definitions and validation/publication controls](/flumic-software-factory/images/control-plane-project-workflow-editor.png)

This existing installation illustrates the editor; its project names and IDs are
examples, not values to copy. Publish creates an immutable revision. Editing the
graph or configuration later does not rewrite admitted runs.

For a first transport check, prefer the documented deterministic echo example
before enabling repository mutations or paid model calls. Container actions need
an authorized executor, digest-pinned image and the documented result contract.

## 5. Run and inspect the result

Launch the published revision, then open the project's **Workflow runs** tab.
Check stage attempts, assignment, result and canonical terminal state. An accepted
receipt is not proof that compatible capacity was available or that a Job ran.

![Project run showing pinned execution and stage progress](/flumic-software-factory/images/control-plane-project-workflow-run.png)

Follow [project history and accounting](/flumic-software-factory/handbook/project-observability/)
for logs, attempts, trace correlation and model usage. Missing usage or prices are
unknown values, not zero spend. Optional [SigNoz setup](/flumic-software-factory/observability/setup/)
adds infrastructure dashboards and exported telemetry.

## Moving an existing repository

Before switching a repository from legacy workers, pause legacy triggers, settle
active work and explicitly stop the relevant workers. Use the documented
[dynamic-mode handover](/flumic-software-factory/handbook/project-runtime-operations/#roll-out-from-a-current-checkout).
The safety check can reject a handover with conflicting or uncertain work; it
cannot automatically migrate historical SQLite business state.

## If the first run does not progress

| Observation | Check next |
| --- | --- |
| UI loads, but no control handler runs | Hatchet credentials/connectivity and `dynamic-runtime` logs |
| Run accepted, no stage assignment | Pool membership, role, installed action/executor versions and live worker registration |
| Worker starts but does not register | Worker transport settings and startup logs; runtime running and Hatchet registered are separate facts |
| Old settings still appear | Loaded revision and run pins; saving is distinct from adoption/recreation |
| Job fails before action execution | Target qualification, image availability, resource policy and broker diagnostics |
| Run succeeds but cost is missing | Instrumentation coverage and model price entry; inspect accounting gaps |

Use [rollout and recovery](/flumic-software-factory/handbook/project-runtime-operations/)
for uncertain attempts. Do not delete claims or force resource cleanup to make a
run appear complete.
