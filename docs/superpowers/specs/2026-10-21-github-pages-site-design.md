# Software Factory website and documentation design

Status: proposed for review; implementation has not started.

## Purpose and audience

Build a public GitHub Pages website for technical developers evaluating, running,
configuring, extending, and troubleshooting this repository. The homepage should
explain the advantages through real product behavior. Documentation should be a
complete operational reference, including the control plane, backend API, and
observability. Repository source and existing guides are the authority.

The user specifically requested prominent coverage of:

- Workflow stages and their configuration and monitoring.
- Scaling simple and developer workers independently.
- Different AI providers/models, including task-level choices.
- Custom instructions for workflow stages and versioned worker profiles.
- Traceability and token/cost visibility per stage within each workflow run.

## Approach and alternatives

Recommended: Astro with Starlight, a custom landing page, and static Markdown/MDX
reference pages. The output is a static GitHub Pages artifact with local search;
no application server or visitor credentials are needed.

Alternatives considered:

- MkDocs: well suited to the Python repository and reference documentation, but
  a bespoke product narrative would require more theme customization.
- A custom Astro site without Starlight: maximum layout freedom, but additional
  work to maintain documentation navigation, search, and accessible interactions.

Keep the website as a separate package under `website/`. Existing runtime workers,
backend, and frontend retain their independent dependencies and build commands.
Use GitHub Pages as requested, rather than the Sites hosting service.

## Visual direction

Take inspiration from Hatchet's product-led introduction and grouped reference
navigation, using original copy, components, and assets. Do not reuse its logo,
customer names, statistics, illustrations, or claims.

Use the control plane's near-black surfaces, mint accent, thin borders, restrained
status colors, and monospace identifiers. Present substantial product screenshots
and readable code examples. The homepage has a deliberate narrative rather than
repeated generic feature cards. Documentation prioritizes reading: persistent
section navigation, page outline, breadcrumbs, search, and previous/next links.

Support desktop and mobile, keyboard navigation, visible focus, meaningful alt
text, copyable code, and sufficient contrast. Honor reduced-motion preferences.
Dark is the initial presentation; documentation also supports a readable light
mode. Diagrams remain understandable without animation or color alone.

## Homepage narrative

1. Hero: "From GitHub issue to reviewed pull request." Describe durable agent
   workflows with developer control. Primary action: Quick start. Secondary:
   Explore the architecture. Include a repository link and actual UI screenshot.
2. Issue-to-PR flow: intake, planning/sub-issues, clarification, coding, review,
   feedback, human approval/merge, verification. Explain where the developer acts.
3. Stage controls: model/prompt choices, routing, retries/timeouts, attempt logs.
   Link to the stage reference and distinguish these from whole-workflow controls.
4. Worker fleet: simple/developer roles, independent scaling, shared profiles,
   routing, concurrency. State the current shared single-host storage constraint.
5. Model and prompt flexibility: API/Codex/Claude coding, separate planner/reviewer
   adapters, task model overrides, immutable profile revisions, restart semantics.
6. Traceability: run → stage → attempt → worker/model call, with token categories,
   estimated spend, errors, and actual loaded configuration revision. Use a real
   run/stage screenshot; any explanatory accounting example is labeled illustrative.
7. Operational visibility: control-plane views and optional SigNoz/OpenTelemetry
   for traces, metrics, logs, and container resources. Explain how they complement
   each other.
8. Start locally: concise prerequisites and launch steps, linked to complete guides.

No fabricated performance benchmarks, customer testimonials, guaranteed cost
savings, or claims of autonomous merging.

## Documentation map

### Start here

- Introduction and capabilities; supported deployment and limitations.
- Prerequisites and credentials; Hatchet development profile and target repository.
- Quick start using managed workers; alternative Compose setup.
- First issue, discussion labels, draft PR, feedback, and human merge.
- Concepts: workers, workflows, stages, attempts, profiles, factory revisions.

### Workflows and stages

- Issue lifecycle, sub-issues, GitHub webhooks, polling and recovery markers.
- Every registered workflow and task: purpose, inputs, dependencies, role, outputs,
  retries, timeouts, concurrency and failure behavior as implemented.
- Workflow trigger/pause/resume, queued triggers, TTL, idempotency, action receipts.
- Stage-specific model overrides and prompts, including non-model stages.
- Attempt investigation and retry context; distinguish viewing history from
  supported mutation actions. Do not invent a per-stage pause or replay control.

### Control plane

- Overview and navigation; login/session behavior.
- Workers: create/start/stop/restart, profile changes, removal, discovered workers.
- Worker details: builds, startup logs, registration and stage-attempt history.
- Workflows and runs: filtering, stages, attempts, errors, contextual log links.
- Parsed/raw logs and URL-preserved filters.
- Model history and conversation access; capture policy and privacy limits.
- Usage and cost: run/stage/attempt scopes and incomplete-accounting indicators.
- Worker profiles: create, edit, clone, history, optimistic conflicts, pinning.
- Factory settings: shared definitions, coordinated adoption and restart requirements.
- YAML validation/formatting; image builds, startup scripts; Markdown prompts.

### Configuration reference

- Manifest and include order; merge and validation rules; credential separation.
- Complete field inventory for defaults, worker definitions, profiles, task
  templates and task overrides, with types, defaults, constraints and examples.
- Models/providers and adapters; budgets, retries, timeouts and coding limits.
- Routing, concurrency, cron, labels, state and telemetry settings.
- Managed versus standalone configuration; loaded revision and restart behavior.
- Environment-variable reference, identifying credentials, selectors, and control
  service settings separately; values are placeholders, never local secrets.

### Backend API

- Base URL and local deployment; authentication/session cookies, CSRF and origin
  rules, worker-token authentication where applicable.
- Route inventory from `main.py`, `worker_configs.py`, and `workflow_controls.py`,
  including aliases and legacy routes clearly marked for compatibility.
- Group routes by health/authentication, targets/workers/lifecycle/build logs,
  workflows/actions, runs/task logs, factory settings/profiles/revisions,
  events/model conversations/usage, worker bootstrap and ingestion.
- For each route: method/path, authentication, parameters, body shape, response,
  status codes, error behavior, side effects, and applicable idempotency semantics.
- Safe curl examples using placeholders and locally acquired session/CSRF tokens.
- Generate a static route inventory from application metadata where reliable;
  verify manual examples against handlers/tests. Many bodies are dynamic, so
  OpenAPI alone is not sufficient to describe actual validation contracts.
- Never embed an API explorer that sends requests to a visitor's local factory.

### Observability

- SigNoz setup, provisioning, credentials, persistence and lifecycle.
- Enable observed workers; exporter configuration and environment filters.
- Trace/log correlation, stage attempts, failures, duration, heartbeats.
- OpenAI/Codex/Claude accounting differences and token normalization.
- Per-run/per-stage/per-attempt usage and known cost subtotals in the control plane.
- Price file, unknown/unpriced records, CLI estimates, large-context limitations,
  missing telemetry, and reconciliation with provider bills.
- Container resource collector, CPU/memory/network/I/O and disk-capacity semantics.
- Synthetic telemetry validation that makes no paid model calls.
- Privacy boundaries and capture differences between application history and OTLP.

### Operations and architecture

- Startup/shutdown, upgrades, backups, database/encryption-key recovery, host storage.
- Scaling multiple workers, routing capacity and concurrency; single-host limits.
- Troubleshooting organized by observable symptom; FAQ and diagnostic commands.
- Architecture, adapters, extension contracts, data boundaries and security model.
- Local development, tests, documentation contribution and website deployment.
- Separate historical plans from current user guidance; future capabilities must
  never be presented as implemented.

## Content ownership and coverage

Maintain one authoritative copy of each topic. During implementation, create a
coverage checklist mapping source guides, UI screens, configuration schemas, and
API routes to website pages. Expand missing API/field reference from code and tests.

Prefer reusing existing Markdown through a deterministic content preparation step
with explicit heading/link/image transformations. Where restructuring a guide is
necessary, put the maintained topic in one location and update repository entry
links; do not leave two manually maintained versions of the same instructions.
Keep the README as a concise onboarding entry point linking to the site and
repository-readable guides. Exclude internal plans/specs from the public sidebar
and search index.

## Screenshots and diagrams

Reuse the three existing approved screenshots for overview, workers and profiles.
Capture additional real screens for workflow/run stage details, stage-scoped logs
and usage, prompts, model history, and SigNoz where available. Capture is read-only:
do not trigger workflows, restart workers, or make paid model calls for images.

Inspect every screenshot for credentials, repository content and private model
exchanges. Use harmless existing views or controlled synthetic fixtures labeled
as examples if real views are unsuitable. Never fabricate a screenshot that
suggests an unsupported feature. Give screenshots captions, purpose-specific alt
text, consistent widths, and click-to-expand viewing where useful.

Use code-native diagrams for architecture, task flow, and traceability hierarchy.

## GitHub Pages and CI

Expected project URL: `https://phudekar.github.io/flumic-software-factory/`, subject to
repository Pages configuration. Configure the base path for project hosting so
assets, search, navigation, canonical links and 404 handling work under that path.

A dedicated workflow installs locked dependencies and builds/checks the site on
pull requests. Only trusted main-branch pushes or manual runs deploy the resulting
static artifact. Grant Pages deployment permissions only to the deployment job;
use the GitHub Pages environment and deployment concurrency protection.

No runtime credentials are needed in the site build. Exported API metadata must
use isolated test settings and must not start external services or expose local
secrets. Exclude `.env`, state databases, telemetry payloads and conversation spools
from all website inputs. Enabling Pages may require a repository settings change;
report the actual setting or permissions blocker if encountered.

## Verification and acceptance

- Every requested capability has a substantive homepage section and linked guide.
- Coverage checklist accounts for every current UI area, configurable field, API
  route, and observability topic; uncovered items are resolved before handoff.
- Build and framework checks pass with locked dependencies.
- Internal links, anchors, images, API route coverage and base-path URLs validate.
- Search finds representative model, stage, CSRF, profile and telemetry topics.
- Browser review covers landing page and representative documentation/API pages
  at desktop/mobile sizes, keyboard navigation, theme contrast and code overflow.
- Screenshot captions reflect the actual UI; known limitations remain explicit.
- PR builds do not deploy; production deploys use the configured project path.
- No publishing claim without a successful deployment result. If remote deployment
  is unavailable, deliver the verified local build and name the exact blocker.

## Decisions awaiting review

Approve or adjust the framework, visual direction, homepage narrative, documentation
scope and content ownership approach. After approval, write an implementation plan
with an explicit content coverage checklist, then choose execution mode before
site implementation. This spec makes no runtime product changes.

## References

- Hatchet homepage: https://hatchet.run/
- Hatchet documentation: https://docs.hatchet.run/v1
- Astro GitHub Pages deployment: https://docs.astro.build/en/guides/deploy/github/
- Repository guides under `docs/`, control-plane handlers and tests, configuration
  schemas, launch scripts and checked-in observability assets.
