---
title: "Choose models and customize prompts"
---

## Independent model choices

| Execution path | Selection | Credentials |
| --- | --- | --- |
| OpenAI planner/reviewer and direct API coding | `openai_model`, task `model` overrides | `OPENAI_API_KEY` |
| Codex coding | `developer_agent: codex`, `codex_model` | Developer-specific OpenAI key if supplied, otherwise OpenAI key |
| Claude coding | `developer_agent: claude`, `claude_model` | `ANTHROPIC_API_KEY` |
| Custom planning/review/coding | Adapter factory references | Adapter-specific integration |

Choose IDs supported by your provider account. A blank Claude model allows the CLI's default. CLI tools manage their own context; direct API calls enforce the configured token budget. Provider/model availability and quotas are independent of worker capacity.

## Prompts for each model stage

Choose a project and open **Configuration** to edit role/task model and prompt defaults. Use workflow and stage overrides for more specific behavior. Saving creates a revision used by new admissions; existing runs keep their pinned prompts. Worker restart is not required to adopt behavior for a newly admitted run.

Stage guidance supplements built-in instructions and output schemas. It does not disable guardrails. Deterministic stages have no model call; putting text in their prompt field does not change their Python behavior.

Example guidance for a planning stage:

```markdown
Break the acceptance criteria into small, independently reviewable subtasks.
State prerequisites explicitly and keep changes to the requested scope.
Ask a question only when the answer blocks safe implementation.
```

## Validate before adopting

Review the guided fields or advanced configuration document before saving. Resolve
stale-revision conflicts explicitly. Evaluate changes with a separate workflow or
stage revision and compare artifacts from newly admitted runs; do not assume an
old run will adopt edited prompts. Keep secrets out of instructions.

[Settings and precedence](/flumic-software-factory/handbook/worker-configuration/) · [Profile revisions](/flumic-software-factory/control-plane/profiles/)

## Artifact quality and evaluation

See [stage prompts, artifact examples and paired evaluation](/flumic-software-factory/handbook/prompt-quality/) for expected outputs, honest validation reporting, effective prompt ownership and deliberate adoption.
