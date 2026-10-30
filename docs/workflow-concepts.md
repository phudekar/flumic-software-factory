# How projects, workflows and workers fit together

A project connects the repositories you work on with the workflows you want to
run. Authorized pools and running workers supply execution capacity. Start with
these relationships before configuring individual fields.

![Relationships between project, repository, workflow, stages, pools and runs](images/workflow-concepts.svg)

| Concept | Meaning | Example |
| --- | --- | --- |
| Project | Owns repository connections, workflows, configuration and credentials; authorizes access to shared pools | Payments |
| Repository connection | A saved pointer to an existing GitHub/GitLab repository, with an alias and source credential | Alias `backend` → `your-team/payments-api` |
| Workflow | A definition of stages, dependencies and input bindings | Issue to pull request |
| Workflow template | A starting definition used to create an editable workflow; not a repository or a live runtime dependency | Standard issue, feedback and verification recipes |
| Stage | One action with inputs, a repository, required capabilities and execution policy | Plan a change using `issue.plan@1` |
| Worker pool | Shared infrastructure policy: role, allowed action/executor versions and per-worker slots | `simple` pool for planning/review |
| Worker | A running process with installed capabilities that can execute eligible actions | Two developer workers sharing a pool |
| Workflow run | One execution of an immutable workflow revision with pinned configuration, repositories and grants | Run issue 42 against a saved recipe |
| Attempt | An individual execution attempt for a stage | First implementation attempt, then a permitted retry |

**Target repository** in the template installer means the connection that the
created workflows operate on. It does not mean a GitHub template repository,
a repository to clone, or a new repository to create.

## How a stage works

A stage receives inputs, executes its action through an eligible worker or job,
and produces an output consumed by later bindings. Dependencies say **when** it
may start; input bindings say **what data** it receives. A dependency does not
implicitly pass every earlier output into the next stage.

![Issue recipe stages and the worker role for each](images/workflow-stages-explained.svg)

The standard issue recipe uses these stages:

| Stage | Receives | Produces / effect | Worker role |
| --- | --- | --- | --- |
| Load | Issue number and repository context | Loaded issue state | Simple |
| Plan | Issue command and loaded state | Structured plan and acceptance criteria | Simple |
| Triage | Loaded state and plan | Decision about the work, including possible child issues | Simple |
| Implement | Issue command and triage output | Code-change / publication result | Developer |
| Review | Issue command and implementation result | Review result | Simple |
| Record | Issue command and review result | Recorded workflow outcome | Simple |

Planning can produce child work, and human review can require clarification or
feedback. Completing the parent run does not imply every child PR is merged.
The feedback and merge-verification recipes are separate workflows used for
follow-up work; they are selected at launch, rather than extra stages silently
appended to the issue recipe.

## Why pools and repositories are separate

A repository tells a stage **where to work**. A pool tells the runtime **which
capabilities may execute it**. Workers supply the actual processes. A stage can
reference a pool without any worker being available: pool creation and project
authorization alone do not provide capacity.

For example, Payments and Billing can each connect several repositories and
publish their own workflows while authorizing the same developer pool. Their
runs retain their own pinned context and grants. Adding eligible workers to the
pool increases execution capacity; it does not change either project's models,
prompts or repository selection.

## What changes when you edit something

- Editing a template-created workflow does not change the template installer.
  Reinstalling retains existing recipes instead of overwriting your edits.
- Publishing a workflow creates a new revision. Existing runs keep their launch pins.
- Saving project/repository settings affects newly resolved launch context; it does
  not rewrite an active run's configuration.
- Advertising a pool capability does not install code in a worker image.

## Follow one complete example

1. **Connect:** create a project and connect its `backend` repository.
2. **Provide capacity:** create/authorize pools and start compatible workers.
3. **Define:** use a template or author stages and their bindings.
4. **Run:** choose a saved revision, input and follow-up workflows explicitly.
5. **Observe:** inspect stage state, attempts, logs, model calls, tokens and cost.

Continue with the [numbered walkthrough](user-walkthrough.md). For custom graphs,
see [workflow development](workflow-development.md). Moving an existing
installation is covered separately in [migration guidance](project-runtime-operations.md#roll-out-from-a-current-checkout).
