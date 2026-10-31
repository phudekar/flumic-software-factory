---
title: "Projects, repositories and credentials"
description: "Browse saved setup first, open guided forms when needed, and inspect workflow stages before running."
---

The project setup screens begin with what is already saved. Use **Add** or **Edit**
to open a form; **Cancel** discards the draft without saving. Technical identifiers
and JSON settings remain available under **Advanced**.

## Choose a project

Open **Projects** in the sidebar. Select a project to open its overview and project-only sidebar. **Add project** opens a short form; the project key
is generated from its name and can be adjusted under Advanced before creation.

![Project list with an explicit Add project action and no always-open creation form](/flumic-software-factory/images/control-plane-projects-guided.png)

Selecting a different project changes the scope of its views and forms. Workspace **Shared workers** and **Runtime targets** describe shared
infrastructure. The legacy global project views have been removed.

## Work inside a project workspace

The sidebar switches to this project's **Overview**, **Workflows**, **Workflow
runs**, **Model history**, **Usage & cost** and setup pages. Duplicate horizontal
tabs are removed. **All projects** returns to workspace navigation, where global
shared infrastructure remains available. Refresh in a project refreshes
its scoped data.

![Selected project workspace with its own overview, execution, accounting and setup navigation](/flumic-software-factory/images/control-plane-project-overview-guided.png)

Overview counts repositories, saved definitions and the latest project runs.
Accounting/history requests and exchange links are project-scoped too. A default
project can contain attributed legacy accounting even when it has no dynamic
project runs; these views describe different retained records. Unknown accounting
values stay unknown, and published workflow counts do not imply live capacity.

## Connect a repository

Repository cards show owner/name, provider, workflow alias and saved credential
name. The alias is the short name workflow definitions use, such as `backend`.
**Add repository** opens a new connection; **Edit connection** loads its saved
values. Models and task behavior overrides are optional Advanced settings.

![Repository card showing its provider, alias, assigned credential and Edit connection action](/flumic-software-factory/images/control-plane-repositories-guided.png)

**Credential assigned** describes configuration, not proven provider connectivity.
A missing credential is reported separately. GitLab connections retain their
numeric project identity validation step. Save form changes before validating
GitLab identity; validation reads the saved revision. Credential options remain
restricted to the matching provider and project/repository scope.

Editing creates a revision; admitted runs keep their original repository pins.
A concurrent update can cause a save conflict. Reload deliberately rather than
assuming another operator's changes can be overwritten.

## Manage credentials safely

The credential list shows the saved name, purpose, project/repository scope and
revocation state. **Add credential** opens the input form. The secret value is
password-masked, encrypted on save and never returned for display. Cancel clears
an unsaved secret. A saved key does not prove provider access.

![Credential cards showing names, purpose and scope without exposing secret values](/flumic-software-factory/images/control-plane-credentials-guided.png)

Revoke is an explicit separate action. It changes credential availability;
opening the list or cancelling a form does not revoke anything. Keep original
credentials in your private secret manager, not in documentation or screenshots.

## Browse workflows before editing

Published workflow cards show repositories and stage counts from the saved
revision. Installed factory recipes have readable titles and descriptions; custom
workflow names remain unchanged. Stored names and revision IDs are under Advanced.

![Workflow cards with readable descriptions, stage counts, repository aliases and View/Edit/Run actions](/flumic-software-factory/images/control-plane-workflows-guided.png)

- **View** opens the immutable revision and stage summary.
- **Edit** opens the advanced YAML/JSON authoring controls. Validate before publishing another revision.
- **Run** opens the existing launch form for that saved revision. It does not submit a run automatically.

A revision that cannot load shows an error; do not treat unavailable information
as an empty or successful workflow. Publication and listed pool capabilities do
not establish live worker capacity or complete repository handover.

## Understand stage dependencies

The saved revision displays each stage's role, action, repository and direct
prerequisites. Independent stages may run in parallel; list order alone is not a
promise of execution order. Advanced stage settings and saved YAML remain
available for exact inputs, conditions and other configuration.

![Saved workflow stage overview with explicit prerequisites and expandable technical settings](/flumic-software-factory/images/control-plane-workflow-stages-guided.png)

Running still requires the repository confirmation and valid inputs/bindings in
the launch form. For legacy repositories, follow the explicit
[handover procedure](/flumic-software-factory/handbook/project-runtime-operations/#roll-out-from-a-current-checkout)
before enabling project execution. Browsing or editing setup does not perform
that handover.

These screenshots show the local populated Default project as an example. They
are not a fresh-install baseline and do not prove that its workflows are ready to
execute. See [first project onboarding](/flumic-software-factory/start/project-onboarding/)
and [workflow authoring](/flumic-software-factory/handbook/workflow-development/)
for setup requirements and the full schema.

For the full execution journey, follow [the numbered user walkthrough](/flumic-software-factory/start/walkthrough/), including guided pool setup, first run and monitoring.

Standard issue workflows can be set up without an API console: use **Workflows → Install templates**, review **Repository execution** on the repository card, then select the issue number and saved follow-up revisions in **Guided issue launch**. Each action requires its own explicit submission. Existing templates retain their saved settings; custom/multiple-repository launches keep the advanced JSON form.
