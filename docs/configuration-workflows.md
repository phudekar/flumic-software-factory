# Workflow, label and manifest configuration

This guide explains the fields that determine how work is routed, queued, retried
and interpreted. Pair it with the [worker setting reference](configuration-workers.md)
and [Control Plane configuration reference](configuration-control-plane.md).
The [generated task inventory](../worker/factory/task_defaults.py) contains the
per-stage code defaults; checked-in `config/workflow.yaml` can override them.

## Where edits take effect

For standalone workers, edit the manifest/configuration files and restart every
participating worker. For managed workers, use **Configuration → Factory settings**
for shared task scheduling, routing, retry and label settings. Save a new factory
revision, select that revision in each participating worker profile, save the
profiles, and restart their workers. A factory edit alone does not update an
already registered worker or an in-flight task. Register compatible shared
workflow definitions across the deployment; there is no automatic rolling drain.

Only `model` and `additional_system_prompt` are worker-local task overrides.
Use **Worker profiles** or **Worker prompts** for those values; save and restart
the linked workers. These do not change routing or task execution timeouts.
The Control Plane records configured/loaded revisions and receipts; saving a
revision is not evidence that a worker has loaded it.

## Manifest and inheritance fields

These fields describe a standalone manifest. The saved worker-profile editor
accepts `settings` and `tasks`, not a complete manifest.

| Field | Values and example | Execution effect |
| --- | --- | --- |
| `version` | Required integer `1` | Selects the supported document schema. Other versions fail startup validation. |
| `includes` | List of relative paths, e.g. `[config/runtime.yaml, config/models.yaml]` | Loads fragments in order before inline values. Paths resolve relative to the including file and must remain inside the root manifest directory. Missing files, duplicate YAML keys and cycles fail loading. |
| `defaults` | Settings mapping, e.g. `repository: example/app` | Supplies settings to every selected worker profile; profile values win. Credentials and unknown names are rejected. |
| `workers` | Mapping of profile names to settings, e.g. `simple: {worker_type: simple}` | Defines valid `WORKER_PROFILE` selectors. The mapping must contain the selected name. A profile name is not a worker instance ID. |
| `extends` | Existing profile name, e.g. `simple` | Inherits another worker profile before applying child values. Invalid or cyclic inheritance fails. Allowed on workers, not `defaults`. The selected profile's local `tasks` mapping replaces the inherited local `tasks` mapping before local task overrides are applied; retain other parent overrides explicitly if needed. |
| `task_defaults` | Partial task settings, e.g. `schedule_timeout_seconds: 86400` | Overrides built-in defaults across all known tasks. Does not add a new stage or change Python dependencies. |
| `task_templates` | Named partial task settings, e.g. `coding: {worker_type: developer, timeout_seconds: 3600}` | Reusable task options. Even unused templates are validated. |
| `tasks` | Mapping of known task IDs to overrides | Changes options for existing Python-defined tasks. Unknown task names fail validation; adding a YAML entry cannot create a new workflow stage. |
| `template` | Name in `task_templates`, e.g. `coding` | Applies the named template to a top-level task entry. Not a field of the resolved task or a permitted worker-local task override. Unknown templates fail validation. |

Task precedence, from lowest to highest: built-in per-task defaults →
`task_defaults` → selected `task_templates` entry → top-level task override →
selected worker's local model/prompt override. Worker settings resolve from
`defaults` → parent profile → child profile. Includes recursively merge mappings;
lists and scalars replace. There is no `${VARIABLE}` interpolation in YAML.

### Bootstrap profile envelope

The shared files under `config/worker-profiles/` use another envelope:

| Field | Example | Meaning |
| --- | --- | --- |
| `version` | `1` | Supported bootstrap format. |
| `name` | `Developer` | Human-readable saved profile name; does not select Hatchet worker routing. |
| `profile` | `simple` or `developer` | Base profile selected from the factory revision. |
| `worker.worker_type` | `developer` | Standalone registration role; must match `profile`. |
| `worker.worker_name` | `flumic-developer` | Nonempty standalone worker registration name. Managed worker instances have their own assigned names. |
| `worker.path_policy` | `source` or `repository` | Standalone repository read/write policy. See [worker settings](configuration-workers.md). |
| `document.settings` | `developer_agent: codex` | Worker-local settings imported into a saved profile. |
| `document.tasks` | `plan_issue: {model: gpt-5-nano}` | Local model/prompt overrides only. |

On an empty Control Plane profile store, these templates seed saved profiles.
Editing a bootstrap file does not overwrite existing database profiles. Managed
profile `id`, `revision`, parent/history IDs and `factory_revision_id` identify
saved versions; use IDs returned by the API, not copied installation-specific
IDs. See [profile operations](control-plane.md#factory-settings-and-migration).

## Task fields

The defaults below are the `TaskSettings` schema fallbacks. Individual stages
already override several values (for example, coding has a 3,600-second timeout
and planning has concurrency 1). Inspect the effective task and
[task defaults](../worker/factory/task_defaults.py), not only these fallbacks.
Source: [TaskSettings and loader](../worker/factory/configuration.py),
[Hatchet task registration](../worker/factory/adapters/orchestration/hatchet.py).

### `worker_type`

Default `simple`; allowed `simple` or `developer`. Example: set
`generate_and_open_draft_pr.worker_type: developer` to route coding to developer
workers. Hatchet requires a matching scheduling label whose key is the global
`worker_label_key`. No matching live worker means queued work, not an automatic
fallback to another role. Changing a profile's coding backend does not change its
registration role. Keep routing consistent across workers.

### `timeout_seconds`

Default `120`; integer 1–2,592,000 seconds. Example `3600` for a coding task.
Sets Hatchet's execution timeout for one attempt. It is separate from queue wait,
API request timeout, CLI subprocess timeout and retry count. A larger value lets
a long stage run longer but also delays timeout recovery. Durable wait tasks use
long limits (30 days in built-in defaults). It is not a preemptive timeout for
local orchestration; local application code still uses it to bound lock waits.

### `schedule_timeout_seconds`

Default `86400`; integer 1–2,592,000 seconds. Example `7200` permits two hours in
the Hatchet queue waiting for capacity. It does not give an executing stage extra
time. Too small a value can expire legitimate work behind a busy developer
worker; increasing it does not provide more workers or fix missing routing labels.
Local orchestration has no Hatchet scheduling queue.

### `lock_wait_seconds`

Default `30`; integer 0–3,600 seconds. Example `0` fails immediately when another
owner holds the same application's work-item claim; `60` tolerates a short
overlap. Applies only to operations that take an exclusive claim. The actual
wait is capped at `min(lock_wait_seconds, max(0, timeout_seconds - 1))`. Exhaustion
raises `OperationBusy`; Hatchet may retry according to `retries`. This is not a
lease lifetime, workflow concurrency limit, or SQLite database connection timeout.

### `retries`

Default `2`; integer 0–20. Example `1` allows one retry after the first Hatchet
attempt, for up to two attempts. `0` allows only the initial attempt. More
attempts can recover transient failures but can repeat model calls and increase
cost. Application claims and durable state still govern idempotency. This does
not change SDK HTTP retries (`api_max_retries`) or coding rounds
(`max_patch_iterations`). Local orchestration does not emulate Hatchet retry policy.

### `backoff_factor`

Default `2.0`; number 1–100. Example `2.0` supplies an exponential retry factor
to Hatchet. A larger factor increases spacing between repeated failed attempts,
subject to `backoff_max_seconds`; it does not slow successful stages or change
API SDK retries. Use the engine's actual attempt timing when investigating
retries rather than assuming this value is a delay measured in seconds.

### `backoff_max_seconds`

Default `30`; integer 1–86,400. Example `120` caps Hatchet retry backoff at two
minutes. Relevant when retries are enabled. It does not cap the stage's runtime,
queue wait or durable polling interval. Engine retry settings are registered at
worker startup and are not live-edited by saving a profile.

### `concurrency`

Default `null`; optional integer 1–1,000. Example `1` serializes executions under
the registered task concurrency rule; `4` allows a larger task limit. `null`
omits this additional numeric task limit. It does not remove per-issue
serialization, workflow limits or worker capacity constraints. Some tasks combine
this numeric limit with issue-key serialization. Adding worker containers alone
cannot bypass a task limit of 1. This setting governs Hatchet execution; it does
not create threads for local orchestration.

### `model`

Default `null`; a nonempty model identifier using letters, digits, `.`, `_`, `:`,
`/` or `-`. Example `gpt-5-nano` on `plan_issue`, or `gpt-6-luna` on a Codex coding
task. `null` uses the selected backend's worker-level model. An override is used
by model calls inside this task, with the backend already chosen by the worker;
it does not switch an API adapter into Codex/Claude or translate model IDs between
providers. Deterministic tasks make no model calls. Verify availability with the
provider/account; accepted identifier syntax does not guarantee support.

### `additional_system_prompt`

Default `''`; string up to 16,000 characters. Example:

```yaml
additional_system_prompt: |
  Follow existing module boundaries and naming conventions.
  Include the checks actually run in the implementation summary.
```

Adds task-specific guidance to API system messages or CLI operator guidance.
It supplements built-in instructions and required output schemas. It does not
change timeout/retry/routing behavior, loosen path/file checks or make a
deterministic stage invoke a model. Multiple model calls within a task can
receive the same guidance, so a feedback task's prompt should suit both feedback
triage and implementation. Save through the selected worker profile or Worker
prompts page, then restart the worker.

## GitHub lifecycle labels

All six fields are nonempty strings and their values must be distinct. They are
shared factory policy, separate from Hatchet's `worker_label_key`. Changes do not
rename labels on existing GitHub issues or PRs; migrate existing labels and adopt
the same settings across workers before continuing an active workflow.
Source: [labels schema](../worker/factory/configuration.py),
[issue/PR transitions](../worker/factory/application/service.py).

### `labels.needs_discussion`

Default `needs-discussion`; example `flumic-needs-discussion`. Added when required
clarification blocks progress; pending-issue scans skip it. The discussion wait
checks for the configured readiness label rather than treating the passage of
time as approval.

### `labels.ready_for_agent`

Default `ready-for-agent`; example `flumic-ready`. Signals that required
clarification is resolved. Planning/triage can use the collected discussion and
resume. It does not override the issue's closed/in-progress/already-planned
checks or grant repository permissions.

### `labels.pr_open`

Default `agent-pr-open`; example `flumic-pr-open`. Marks issues/PRs associated
with agent work already in progress. Scans avoid re-enqueueing marked issues;
PR association and merge verification use this policy. Renaming it without
migrating existing items can disrupt discovery and duplicate suppression.

### `labels.pr_reviewed`

Default `agent-pr-reviewed`; example `flumic-pr-reviewed`. Marks the agent's
completed review work and participates in the review skip checks. It is not a
GitHub branch-protection approval or proof that all CI checks passed.

### `labels.plan_created`

Default `agent-plan-created`; example `flumic-planned`. Marks a parent issue whose
sub-issue plan was already created. Planning and pending scans use it to avoid
replanning the same parent.

### `labels.plan_subtask`

Default `agent-subtask`; example `flumic-subtask`. Applied to generated sub-issues.
Planning recognizes the marker and avoids decomposing a subtask again; the
subtask can still proceed through implementation.

## Worked examples

This standalone manifest fragment adjusts scheduling and one local prompt. Merge
it into an existing valid manifest; omitted settings inherit their defaults.

```yaml
version: 1
workers:
  simple:
    worker_type: simple
  developer:
    worker_type: developer
    developer_agent: codex
    tasks:
      generate_and_open_draft_pr:
        model: gpt-6-luna
        additional_system_prompt: |
          Prefer focused changes and report the checks performed.
task_defaults:
  schedule_timeout_seconds: 86400
  lock_wait_seconds: 30
  retries: 2
  backoff_factor: 2.0
task_templates:
  coding:
    worker_type: developer
    timeout_seconds: 3600
tasks:
  generate_and_open_draft_pr:
    template: coding
    retries: 1
    concurrency: 2
```

For a managed worker profile, only the local part belongs in the profile document:

```yaml
settings:
  developer_agent: codex
tasks:
  generate_and_open_draft_pr:
    model: gpt-6-luna
    additional_system_prompt: |
      Prefer focused changes and report the checks performed.
```

A task has up to `1 + retries` Hatchet attempts. Each attempt can contain multiple
coding rounds and API requests, each with its own HTTP retry budget. Increasing
all limits together can substantially increase latency and model spend. Use the
run's stage/attempt filters and recorded model calls to identify the specific
limit you are trying to change.

## Price-file fields and accounting effects

`pricing_file` points to operator-maintained JSON read when the worker observer
is constructed. Restart workers after updating it. Example rates below are
**illustrative configuration values, not current provider prices**:

```json
{
  "currency": "USD",
  "unit": "per_million_tokens",
  "models": {
    "openai/example-model": {
      "input": 1.0,
      "cached_input": 0.1,
      "output": 4.0,
      "cache_creation": 1.25
    }
  }
}
```

| Field | Effect and valid examples |
| --- | --- |
| `models` | Required mapping of exact `provider/model` keys, e.g. `openai/example-model`. Missing model entries leave estimated cost unknown; they do not block model execution. |
| `input` | Nonnegative finite USD per million ordinary input tokens, e.g. `1.0`. Required with `output` to calculate an estimate. |
| `output` | Nonnegative finite USD per million output tokens, e.g. `4.0`. Reasoning tokens included in reported output are not added again. |
| `cached_input` | Optional nonnegative finite cached-input rate, e.g. `0.1`. When present, missing cached-input usage makes the estimate unknown. When omitted, known cached tokens use the input rate. |
| `cache_creation` | Optional nonnegative finite cache-write rate, e.g. `1.25`. Reported nonzero cache-creation usage without this rate leaves the estimate unknown. |
| `currency`, `unit` | Documentation convention `USD` and `per_million_tokens`; the estimator uses these units internally and does not convert currency from the metadata. |
| `verified_at`, `source`, `sources`, `pricing_scope`, `notes` | Operator-maintained provenance/assumptions. Not interpreted as execution switches or a live price feed. |

Known estimates flow into telemetry and retained Control Plane accounting.
Updating a price file does not recalculate existing records. CLI-reported USD
remains a separate figure. Prices do not select a model, enforce a spend cap or
change provider billing; unsupported/missing accounting remains unknown rather
than zero. Malformed files or invalid rates can fail observer startup.
See [accounting semantics](observability.md#cost-estimates) and
[estimator implementation](../worker/factory/adapters/pricing.py).
