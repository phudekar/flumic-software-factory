# Software factory architecture migration

## Objective

Separate factory behavior from workflow orchestration, coding tools, and
observability. Keep GitHub as the initial issue and source host, Hatchet as the
production orchestrator, and the current API/Codex/Claude implementations as
initial adapters. Start with one modular application, not separate services.

## Findings from the original implementation

- `worker/worker.py` combines Hatchet declarations, business decisions, provider
  construction, GitHub mutations, recovery, and startup in 1,486 lines.
- Choosing a coding CLI does not replace OpenAI planning, triage, or review.
- Shared contracts and execution limits live in the OpenAI implementation.
- Backend names determine context loading and allowed file paths.
- Importing the worker initializes Hatchet and registers workflows.
- GitHub labels, issue comments, and commit messages also serve as recovery state.
  Check-then-create marker handling is not atomic across concurrent runs.
- Python logging is already a useful boundary, but needs correlated structured
  context and bootstrap-owned configuration.
- Generated changes are not bound to the commit used to prepare agent context.

## Target boundaries

```text
GitHub events / scheduled scan / local commands
                    |
          Orchestrator adapter
                    |
        Application use cases
                    |
     Domain contracts and policies
                    |
       Core-owned integration ports
                    |
GitHub | agents | execution | state | telemetry
```

The core must not import Hatchet or an AI provider SDK. Adapters depend on the
core. Bootstrap reads configuration and constructs integrations explicitly.

### Orchestration

Hatchet tasks translate engine inputs and upstream results into application
calls. The adapter owns DAG registration, routing, scheduling, retries, durable
sleeps, and cancellation behavior. Do not build a universal workflow SDK: another
engine reuses business operations but implements its own execution semantics.
Provide a small local runner to prove that use cases run without Hatchet; it is
not a durable production replacement.

### Application and agents

Extract load, plan, triage, implement, review, feedback, and merge verification
operations. Inject ports rather than constructing clients inside operations.
Select planner, coding agent, and reviewer independently. A provider may
implement multiple ports. Support custom installed Python factories at the
composition boundary without provider-name branching in the application.

Separate CLI workspace/process handling from provider command/authentication
details. A remote agent must also be able to implement the coding port.
Move FileChange, PatchPlan, planning/review models, and errors into the core.
Retain UTF-8 replacement changes initially and explicitly reject unsupported
deletions/binaries; richer change artifacts can follow when needed.

### Policy and context

Select path permissions and limits explicitly through execution policy, not
provider identity. Preserve current API restrictions and CLI defaults while
allowing operators to narrow CLI output policy. Keep content scanning and path
validation shared; keep model token estimation and context budgeting inside
the model adapter.

### State and recovery

The factory owns business progress, plans, issue/PR mappings, feedback cursors,
operation identity, and concurrent-work claims. The engine owns execution history
and durable scheduling. Stable operation keys include repository, work identity,
version, and operation. Retain GitHub markers for recovery of uncertain external
outcomes and compatibility with existing issues.

Database transactions do not include GitHub requests. Persist intent/results,
reconcile before repeating external effects, and make no exactly-once claim.
Serialize mutations to the same issue/PR across scan and webhook executions.
Bind agent input and publication to an immutable base commit; reject stale branch
updates rather than silently applying changes to a different revision.

Use a local database adapter initially if appropriate. Document its deployment
limits; multi-host execution needs a shared transactional database adapter.

### Events and waits

Normalize external events at adapter boundaries, carrying repository identity,
event ID, schema version, and correlation ID. Reconcile current GitHub state
before and during waits, so a missed approval/merge event does not strand work.
Represent abandoned PRs and closed issues explicitly. Parent aggregation,
task cancellation, and plan revision require explicit lifecycle rules.

### Observability

Keep standard Python logging and configure handlers/exporters in bootstrap.
Carry factory run ID, task/stage, attempt, repository, issue/PR, and agent identity
across boundaries. Expose an operation observation port for spans, durations,
and outcomes so monitoring backends can be replaced without business edits.
Redact before export. Durable business state is not diagnostic logging.

## Migration sequence

1. Extract core models, policies, and settings; remove infrastructure side effects
   from imports.
2. Extract implementation first, then other operations; inject ports and keep
   existing Hatchet workflow names and routing.
3. Introduce independent coding/planning/review adapters and remove application
   backend conditionals.
4. Add explicit state, operation claims, recovery, and base revision checks.
5. Normalize event handling and reconcile human waits.
6. Add correlated telemetry and a local runner; validate adapter boundaries.

## Validation

Preserve existing guardrail/provider tests. Add tests with in-memory/fake adapters
for application behavior, plus integration contract tests. Prioritize duplicate
delivery, overlapping runs, publication success followed by recording failure,
feedback retries, concurrent branch changes, already-merged PRs, and clean imports
without credentials. No live GitHub mutation or paid agent request is required
for the test suite.

## Acceptance criteria

- A coding backend requires an adapter and composition/configuration changes.
- Planning and review can be selected independently from implementation.
- Logging/monitoring destinations do not require business logic edits.
- Replacing Hatchet requires orchestration wiring and engine semantics tests,
  while the application use cases remain unchanged.
- Remaining production limitations and deferred capabilities are documented.

Implementation details and current limitations are tracked in
`docs/architecture.md` as migration changes land.
