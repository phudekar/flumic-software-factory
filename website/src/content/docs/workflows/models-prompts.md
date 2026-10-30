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

Open **Worker prompts**, select a profile, then the stage. Edit Markdown in **Write** and inspect **Preview**. Saving creates a profile revision; restart linked workers to apply it. History is read-only, and cloning creates a separate profile for experiments.

Stage guidance supplements built-in instructions and output schemas. It does not disable guardrails. Deterministic stages have no model call; putting text in their prompt field does not change their Python behavior.

Example guidance for a planning stage:

```markdown
Break the acceptance criteria into small, independently reviewable subtasks.
State prerequisites explicitly and keep changes to the requested scope.
Ask a question only when the answer blocks safe implementation.
```

## Validate before adopting

Use **Validate** for complete profile validation without publication and **Format YAML** for indentation. Formatting preserves draft comments, but saved profiles store parsed values rather than original YAML formatting. Use a clone to evaluate new models/prompts without changing every worker attached to the existing family.

[Settings and precedence](/flumic-software-factory/handbook/worker-configuration/) · [Profile revisions](/flumic-software-factory/control-plane/profiles/)

## Artifact quality and evaluation

See [stage prompts, artifact examples and paired evaluation](/flumic-software-factory/handbook/prompt-quality/) for expected outputs, honest validation reporting, effective prompt ownership and deliberate adoption.
