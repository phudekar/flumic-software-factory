# Worker settings reference

This reference covers every field of [`Settings`](../worker/factory/settings.py). A **code default** is the value obtained by constructing `Settings()`; it is not necessarily the value from the checked-in manifest. The shipped [`worker-config.yaml`](../worker-config.yaml) includes [`runtime.yaml`](../config/runtime.yaml), [`models.yaml`](../config/models.yaml), [`workflow.yaml`](../config/workflow.yaml), and the [simple](../config/worker-profiles/default.yaml) and [developer](../config/worker-profiles/developer.yaml) profile templates.

## Where values live and when they apply

Standalone startup calls `Settings.load()`: `WORKER_CONFIG_FILE` defaults to `worker-config.yaml`, and `WORKER_PROFILE` defaults to `simple`. YAML `defaults` and the selected `workers.<profile>` accept Settings field names. Profile values override defaults; `extends` resolves parent profiles first. For example:

```yaml
version: 1
defaults:
  repository: example/project
  openai_model: gpt-5-nano
workers:
  simple:
    worker_type: simple
    max_patch_iterations: 4
```

Includes merge mappings recursively, in order, followed by inline values. Worker inheritance itself is a shallow mapping override. Unknown settings fail validation. Profile template files use `document.settings` for local settings and a separate `worker` block for registration/path policy; [`profile_fragment`](../worker/factory/configuration.py) compiles those templates into standalone worker entries.

Managed workers fetch a resolved saved snapshot from the control plane at **every process start**, including Docker restart. They fail startup if fetching that snapshot fails. The control plane separates settings using [`LOCAL_FIELDS` and `BUILD_FIELDS`](../control-plane/backend/app/services.py):

- **Factory** means shared configuration, tied to the worker's selected factory revision. It cannot be supplied as a worker profile's local setting.
- **Profile** means a worker-local override in `document.settings`. Omitted fields inherit resolved factory/profile defaults.
- **Build** means a profile setting used in managed image construction as well as the resolved snapshot.
- **Runtime metadata** means a value supplied by the control plane, rather than a normal profile override.

All ordinary runtime fields are read into an immutable settings object at startup; saving YAML or a managed profile does not update a running Python process. Restart affected workers to load changes. Shared workflow registration settings should be consistent across workers registering the same workflow definitions. Build settings additionally require managed image preparation/container replacement to change installed software. Standalone YAML does not build or modify its container image: install dependencies in that image yourself.

Managed profile reference YAML files bootstrap an empty profile store; editing them does not overwrite saved profiles. Edit saved profiles through the control plane. `Settings.from_env()` is a legacy partial constructor, not the normal worker entry point: arbitrary uppercase versions of the fields below do **not** override YAML. Credentials belong in environment injection, not these non-secret documents. See the [loader](../worker/factory/configuration.py), [entry point](../worker/worker.py), and [composition root](../worker/factory/bootstrap.py).

## Repository, execution and registration

### `repository`

**Factory; code/shipped default:** `phudekar/software-factory-test`. Must match `owner/name`, with letters, digits, `_`, `.`, and `-` in each component. This is the repository operated on by scans and local issue runs; the service rejects incoming commands for a different repository. Example: `repository: example/payments`. Changing it redirects subsequent work after restart; ensure injected GitHub credentials can access that repository. It does not clone or migrate state on its own. See [service](../worker/factory/application/service.py).

### `worker_type`

**Factory registration; code default:** `simple`. Allowed values: `simple`, `developer`. Shipped simple/developer templates set their matching types. This determines the worker's advertised Hatchet label value, with `worker_label_key`; task `worker_type` determines the desired target label. Example: `worker_type: developer` allows this worker to receive developer-routed tasks. The adapter registers the workflow collection for either type; the type is routing metadata, not a switch that automatically chooses Codex or limits registered workflows. Pair it with `developer_agent` and task routing deliberately. See [Hatchet registration](../worker/factory/adapters/orchestration/hatchet.py).

### `developer_agent`

**Profile; code default:** `api`; shipped simple: `api`, developer: `codex`. Selects built-in coding implementation: `api` sends selected-file context through the API model client; `codex`/`claude` operate on a repository archive through their CLI. Example: `developer_agent: claude` with an installed Claude CLI and credentials. It does not change the planner/reviewer factories. A nonempty `coding_factory` takes precedence and permits a custom agent name; managed builds then treat the tool as `api` and do not install a built-in coding CLI. With managed `base_image`, this selection affects generated build inputs, so switching CLI adapters requires preparing the corresponding image. See [agents](../worker/factory/adapters/agents.py) and [image builder](../control-plane/backend/app/adapters/image_builder.py).

### `orchestrator`

**Factory; code/shipped default:** `hatchet`. Allowed: `hatchet`, `local`. `hatchet` builds the engine client and starts the long-running worker. `local` executes one issue synchronously using the positional issue number, e.g. `python worker.py 42` with `orchestrator: local`; it can create issues and PRs. Local dispatch reports pending follow-up requests instead of maintaining durable timers. Hatchet cron, routing, deduplication and engine concurrency/retry controls do not govern this local runner. Startup scripts still run before either branch. See [local runner](../worker/factory/adapters/orchestration/local.py).

### `worker_name`

**Runtime metadata in managed mode; standalone registration setting; code default:** `''`. Empty uses `software-factory-<worker_type>-worker`; shipped standalone templates explicitly use those names. Managed snapshots supply a unique worker name. Example: `worker_name: software-factory-developer-east`. Used for Hatchet registration and the OTEL service/worker identity; it does not route tasks (labels do that). A saved profile cannot override this managed identity. See [snapshot](../control-plane/backend/app/services.py) and [OTEL observer](../worker/factory/adapters/otel.py).

### `hatchet_tls_strategy`

**Factory; code/shipped default:** `tls`. Allowed: `tls`, `none`, `mtls`. Passed to Hatchet `ClientTLSConfig` at client creation. Example: `hatchet_tls_strategy: none` for an engine endpoint intentionally using plaintext transport. This chooses the SDK transport strategy; it does not provision certificates, change the endpoint, or configure GitHub/model HTTP clients. Relevant only to Hatchet orchestration. See [create_client](../worker/factory/adapters/orchestration/hatchet.py).

### `scan_cron`

**Factory; code/shipped default:** `'*/5 * * * *'`. Registers the pending-issue and open-PR-feedback scans with Hatchet. The application checks only that there are five whitespace-separated fields; actual cron semantics are delegated to the engine. Example: `scan_cron: '*/10 * * * *'` reduces scheduled scan frequency. It does not change `discussion_poll_seconds`, and local mode does not register a scheduler. Restart workers to re-register the schedule. See [Hatchet workflows](../worker/factory/adapters/orchestration/hatchet.py).

### `max_patch_iterations`

**Profile; code/shipped default:** `10`; range `1–20`. Maximum incremental coding rounds in the built-in API/CLI agents, not workflow attempts. Example: `max_patch_iterations: 4` with `max_files_per_iteration: 3` yields a final publication cap of 12 changed files. Each round may make a model/CLI call; task retries can run a fresh attempt. Reaching the round limit without a complete patch fails rather than publishing an incomplete result. Custom adapters receive settings but are responsible for their own round behavior; final service policy still enforces the product-based file cap. See [bootstrap](../worker/factory/bootstrap.py), [API client](../worker/model_client.py), and [CLI agent](../worker/cli_agent.py).

### `path_policy`

**Factory; code default:** `source`; shipped simple: `source`, developer: `repository`. `source` uses the explicit source-path allow/deny policy, including special cases for dependency manifests/tool configs; it is not simply a file-extension filter. `repository` permits safe relative repository paths, including files excluded by the source policy, while rejecting traversal, absolute paths and `.git` components. Example: `path_policy: repository` can permit a coding patch to a lockfile or Dockerfile. This is final publication policy, not a sandbox granting access outside the repository. Size, duplicate-path and safe-text checks still apply. See [policy](../worker/factory/domain/policy.py) and [path predicates](../worker/factory/domain/safety.py).

### `state_path`

**Factory; code/shipped default:** `/data/factory.sqlite3`. Location opened by the default SQLite state adapter for application state and issue ownership/locking. Example: `state_path: /data/team-a.sqlite3`. Persist the containing directory if state should survive container replacement. Changing the path selects another store; no migration is performed by the setting. Managed control telemetry also derives its durable spool directory from the parent: `<parent>/control-spool/<worker-id>`. A custom `state_factory` may interpret or ignore the value. See [state adapter](../worker/factory/adapters/state.py) and [control publisher](../worker/factory/adapters/control.py).

## Adapter extension points and logging

Custom `module:callable` examples below describe standalone extension points.
Managed configuration validates adapter values against the approved registry
built from the bootstrap document's `defaults` fields ending in `factory`, plus
an explicit allowance for `factory.adapters.otel:otel_observer`. Installing a
custom module or adding image dependencies alone does not approve its use in a
saved profile/factory revision. An operator must deliberately update that
bootstrap registry and recreate the API/reconciler before the managed save can
accept the callable; existing saved configurations are not rewritten. The code
must also be installed in the worker image. See
[configuration validation](../control-plane/backend/app/services.py).

Factories use `module:callable` import paths and are trusted installed Python code. The loader checks the import target is callable; factory-specific interface compatibility is exercised when services use it. No dependency installation follows merely from naming a module.

### `log_level`

**Profile; code/shipped default:** `INFO`. Case-insensitively validated against `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`; applied as the Python root logging threshold at startup. Example: `log_level: DEBUG` includes lower-severity diagnostic records, where emitted. It does not change provider generation or the managed conversation-capture mode. See [logging configuration](../worker/factory/adapters/telemetry.py).

### `log_format`

**Profile; code/shipped default:** `json`. Allowed: `json`, `text`; chooses the structured JSON formatter or text formatter for the worker's logging handler. Example: `log_format: text` is convenient for reading standalone console output. It does not select an observer/exporter or alter model responses. See [logging configuration](../worker/factory/adapters/telemetry.py).

### `planner_factory`

**Profile; code/shipped default:** `factory.adapters.agents:openai_model`. Supplies the planner model client used for planning/triage. Example: `planner_factory: company.adapters:planner` loads that callable and passes the resolved Settings object when creating a planner. The built-in uses `openai_model` and API limits regardless of `developer_agent`. See [bootstrap](../worker/factory/bootstrap.py) and [service ports](../worker/factory/application/ports.py).

### `reviewer_factory`

**Profile; code/shipped default:** `factory.adapters.agents:openai_model`. Supplies the reviewer model client. Example: `reviewer_factory: company.adapters:reviewer` selects an installed reviewer implementation without changing coding. The built-in uses API model selection/budgets, including task-level model overrides. See [bootstrap](../worker/factory/bootstrap.py).

### `coding_factory`

**Profile; code/shipped default:** `''`. Empty chooses the built-in adapter selected by `developer_agent`; nonempty overrides it. Example: `coding_factory: company.adapters:coder`. The callable receives Settings and must return the coding-agent interface/capabilities expected by the application. Managed builds skip automatic Codex/Claude installation for custom factories: use build setup to install their dependencies. Final patch policy still applies. See [bootstrap](../worker/factory/bootstrap.py), [ports](../worker/factory/application/ports.py), and [image builder](../control-plane/backend/app/adapters/image_builder.py).

### `observer_factory`

**Profile; code/shipped default:** `factory.adapters.telemetry:logging_observer`. Selects instrumentation implementation; for example `observer_factory: factory.adapters.otel:otel_observer` enables the OTEL observer. The loader also creates `simple-observed` and `developer-observed` standalone profiles through the template compiler. Managed composition wraps the selected observer with `ControlObserver` when `FACTORY_CONTROL_ENDPOINT` is present; choosing the logging observer does not disable managed telemetry. See [bootstrap](../worker/factory/bootstrap.py), [logging observer](../worker/factory/adapters/telemetry.py), and [OTEL](../worker/factory/adapters/otel.py).

### `state_factory`

**Factory; code/shipped default:** `factory.adapters.state:sqlite_state`. Creates the state-store adapter from Settings at service construction. Example: `state_factory: company.state:shared_store` selects an installed implementation of the state port. The built-in consumes `state_path`; changing the factory does not copy existing SQLite state into the new backend. See [state adapter](../worker/factory/adapters/state.py) and [ports](../worker/factory/application/ports.py).

## Models, coding bounds and request limits

These fields control the built-in adapters. A custom factory can interpret them differently. API request limits are separate from CLI subprocess limits, engine task execution/queue timeouts, workflow retries, and total coding rounds.

### `openai_model`

**Profile; code default:** `gpt-4.1-mini`; **checked-in manifest/simple profile:** `gpt-5-nano` (also inherited by the developer profile's API planner/reviewer). Selects the default model for the OpenAI-backed planner, triage, reviewer and API coding client. Example: `openai_model: gpt-5-nano`. A non-null task `model` overrides it for a call. Model identifiers permit letters, digits, `.`, `_`, `:`, `/`, and `-`; validation does not verify provider availability or model capabilities. This field does not select a CLI model. See [model client](../worker/model_client.py).

### `codex_model`

**Profile; code/shipped default:** `gpt-6-luna`. Passed to the Codex CLI for Codex coding unless the current task provides a model. Example: `codex_model: gpt-6-luna`. Also used by the Codex healthcheck when the built-in Codex adapter is selected. It does not affect API planning/review or install a particular CLI release. General Settings model identifier validation applies; provider acceptance is determined at execution. See [CLI agent](../worker/cli_agent.py) and [healthcheck](../worker/codex_healthcheck.py).

### `claude_model`

**Profile; code/shipped default:** `''`. Empty leaves model selection to the Claude CLI; nonempty or task-level `model` is passed as its model argument. Example: `claude_model: claude-sonnet-4-5` illustrates a model identifier, not an availability guarantee. Used only by the built-in Claude coding adapter; it does not change `reviewer_factory`. General Settings model identifier validation applies. See [CLI agent](../worker/cli_agent.py).

### `max_files_per_iteration`

**Profile; code/shipped default:** `5`; range `1–5`. Bounds file changes in each built-in coding round and contributes to the final unique-file cap `max_patch_iterations × max_files_per_iteration`. Example: `2` with 4 rounds permits at most 8 final files; repeatedly editing one file does not add unique final paths. The API validates response changes; CLI compares each round's workspace changes and accumulated output. This is unrelated to the planner's fixed prompt instruction to select up to five context paths. See [API coding](../worker/model_client.py), [CLI coding](../worker/cli_agent.py), and [publication policy](../worker/factory/domain/policy.py).

### `max_file_bytes`

**Profile; code/shipped default:** `65536` (64 KiB); positive integer. Bounds UTF-8 bytes per source/output file, used by GitHub retrieval, built-in API/CLI output checks, and final patch validation. Example: `max_file_bytes: 32768` rejects an added/modified file larger than 32 KiB; character count can be lower than byte count for Unicode text. This is not a total context/patch byte budget and does not increase CLI archive/output hard caps. See [GitHub client](../worker/github_client.py), [CLI agent](../worker/cli_agent.py), and [policy](../worker/factory/domain/policy.py).

### `max_context_tokens`

**Profile; code/shipped default:** `32000`; positive, and strictly greater than `max_completion_tokens`. API request-budget check includes reserved completion tokens. Example: `max_context_tokens: 48000` with `max_completion_tokens: 16000` leaves 32000 tokens for counted input. Over-budget API requests fail rather than automatically summarizing/truncating content. This operator budget does not assert the model's actual context window and does not constrain Codex/Claude CLI context use. See [token budgeting](../worker/factory/adapters/token_budget.py) and [API completion](../worker/model_client.py).

### `max_completion_tokens`

**Profile; code/shipped default:** `16000`; positive and strictly below `max_context_tokens`. Reserved in the API budget and sent as `max_completion_tokens` to Chat Completions. Example: `max_completion_tokens: 8000` with a 32000 context budget leaves more input headroom but less response allowance. Applies to all built-in API calls, not just patch output; a smaller allowance can make a complete JSON result harder to produce. It is not sent to coding CLIs. See [API client](../worker/model_client.py).

### `api_max_retries`

**Profile; code/shipped default:** `2`; range `0–20`. Passed to the OpenAI SDK client, controlling SDK retry behavior for API requests. Example: `api_max_retries: 0` removes this client retry layer. Workflow `tasks.<name>.retries` is independent and may repeat the whole task after failure; coding iterations are another distinct loop. This setting does not retry a failed CLI subprocess. See [client construction](../worker/model_client.py).

### `api_timeout_seconds`

**Profile; code default:** `90.0`; shipped: `90`; positive. Timeout supplied to the OpenAI SDK client, not a wall-clock deadline for an entire workflow. Example: `api_timeout_seconds: 120` allows a longer SDK request timeout; SDK retry behavior and multiple calls can make a task take longer. Coordinate with the task execution timeout. Does not bound CLI processes or managed bootstrap fetches (the latter uses a separate fixed timeout). See [client](../worker/model_client.py) and [managed loader](../worker/factory/configuration.py).

### `cli_timeout_seconds`

**Profile; code/shipped default:** `240`; positive. Timeout used for each built-in coding CLI process. Example: `cli_timeout_seconds: 600` permits a coding round to run longer, provided its engine task timeout also allows it. Multiple rounds may each consume this time; authentication has its own timeout. This does not change API SDK timeouts, task retries or the startup hook timeout. See [CLI process runner](../worker/cli_agent.py).

## Workflow integration

### `workflow_concurrency`

**Factory; code/shipped default:** `3`; positive. Adds a workflow-local concurrency expression to the multi-task `software-factory-issue-agent` Hatchet workflow. Example: `workflow_concurrency: 1` reduces concurrent instances of that workflow. It is not a global count of every task, subprocess or worker. Separate issue-key serialization and task-level concurrency expressions still apply; increasing it does not bypass either or create more worker capacity. Local mode does not apply Hatchet concurrency controls. See [workflow registration](../worker/factory/adapters/orchestration/hatchet.py).

### `workflow_dedup_ttl_seconds`

**Factory; code/shipped default:** `86400`; positive. Passed as the fallback TTL to Hatchet status-based idempotency for the issue workflow, keyed by repository/issue identity. Example: `workflow_dedup_ttl_seconds: 7200` changes the fallback lifetime to two hours. It is active-run duplicate suppression; completion normally releases the key. It is not a guaranteed daily cooldown, task timeout or replacement for issue locks/concurrency. Exact expiry/release semantics belong to the Hatchet SDK/engine; a TTL alone does not promise exactly-once GitHub effects. Local dispatch has no corresponding engine idempotency mechanism. See [idempotency configuration](../worker/factory/adapters/orchestration/hatchet.py).

### `discussion_poll_seconds`

**Factory; code/shipped default:** `30`; positive. Sleep interval between checks in both durable waits: issue clarification/readiness and PR review/merge. Example: `discussion_poll_seconds: 60` reduces polling frequency and can increase recognition latency. It is not the overall wait deadline: task timeouts control that separately. Local mode returns pending wait requests instead of sleeping/rechecking. See [durable waits](../worker/factory/adapters/orchestration/hatchet.py).

### `worker_label_key`

**Factory; code/shipped default:** `worker_type`; must contain non-whitespace text. Used as both the worker registration label key and required desired-worker label key on tasks. Example: `worker_label_key: factory_role` requires consistently re-registering workers/workflows; tasks still use their configured `worker_type` as the label value. A mismatched key/value can leave tasks waiting for a compatible worker. This has no relationship to GitHub issue labels under `labels`. See [routing](../worker/factory/adapters/orchestration/hatchet.py).

### `labels`

**Factory; code default:** a `Labels()` object; the shipped mapping uses the same six values. GitHub workflow labels must be unique and nonempty. Example: `labels: {ready_for_agent: ready-for-agent}` in standalone YAML overrides that nested label while the model supplies omitted defaults. See the complete [labels reference](configuration-workflows.md#github-lifecycle-labels) for all keys, defaults and transitions. Changing names requires aligning existing repository labels/issues as well as restarting workers; the setting is distinct from Hatchet worker routing.

### `tasks`

**Mixed surface; code default:** `{}` with `Settings.task(name)` falling back to built-in task defaults; normal YAML loading materializes validated settings for every known task. Factory-owned fields govern routing, timeouts, locking, retries/backoff and task concurrency; worker profiles can override only `model` and `additional_system_prompt`. Example: a profile `document.tasks.plan_issue.model: gpt-5-nano` changes that task's model after restart. See the complete [task reference](configuration-workflows.md#task-fields) for schemas, all task defaults, templates and merge precedence. Local mode does not reproduce Hatchet scheduling/retry/task-context behavior.

## Telemetry and accounting

### `telemetry_endpoint`

**Factory; code/shipped default:** `http://factory-signoz-ingester:4318`. Base OTLP HTTP endpoint consumed by the OTEL observer, which appends `/v1/traces`, `/v1/metrics`, and `/v1/logs` after trimming trailing slashes. Example: `telemetry_endpoint: http://collector:4318`. Do not include a single signal's suffix as the base. This does not select the observer or change the separate managed control-plane endpoint. With only the default logging observer, this field does not enable OTLP export. See [OTEL observer](../worker/factory/adapters/otel.py).

### `deployment_environment`

**Factory; code/shipped default:** `development`. Exported as the OTEL resource attribute `deployment.environment`. Example: `deployment_environment: staging` changes telemetry grouping after restart; it does not change credentials, repository selection or runtime isolation. Used by the OTEL observer. See [resource setup](../worker/factory/adapters/otel.py).

### `pricing_file`

**Factory; code default:** `''`; shipped runtime manifest: `/observability/model-pricing.json`. Optional pricing-catalog path used to estimate model cost by the OTEL/control observers. Empty supplies no catalog: the estimator has an empty model-rate mapping and cannot produce priced estimates. Example: `pricing_file: /data/model-pricing.json` requires the file to exist inside the worker. This affects estimates, not provider billing, model selection or a spending limit; unknown model/usage combinations can remain unpriced. See [pricing](../worker/factory/adapters/pricing.py), [OTEL](../worker/factory/adapters/otel.py), and [control accounting](../worker/factory/adapters/control.py).

### `control_revision`

**Runtime metadata; code default:** `''`. Managed snapshots inject the resolved configuration revision and include it in emitted control events/model accounting for attribution. Example of resolved metadata: `control_revision: <resolved-revision>`. It is excluded from normal local profile/factory split settings; changing this string does not select/load a configuration revision. The control plane selects revisions and then stamps the snapshot. Standalone operation normally leaves it empty. See [snapshot](../control-plane/backend/app/services.py) and [control observer](../worker/factory/adapters/control.py).

### `conversation_capture`

**Profile; code/shipped default:** `redacted`. Allowed: `redacted`, `metadata_only`, `disabled`. In managed control recording, `redacted` records redacted submitted request/final response payloads with model-call metadata; `metadata_only` omits conversation bodies but still emits `model.completed`; `disabled` suppresses those control model-completion records. Other workflow events, log forwarding, and a delegated observer are separate and are not disabled by this mode. Example: `conversation_capture: metadata_only` retains usage/outcome metadata without submitted/result text. CLI coverage is prompt and final result, not internal reasoning or a complete tool transcript. Redaction is applied by the implementation; this setting is not a guarantee that arbitrary sensitive text can be safely submitted. Standalone logging alone has no managed conversation publisher. See [CapturedCall/Recorder](../worker/factory/adapters/control.py).

## Managed image construction and process startup

The five **Build** fields below belong to `BUILD_FIELDS`. They are used by the managed Docker image path only when `base_image` is nonempty. The builder hashes worker source/build inputs and the pulled base image identity into a content-addressed tag; unchanged inputs can reuse a verified cached image. Model names and startup scripts are not build inputs. Changing `developer_agent` or `coding_factory` can also change the selected tool build input. See [image builder](../control-plane/backend/app/adapters/image_builder.py), [Docker adapter](../control-plane/backend/app/adapters/docker.py), and [Dockerfile template](../worker/image/Dockerfile.template).

### `base_image`

**Build/profile; code default:** `''`; shipped simple: `python:3.12-slim`, developer: `node:24-bookworm-slim`. Nonempty chooses the managed build's `FROM`; empty uses legacy prebuilt role images (`hatchet-test-worker:latest` or `hatchet-test-developer-worker:latest`). Example: `base_image: node:24-bookworm-slim`. Validated as a limited Docker reference syntax, at most 255 characters. Build scripts support Debian, Ubuntu and Alpine; a syntactically accepted image can still fail build due to distribution or dependency incompatibility. The builder pulls the reference before hashing and pins that build to resolved content; a mutable tag may resolve differently on later preparation. Standalone YAML does not replace its running image.

### `system_packages`

**Build/profile; code/shipped default:** `[]`. Up to 100 apt/apk package names; each must match lowercase `[a-z0-9][a-z0-9+._-]{0,99}`. Example: `system_packages: [jq, make]`. Names are deduplicated/sorted in the generated package list and installed alongside the runtime's baseline packages using the base distribution's package manager. Package availability is distribution-specific. Values are package names, not shell commands, flags or version expressions using `=`. Requires image preparation, not just restarting Python. See [dependency installer](../worker/image/install-system-dependencies.sh).

### `image_setup_script`

**Build/profile; code/shipped default:** `''`; at most 32768 encoded bytes. Trusted `/bin/sh -eu` commands run during managed image build after worker requirements and selected CLI installation. Example: `image_setup_script: 'python3 -m pip --version'` validates an available command without changing runtime startup. Nonzero exit aborts the build. The script is part of image-cache inputs; installed dependencies persist in that image. Worker integration credentials are not supplied to build/pull; use this for non-secret setup. A startup hook instead runs in the runtime container on each process start. See [Dockerfile template](../worker/image/Dockerfile.template).

### `codex_version`

**Build/profile; code/shipped default:** `'0.163.0'`. Pinned semantic version matching three numeric components with optional prerelease suffix; floating `latest` is rejected. Example: `codex_version: '0.163.0'`. The managed build installs `@openai/codex@<version>` only when `developer_agent: codex` and no custom coding factory is selected. It selects CLI software, not `codex_model`. With a legacy prebuilt image or standalone container, editing YAML does not reinstall the binary. Both version files enter managed build inputs even when the corresponding CLI is not selected. See [tool installer](../worker/image/install-coding-tools.sh).

### `claude_version`

**Build/profile; code/shipped default:** `'2.1.306'`. Same pinned-version syntax as `codex_version`. Example: `claude_version: '2.1.306'`. Installs `@anthropic-ai/claude-code@<version>` during managed builds only for `developer_agent: claude` without custom `coding_factory`. It does not set the Claude model or API planner/reviewer dependencies. Changing it requires preparing an image to affect installed software; legacy/standalone images need their own build updates. See [tool installer](../worker/image/install-coding-tools.sh).

### `startup_script`

**Profile; code/shipped default:** `''`; at most 32768 encoded bytes. Nonblank trusted shell script runs as `/bin/sh -eu -c ...` after logging configuration and before service construction/orchestration registration, on every process start in both managed and standalone mode. Example:

```yaml
startup_script: |
  python -m pip install --no-cache-dir -r /data/requirements.txt
startup_timeout_seconds: 600
```

Commands inherit the container environment, network, permissions and mounts; make repeated runs safe. Shell exports remain in the subprocess and do not update the already-running Python process environment. Failure/timeout aborts worker startup before task registration. The implementation continuously drains output, retains up to 64 KiB for redacted logging, and kills the script's process group on completion/timeout, including background children; do not use it to launch a persistent sidecar. Runtime filesystem changes are subject to container lifecycle/mount persistence. No image build occurs merely from editing this field. See [startup hook](../worker/factory/startup.py).

### `startup_timeout_seconds`

**Profile; code/shipped default:** `600`; integer `1–3600` (booleans rejected). Bounds only the startup shell process. Example: `startup_timeout_seconds: 120` aborts startup if that script exceeds two minutes. No script means no hook execution, so this timeout does not delay registration. It does not affect API requests, coding subprocesses, Hatchet task execution, or control-plane bootstrap fetching. Process cleanup/output reader joining can add a small amount of time after the subprocess deadline. See [startup hook](../worker/factory/startup.py).

## Repository provider metadata

### `repository_provider`

**Repository authority; code default:** `github`. Allowed values: `github` or `gitlab`. Chooses the installed repository adapter and source credential purpose. Example standalone YAML: `repository_provider: gitlab`. Shared runs derive this from the selected repository revision; behavior overrides cannot switch it. A GitLab run requires a worker that advertises GitLab capability. Old workers with no provider declaration remain GitHub-only. Existing GitHub snapshots omit this new default to preserve their wire format. In legacy mode, changing the provider requires restarting the affected worker and deliberately registering its provider-qualified workflows. GitLab.com is supported; self-managed hosts are excluded.

### `repository_host`

**Repository authority; code default:** `''` (resolved from the provider). Only `github.com` and `gitlab.com` are allowed for their matching provider. Example: `repository_host: gitlab.com`. This is an identity and outbound-host restriction, not a configurable proxy or arbitrary API URL. The Control Pane derives the fixed host from the saved connection. A mismatched provider/host fails validation before source operations. GitLab HTTPS redirects are refused.

### `gitlab_project_id`

**Repository authority; code default:** `null`. GitLab requires a strictly positive integer, for example `gitlab_project_id: 12345678`; GitHub must not supply it. The Control Pane resolves the real ID with the selected connection's GitLab credential, appends a validated immutable repository revision, and pins it into new runs. Do not guess an ID or use an issue/MR IID. This stable ID scopes source requests, state and claims even if namespaces change. A path reused by a different numeric project is rejected; create a new connection for that project. Current-path validation blocks publication and trigger acceptance until a rename is reconciled.

### `repository_check_policy_json`

**Pinned repository policy; code default:** `'{}'`. A JSON string configuring GitLab verification. Example standalone YAML:

```yaml
repository_check_policy_json: '{"required_pipeline_sources":["push","merge_request_event"],"required_status_names":["security"]}'
```

The empty object requires the `push` pipeline source and no named external statuses. The Control Pane uses the structured `check_policy` field on the connection; workers receive its serialized value in their run pin. Every configured pipeline source must have a successful pipeline for the exact commit SHA and target ref, including required nonoptional jobs and accessible same-project children. Optional failures may be ignored; skipped jobs alone do not prove execution, and a blocking manual job keeps verification pending. Required external status names must also succeed for that SHA/ref. Missing, inaccessible, cross-project child or truncated checks fail closed. Each list must contain unique names; pipeline sources cannot be empty and pipeline sources are limited to 16 entries and external status names to 32 entries. Increasing requirements can keep verification pending until the source supplies every required proof. Later policy edits affect new runs; an already accepted run retains its prior policy.
