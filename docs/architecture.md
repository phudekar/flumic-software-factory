# Flumic AI architecture

The [migration plan](architecture-migration.md) records the original analysis,
sequence, and acceptance criteria. This document describes the implemented
boundaries and operational limits.

## Code map

| Location | Responsibility |
| --- | --- |
| `worker/factory/domain/` | Shared models, events, patch policy, and content/path safety |
| `worker/factory/application/ports.py` | Planner, reviewer, coding, repository, dispatch, state, and observation interfaces |
| `worker/factory/application/service.py` | Issue intake, planning, triage, implementation, review, feedback, and merge verification operations |
| `worker/factory/application/operations.py` | Repository validation, operation observation, exclusive claims, and progress recording |
| `worker/factory/adapters/orchestration/hatchet.py` | DAG, task affinity, retries, schedule, durable polling, dispatch |
| `worker/factory/adapters/orchestration/local.py` | One-shot synchronous execution of the same operations |
| `worker/factory/adapters/agents.py` | API and CLI coding adapters and default role factories |
| `worker/factory/adapters/github_events.py` | GitHub payload normalization into versioned factory events |
| `worker/factory/adapters/state.py` | SQLite/file-lock production adapter and in-memory test adapter |
| `worker/factory/adapters/telemetry.py` | Correlated structured logging and default operation observation |
| `worker/factory/adapters/token_budget.py` | OpenAI token estimation and context budgeting |
| `worker/factory/bootstrap.py` | Explicit dependency construction and trusted custom adapter loading |
| `worker/factory/settings.py` | Validated settings; environment reads only through `Settings.from_env()` |
| `worker/worker.py` | Runtime entry point; imports create no clients or workflows |

The existing `github_client.py`, `model_client.py`, and `cli_agent.py` remain
integration implementations. Their locations are retained to avoid unnecessary
launcher/healthcheck churn. `model_client.py` re-exports legacy contract names;
new code imports `factory.domain.models`. `guardrails.py` is a compatibility
facade; the CLI and GitHub implementations no longer import its tokenizer code.

The application has no imports of Hatchet, OpenAI, or concrete adapters. There is
one cohesive application facade with explicit operation methods rather than a
new microservice for each stage. The orchestrator calls these methods with typed
inputs and upstream results, without leaking its execution context into them.

See [worker configuration](worker-configuration.md) for the runtime YAML schema.

## Selecting integrations

Default behavior remains Hatchet plus GitHub, with OpenAI planning/review and
`workers.developer.developer_agent: api|codex|claude` selecting implementation. Each role can now be
replaced independently using an installed Python factory:

```yaml
defaults:
  planner_factory: my_factory.adapters:make_planner
  reviewer_factory: my_factory.adapters:make_reviewer
  coding_factory: my_factory.adapters:make_coding_agent
  observer_factory: my_factory.observability:make_observer
  state_factory: my_factory.persistence:make_state_store
```

Each factory receives a `Settings` object and returns the corresponding port
implementation. Planner/reviewer/coding factories are invoked lazily; unused
roles do not require credentials or initialize model clients. Custom modules
must be installed/copied into the container image. Factory paths are trusted
operator configuration, never values from an issue or webhook. Custom factories
can obtain their own credentials at this integration boundary.

A minimal coding integration has this shape:

```python
from factory.application.ports import AgentCapabilities, CodingRequest
from factory.domain.models import PatchPlan

class RemoteCodingAgent:
    capabilities = AgentCapabilities(context="selected-files")

    def generate(self, request: CodingRequest) -> PatchPlan:
        # Invoke your tool and translate its result into a completed PatchPlan.
        # request.base_commit identifies the immutable input revision.
        ...

def make_coding_agent(settings):
    return RemoteCodingAgent()
```

`selected-files` receives selected UTF-8 source files. `repository-archive`
receives an archive at the same immutable commit instead. Built-in CLI adapters
handle safe extraction, scanning, workspace ownership, execution, and collecting
changes through `cli_agent.py`; they accept an injected process runner so command
execution can be tested/replaced separately. A remote service can implement the
coding port directly without using local subprocesses.

The application scans selected text and validates completed patches before
publication regardless of the coding backend. Custom adapters are trusted code
and must preserve confidentiality checks for any additional data they load,
including scanning archives before sending their contents to a provider.

## Policy and limits

`workers.<profile>.path_policy: source|repository` selects output permissions independently of
agent capability. The checked-in profiles use these defaults: API coding
uses the source allowlist and CLI coding permits safe repository-relative text
paths. Setting `source` can narrow a CLI's publishable output, although that CLI
still sees its complete archive workspace. This setting is an output policy,
not a process sandbox or a network restriction.

`defaults.max_patch_iterations` is read at startup and passed explicitly to the built-in
adapters. Runtime policy caps the aggregate patch at `defaults.max_files_per_iteration` files per configured
iteration, with up to 20 iterations, and `max_file_bytes` bytes per file (default 65,536 / 64 KiB). The shared schema
has an absolute 100-file ceiling. Incomplete patches cannot be published.
Deletions, binaries, renames, and executable-bit changes remain unsupported by
the replacement-content change contract.

## State and recovery

`FACTORY_STATE_PATH` defaults to `/data/factory.sqlite3`. All Compose workers
share the same host `data/` mount and therefore the same state and lock files.
The adapter stores:

- Immutable version-1 issue plans and prepared implementation patches.
- Issue/PR associations, feedback cursors, and latest stage results.
- Operation attempts with status, timestamps, and error type, excluding exception
  contents. An attempt left running after a crash becomes interrupted on the
  next claim of that resource.

Exclusive claims cover every mutating stage for a repository/issue, including
feedback. Nonblocking kernel file locks prevent simultaneous mutations and
release on process exit. Contention raises `OperationBusy`; Hatchet retries
according to the stage configuration. Duplicate execution is safe to reconcile,
but a busy delivery can exhaust its retries while another execution continues.

Prepared implementation changes survive a failed publication. The GitHub adapter
writes a deterministic patch fingerprint into its commit message, so a retry can
recover a successful branch commit and finish creating the PR. Existing PRs
recover their labels/waits and still receive their initial review. Feedback
cursors reconcile factory state with legacy comment/commit markers before
applying changes again. Notification comments may be absent if recording fails
after a feedback commit; the code change is not repeated.

All generation context is loaded at one resolved commit. Initial publication
checks that the base has not changed, feedback updates check the PR head, and
ref updates are non-forced. A stale prepared implementation is invalidated for
regeneration on a later attempt. A branch created by unrelated/older work is
not overwritten just to make a retry succeed.

SQLite and these file locks support workers on **one host with a shared local
filesystem**, including the current Compose setup. Do not use independent
per-host databases or assume network filesystem locking is sufficient. A
multi-host installation needs a shared transactional `FactoryStateStore` adapter
with equivalent claim and recovery semantics. GitHub is outside its transaction;
this implementation does not promise exactly-once external effects.

The database contains prepared source contents and plans. It is ignored by Git
and the Docker build context, but must be protected and backed up as application
data. Version-1 plans are intentionally reused on retry; editing an issue does
not automatically start a new plan version. Plan revision, retention cleanup,
and operator reset tooling are follow-up work.

## Events, waiting, and orchestration

GitHub transport payloads become `FactoryEvent` objects with schema version,
event identity, repository, correlation identity, and issue/PR references.
Ingress uses a supplied delivery ID when present, otherwise a payload digest.
The event identity is metadata, not a claim of transport-level exactly-once
processing. Repository validation and operation recovery remain authoritative.

Hatchet retains the original workflow/task names, concurrency limits, role
labels, and cron triggers. Both clarification and merge waits now reconcile
current GitHub state every 30 seconds through durable sleeps. Missed events,
merge-before-wait, and approval/merge event ordering do not strand completion.
GitHub branch protection remains responsible for required approval rules;
completion independently verifies the merged commit and its checks. Closed,
unmerged PRs finish the waiter with a terminal status recorded in factory state;
they are not automatically reopened or assigned another implementation branch.

To add a production orchestrator, implement factory dispatch requests, wire the
application operations into its task model, and add a bootstrap selection. The
adapter owns durability, retries, routing, scheduling, timeout, and cancellation
semantics. There is no universal workflow DSL to emulate across engines.

A local adapter proves the application can execute without Hatchet. In a copy
of `worker-config.yaml`, set `defaults.orchestrator: local` and
`defaults.state_path: ./data/factory.sqlite3` before running:

```sh
WORKER_CONFIG_FILE=worker-config.local.yaml WORKER_PROFILE=simple \
  .venv/bin/python worker/worker.py 123
```

Export the integration credentials/settings first. This command performs real
GitHub operations and agent calls. It prints the result and pending dispatch
requests; it does not schedule scans, run child issue requests, wait durably, or
resume human review automatically. Embedded callers can invoke `LocalRunner`
and the application's reconciliation operations directly. It is a development
runner, not a production scheduler.

## Logging and monitoring

The default observer emits operation start/end, outcome, duration, and error
type. JSON logs include `factory_run_id`, stage, attempt ID, operation ID,
repository, issue, and PR where applicable. Issue workflow inputs propagate the
factory run ID across tasks and into review waits. Feedback deliveries start
their own correlated processing. `defaults.log_format: text` is available for development;
`defaults.log_level` controls verbosity.

Bootstrap configures standard Python logging; handlers/collectors can consume
logs without application changes. A custom `OperationObserver.observe(stage,
attributes)` context manager can send spans and metrics to another system.
Recognizable secrets are redacted before the default log handler exports data;
exception type is emitted without exception text or tracebacks. The existing
scanner is not a proof that arbitrary source content is nonconfidential.

No vendor monitoring SDK/exporter is required by the core. The optional OTLP
observer exports traces, metrics, and metadata logs; the default remains standard
logging. See [the observability guide](observability.md) for SigNoz deployment,
model usage accounting, dashboards, and current coverage.

## Validation and remaining work

Run:

```sh
.venv/bin/python -m unittest discover -s tests -q
ruff check worker tests
```

Tests include application behavior with fake integrations, provider regression
coverage, SQLite cross-instance claims and crash recovery, publication failures,
stale revisions, duplicated feedback, independent factories, clean imports, and
Hatchet SDK workflow serialization without a server. Token-budget tests require
the normal tiktoken vocabulary cache on first use.

Production alternatives to Hatchet, distributed database adapters, other vendor
telemetry integrations, per-task containers, richer change artifacts, parent issue
aggregation, plan revisions, and cancellation workflows are extension work.
The built-in CLI execution still uses the existing unprivileged subprocess
boundary and shared provider home. No live deployment, paid agent run, or GitHub
mutation is part of the automated validation.
