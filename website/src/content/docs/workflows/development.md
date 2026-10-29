---
title: "Build workflows and link stages"
description: "Cookbook for composing project workflows, linking stage inputs and outputs, and extending installed actions."
---

Create project workflows in **Workflows → New workflow** using YAML or JSON.
You can add stages that use installed actions and connect them without changing
worker code. The UI provides source editing, validation and readable stage
summaries; it is not a drag-and-drop designer.

## Follow the workflow cookbook

[The authoring cookbook](/flumic-software-factory/handbook/workflow-development/#cookbook-choose-your-task)
provides recipes for:

- [A complete two-stage workflow](/flumic-software-factory/handbook/workflow-development/#create-and-link-stages), including its run input.
- [Conditional execution](/flumic-software-factory/handbook/workflow-development/#gate-a-stage-with-a-condition) and skip behavior.
- [Parallel branches and a join](/flumic-software-factory/handbook/workflow-development/#fan-out-and-join-a-project-workflow), with [downloadable YAML](/flumic-software-factory/examples/walkthrough/fanout-workflow.yaml).
- [Per-stage models, prompts and credential grants](/flumic-software-factory/handbook/workflow-development/#configure-models-prompts-and-grants).
- [Installing issue, feedback and verification templates](/flumic-software-factory/handbook/workflow-development/#seed-compatible-recipes).
- [Publishing, revising, launching and inspecting runs](/flumic-software-factory/handbook/workflow-development/#publish-revise-and-pin).

For screenshots of the whole setup, follow [walkthrough steps 10–12](/flumic-software-factory/start/walkthrough/#10-publish-the-deterministic-two-stage-workflow--ui).
For real run, activity and model evidence, see [the Hello World example](/flumic-software-factory/handbook/hello-world-live-example/).

## Connect order and data separately

In a stage definition:

```yaml
after: [receive]
inputs:
  message:
    $ref: stages.receive.output.message
```

`after` makes this stage wait for `receive`. The `$ref` passes the value returned
by `receive`. Every producer you read must be a direct parent, even if it is
already a transitive ancestor. A join lists all of its direct parents in `after`.
Bindings refer to stages in the same run; they do not start another workflow.
Standard issue workflows link follow-up runs through explicitly selected issue,
feedback and verification revisions.

## Choose data changes or implementation changes

| Desired change | Starting point |
| --- | --- |
| Add a stage using an installed action or change its dependencies | Edit the project workflow document and publish a new revision |
| Tune a stage's model, prompt, timeout or retries | [Stage fields and behavior overrides](/flumic-software-factory/handbook/workflow-development/#definition-and-stage-configuration) |
| Run your own program as a one-time container action | [Container action contract](/flumic-software-factory/handbook/docker-action-jobs/) |
| Choose Docker or Kubernetes execution | [Operator-configured runtime targets](/flumic-software-factory/operations/kubernetes/); author the same supported executor contract |
| Implement a new typed builtin action or a statically registered Python workflow | [Contributor extension examples](/flumic-software-factory/handbook/workflow-development/#where-the-definition-lives) |

New executable Python behavior requires implementation, tests, registration and
compatible worker images. Merely adding an unknown action ID or an arbitrary
Python import to YAML cannot install it. Publishing a revision does not start
work or rewrite an existing run's accepted definition/configuration.

The diagram below illustrates the Python extension APIs used by the advanced
contributor examples; project workflow documents use `after` and `$ref` as above.

![Python task dependencies and separate-workflow dispatch](/flumic-software-factory/images/workflow-linking.svg)
