# Worker configuration

`worker-config.yaml` is a small manifest that includes related YAML files:

| File | Edit here for |
| --- | --- |
| `config/runtime.yaml` | Repository, integrations, logging, state, telemetry |
| `config/models.yaml` | Model choices, token budgets, API retries and timeouts |
| `config/workflow.yaml` | Schedules, labels, patch limits, task templates and overrides |
| `config/worker-profiles/default.yaml` | Simple worker definition, local settings, prompts and startup script |
| `config/worker-profiles/developer.yaml` | Coding worker definition, local settings, prompts and startup script |

Compose mounts the manifest and `config/` tree read-only. Restart workers after
editing them. Credentials stay in `.env` or the Hatchet CLI profile; unknown
configuration fields, including credential fields, are rejected.

`includes` paths are relative to the file containing them and must remain within
the manifest directory. Files merge in listed order; later values override earlier
values, and inline values override included values. Mappings merge recursively;
scalars and lists replace earlier values. Duplicate keys within a file, missing
files, and cyclic includes fail at startup. A standalone monolithic YAML file
remains supported. There is no environment interpolation or executable template
language in these files.

Workers reuse common settings through `defaults` and profile `extends`. Compose
selects `simple`/`developer`; its telemetry overlay selects their observed
variants. The loader generates these compatibility variants from the shared profile
files, adding the OpenTelemetry observer. Changing the base profile updates its
observed variant too. The same files seed control-plane profiles on fresh installs;
existing saved profiles remain database-owned. The `worker` block defines standalone
registration, while `document` defines editable worker settings and task prompts.

Tasks reuse `task_defaults` and named `task_templates`. Effective settings are:
built-in compatibility defaults → `task_defaults` → selected template → task
overrides. For example, both coding tasks use `template: coding`, and only the
initial coding task overrides its retry count. Add `model` or
`additional_system_prompt` to a task or template when needed. Omitted empty prompts
and null models retain their defaults.

Profile values override defaults. Observed profiles inherit changes made to their
base profile. Task model overrides take precedence over `openai_model`,
`codex_model`, or `claude_model`. A null task model uses the backend default;
an empty `claude_model` uses the Claude CLI's default. Model identifiers must be
supported by the selected backend and account. Direct API models must have a
known tokenizer in the installed `tiktoken` package.

For example, edit the corresponding entries in the files listed above:

```yaml
defaults:
  scan_cron: '*/10 * * * *'
  log_level: DEBUG
  max_patch_iterations: 8
  openai_model: gpt-5-nano
workers:
  developer:
    developer_agent: codex
    path_policy: repository
tasks:
  generate_and_open_draft_pr:
    worker_type: developer
    timeout_seconds: 3600
    retries: 1
    model: gpt-6-luna
    additional_system_prompt: |
      Follow the repository's existing style.
      Add tests for changed behavior and describe the checks performed.
```

Additional prompts supplement built-in instructions. API adapters append them to
the system message; CLI adapters add them as operator guidance before the task
instructions. Built-in safety checks and output validation still apply. Prompts
apply to model calls within that task, including feedback triage and coding;
steps that make no model calls have no prompt to customize. Custom adapters
receive `Settings` and can read the task-local configuration with
`factory.configuration.current_task()`.

The checked-in `config/workflow.yaml` provides stage-specific prompt suggestions:
planning produces focused subtasks and verifiable acceptance criteria; triage
asks only blocking questions; implementation follows existing conventions and
reports actual validation; review prioritizes evidence-backed defects; feedback
processing addresses actionable requests without broadening scope. These prompts
apply to all model calls made within their respective stage. Deterministic tasks
(scans, event routing, waits, state recording, and merge verification) have empty
prompt fields because they do not call a model. Editing those fields does not
change the task's Python behavior or create a model call.

`tasks.*.retries` is the number of retries after the first Hatchet attempt (zero
means one attempt). `api_max_retries` controls HTTP retries inside each API call.
`max_patch_iterations` limits coding rounds; it is independent of either retry
setting. Hatchet timeouts cover whole task attempts; `cli_timeout_seconds` covers
one CLI subprocess invocation. Local execution uses the same prompts/models and
application limits; engine retry/concurrency/schedule settings apply to Hatchet.

Patch limits can be narrowed to 1–5 files per iteration, 1–20 iterations, and at
most `max_file_bytes` bytes per file (default 65,536 / 64 KiB). The aggregate file limit is the product of iterations
and files per iteration, with the shared schema's absolute 100-file ceiling.
Token budgets apply to direct API calls; CLI tools manage their own model
contexts. The native Codex healthcheck uses `codex_model` from the same file.

All workers in a deployment must share the task routing and label settings.
Changing GitHub status labels does not rename labels on existing issues/PRs;
move existing labels before switching an active deployment. `worker_label_key`
controls the Hatchet scheduling label, and each task's `worker_type` selects
its required worker role. Cron expressions use Hatchet's five-field syntax. `hatchet_tls_strategy` selects
`tls`, `none` (local development), or `mtls`; private certificate/key paths remain
SDK credential environment settings.

For a local process, set `WORKER_CONFIG_FILE` to the YAML path and
`WORKER_PROFILE` to the desired profile. These selectors and credentials are the
only configuration environment variables consumed by deployed workers. Legacy
non-secret `.env` settings no longer override this YAML. The developer image
includes both supported CLI tools, so runtime backend selection needs no image
variant. Rebuild images for this migration to include the split configuration loader and files; subsequent YAML edits need only a restart.

The checked-in telemetry settings use the internal collector endpoint. For a
host process, change `telemetry_endpoint` to `http://localhost:4318`, and use host
paths for `state_path` and `pricing_file`. Exporter authentication headers can
still be supplied through standard OTLP credential environment variables.


## Startup hooks and control-plane profiles

Worker profiles can set `startup_script` (POSIX shell, at most 32 KiB) and
`startup_timeout_seconds` (1–3600, default 600). The script runs before workflow
registration on every process start; failure aborts startup. Standalone YAML
example:

```yaml
workers:
  developer:
    startup_script: |
      python -m pip install --no-cache-dir -r /data/requirements.txt
    startup_timeout_seconds: 600
```

The script has the worker container's permissions, mounts, environment and network.
Keep it idempotent and keep credentials in environment variables. Its shell
exports do not persist into the Python worker process. See the
[control-plane guide](control-plane.md#startup-scripts) for logging and failure
behavior.

The control plane separates shared factory revisions from saved worker profiles.
A saved profile contains worker-local settings, per-task model/prompt overrides
and startup hooks; workers created from it follow the latest profile revision on
restart. Factory routing, engine retries/timeouts/concurrency, repository and
state settings remain separate and pinned by factory revision.


`max_file_bytes` defaults to **65,536 bytes (64 KiB)** and can be set per worker
profile. It must be a positive integer. The CLI prompt/collector, API coding
prompt/validator, repository source reads and final publication policy all use
the same configured value. Limits count UTF-8 bytes, not characters. Received
coding file paths and byte counts are logged before validation, including files
that are rejected; file contents are not included in these messages.
