# Hatchet GitHub Issue Worker

This project runs an Alpine-based Python worker that monitors issues in
`phudekar/software-factory`, asks for clarification when needed, proposes code
changes on an isolated branch, and opens a draft pull request. Hatchet handles
the task DAG, three concurrent issue workflows, five-minute scan schedule,
event triggers, and durable waits.

## Architecture and migration

The factory now separates application operations from Hatchet, coding agents,
state, and telemetry. See [the migration plan](docs/architecture-migration.md)
and [the implemented architecture and extension guide](docs/architecture.md).
Planning, implementation, review, state, and observation adapters can be selected
independently. Existing Hatchet task names and GitHub recovery markers remain.

## Worker and model dashboards

An optional SigNoz stack visualizes worker activity, issue traces, models, token
usage, and estimated costs. See [the observability guide](docs/observability.md).
Start it with `./scripts/start-observability.sh`, open `http://localhost:3301`,
and the factory dashboard is provisioned automatically. Start factory
workers with telemetry using `./scripts/start-observed-workers.sh -d`.

## Control-plane application

A separate React/TypeScript webapp provides worker management, Hatchet run/stage
views, logs, redacted model history, usage, and versioned config/prompt editing.
Start it with `./scripts/start-control-plane.sh` and open `http://localhost:3400`.
The private `.env.control-plane` file contains the generated administrator password.
Existing workers are discovered read-only and are not restarted by this command.
See the [operator guide](docs/control-plane.md) for capabilities and current
limits, and the [architecture plan](docs/control-plane-architecture.md) for the
full design and remaining milestones.

## Prerequisites

- Docker Engine and the standalone `docker-compose` command
- Hatchet CLI with a profile named `development`
- Fine-grained GitHub token for `phudekar/software-factory`
- OpenAI API key
- A GitHub webhook configured to deliver issue and pull-request events to
  Hatchet (see below)
- An initialized `phudekar/software-factory` repository with a default branch
  and source files

## Configure credentials

Copy `.env.example` to `.env` and set `GITHUB_TOKEN` and `OPENAI_API_KEY`.
[`worker-config.yaml`](worker-config.yaml) includes related files under `config/` for models,
agent selection, limits, task retries/timeouts, per-task additional prompts,
GitHub labels, scan cron, logging, and telemetry. See the
[configuration guide](docs/worker-configuration.md) for examples and precedence.

Set `workers.developer.developer_agent` to `codex`, `claude`, or `api`.
Codex/API coding requires `OPENAI_API_KEY` (or `DEVELOPER_OPENAI_API_KEY`);
Claude Code requires `ANTHROPIC_API_KEY`. The developer image includes both CLIs.
`defaults.codex_model` selects the model used by coding and the Codex healthcheck.
Restart workers after editing YAML; rebuild once to apply this migration.

The GitHub token needs permission to read issues and source, create comments
and labels, create/link sub-issues, create branches, and open draft pull requests.
Configure it only for this repository (`Contents: read/write`,
`Issues: read/write`,
`Pull requests: read/write`, and `Checks`/`Commit statuses: read`). Do not put
the Hatchet profile token in `.env`.

Do not commit or share `.env`. Hatchet credentials stay in the host CLI profile.
The launcher loads the specifically named profile and passes its
`HATCHET_CLIENT_TOKEN` and TLS setting to Compose as container environment
variables; the CLI profile store is not copied into the image.

## Start workers

Start the simple and developer workers:

```sh
./scripts/start-workers.sh -d
```

Scale the lightweight simple-worker role to three containers:

```sh
./scripts/start-workers.sh --scale worker=3 -d
```

The script loads the `development` profile using the CLI's checked two-step
environment export, then builds and starts the services. Hatchet routes intake,
planning, waiting, and review tasks to workers labeled `worker_type=simple`;
code generation and PR-feedback changes require `worker_type=developer`.
Both roles must be running for end-to-end issue processing. The simple workers
join `worker-network`. All workers have shared directories mounted read/write
at `/shared` and `/data`. Factory state and prepared changes are stored in
`/data/factory.sqlite3`; all workers must share this database and its lock directory
on the same host. See the architecture guide before using multiple hosts.

Developer CLI processes run as the unprivileged `agent` user with a reduced
environment and receive the configured provider API key directly. They can run
shell commands in an isolated repository workspace, but can access network
resources and the mounted shared directories. Treat repositories and their
model-generated changes as untrusted, and do not place sensitive data in the
mounted directories.

When `workers.developer.developer_agent: codex`, Compose checks the developer worker by making a
small Codex completion request using the configured `defaults.codex_model` and provider
key every 10 minutes. The same model is used for Codex code-generation tasks. This
verifies that Codex can reach and use the configured OpenAI endpoint, not just
that DNS or TCP connectivity works. The request can incur a small provider
charge. Check `docker-compose ps developer-worker` for the health status and
inspect probe output with:

```sh
docker inspect --format '{{range .State.Health.Log}}{{println .End .ExitCode .Output}}{{end}}' \
  "$(docker-compose ps -q developer-worker)"
```

CLI process completion logs contain the agent name and exit code. Raw JSON
streams, prompts, completions, and tool output are not exported as normal progress
logs. Structured usage is recorded by the optional OpenTelemetry observer.
Use `docker-compose logs developer-worker` for workflow and process metadata.

To check Hatchet connectivity, authenticated GitHub repository access, and
OpenAI model access from an ephemeral container without starting the worker:

```sh
./scripts/check-hatchet-connectivity.sh
```

The check uses the configured Hatchet profile endpoint and reports DNS
resolution, TCP connection, and gRPC channel readiness. It also makes an
authenticated read request for `defaults.repository` using `GITHUB_TOKEN`. The
token is never printed. Finally, it sends a small, standalone JSON-mode
completion request to the configured `defaults.openai_model` using `OPENAI_API_KEY`;
this request is billable. API failures report status/type/code, not the key.
For example, `model_not_found` means the configured model is unavailable to
the API project or the project is not authorized to use it.
The GitHub check confirms repository read access, not write permissions such
as creating issues, branches, or pull requests.

## Worker operating model

### Issue processing

1. A Hatchet cron-triggered task checks for open, unblocked issues every five
   minutes. GitHub issue webhooks can also start work immediately.
2. Issues needing clarification are deferred before planning: the worker adds
   `needs-discussion`, posts questions, and waits durably for a team member to
   add `ready-for-agent`. Adding that label marks discussion resolved; the
   worker uses issue comments as context. The durable waiter polls every 30
   seconds, so resumption does not depend on GitHub webhook delivery.
3. Before implementation, the worker creates an implementation plan from the
   issue requirements, comments, and safe repository path inventory. It posts
   the plan on the parent issue, then creates one labeled `agent-subtask` issue
   per independent task with an objective and acceptance criteria, and links
   each child using GitHub's native sub-issues relationship. Each child
   is eligible for a worker immediately, with up to three issue workflows
   active concurrently. The parent receives
   `agent-plan-created` and is not itself sent through patch generation. A
   persisted plan and per-child markers let retries resume without duplicating
   already-created sub-issues.
4. Each sub-issue worker receives its task requirements and comments, plus
   specific safe text files selected from the repository inventory.
   If no existing file is relevant, it may still propose a new file. The total
   model request, including its reserved response budget, is limited to 32,000
   tokens, with up to 16,000 tokens reserved for the response. Patch generation
   can use up to `defaults.max_patch_iterations` incremental model rounds (default 10,
   configurable from 1 to 20), each changing at most five files. Earlier
   changes and progress carry forward; the worker publishes only after the
   model reports the requirements complete. Set the variable in `.env` to
   customize it for Docker Compose.
5. A fail-closed scanner checks issue text, selected files, outgoing model
   prompts, model responses, and proposed comments/changes. If configured
   secret patterns are detected, processing stops; logs include finding
   categories only, not the matched text.
6. Patch generation runs on a `worker_type=developer` worker. With the `api`
   backend, the model has no GitHub tools, shell, or credentials. With the
   Codex/Claude backends, the CLI works in a temporary repository workspace as
   an unprivileged user; it receives the provider API key but no GitHub or
   Hatchet credentials. The worker validates
   changed paths and content, then writes a dedicated `hatchet/issue-N` branch
   through the GitHub API and opens a draft PR.
7. After creating a draft PR, a separate workflow step compares its diff with
   the issue requirements and comments, the current contents of changed files,
   and up to three related source files selected from the PR branch. It posts
   actionable defects and specific improvement suggestions—or a no-findings
   summary—as a PR comment. It applies `agent-pr-reviewed` to the PR once review
   succeeds, and skips a second post-creation review when that label is present.
   This review reports findings; it does not modify the PR branch.
8. Draft-PR CI runs in GitHub. Configure required status checks and branch
   protection in GitHub; the worker does not run project-controlled tests in
   its credential-bearing container. A team member marks the draft ready for
   review, approves it, and merges it.
9. Durable Hatchet tasks poll current PR state through review and merge, verify the merged
   commit and checks, comment on the issue, and close it. Cron processing then
   advances to the next pending issue.
10. A separate five-minute scan and GitHub comment/review webhooks inspect open
   PRs linked to issues labeled `agent-pr-open`. New PR conversation comments,
   inline review comments, and submitted review bodies are passed as untrusted
   feedback alongside selected files from the PR branch. For actionable code
   requests—including findings in the worker's own post-creation review—the
   developer worker commits changes to that same branch; it never opens a second PR. A
   cursor comment and commit marker prevent already-processed feedback from
   being applied repeatedly.

The Hatchet DAG is `load-issue` → `plan-issue-and-create-sub-issues` →
`triage-issue` →
`generate-and-open-draft-pr` → `review-opened-pull-request` →
`record-workflow-result`. Task outputs contain only issue/PR metadata and
selected file paths; source contents, diffs, and generated file contents are
not returned as Hatchet task outputs. Durable tasks perform the long
human-review waits without relying on a worker process staying alive.

### Configure GitHub webhook delivery

In the Hatchet dashboard, create a GitHub webhook with:

- Event key expression:
  `'github:' + headers['x-github-event'] + ':' + input.action`
- Events: issues (`opened`, `reopened`, `labeled`), issue comments (`created`),
  pull-request review comments (`created`), pull request reviews (`submitted`),
  and pull requests (`closed`)
- A webhook secret, also configured in the GitHub repository's webhook settings

Use the generated Hatchet URL as the GitHub webhook payload URL and set content
type to `application/json`. The expression must produce the event keys used by
the worker, including `github:issues:labeled`,
`github:issue_comment:created`,
`github:pull_request_review_comment:created`,
`github:pull_request_review:submitted`, and `github:pull_request:closed`.

When an issue needs discussion, a team member adds the `ready-for-agent` label
to resume work. The issue remains open while its draft PR is reviewed. The
webhook can queue eligible issues immediately; durable discussion waits also
poll GitHub, so a missed label webhook does not leave them waiting indefinitely.

### Cron schedule and manual event

The scan schedule defaults to `*/5 * * * *` (UTC) in factory settings.
Set `defaults.scan_cron` in `config/workflow.yaml` and restart/redeploy workers to change it. List registered
cron jobs for the named profile with:

```sh
hatchet cron list --profile development --output json
```

Upcoming executions are visible in the Hatchet dashboard under **Triggers →
Cron Jobs**.

To trigger one sample issue event:

```sh
./scripts/push-sample-event.sh 123
```

The helper pushes the `issue-agent:issue` event; it uses the same
`development` profile already loaded into the running worker container.
Replace `123` with an existing issue number.

## Check status, logs, and runs

```sh
docker-compose ps
docker-compose logs -f worker
hatchet worker list --profile development --output json
hatchet runs list --profile development --output json --since 1h
```

Worker INFO logs record workflow progress with the repository, issue and PR
numbers, safe titles, and stage/status details. Issue bodies, comments, and
source contents are not logged; titles matching configured sensitive-content
patterns are redacted. If a model response contains no text, a warning records
only response metadata (model/response ID, finish reason, refusal presence,
and token counts) to help diagnose the result; request and response content is
not logged.

## Stop workers

```sh
docker-compose down
```

This removes worker containers and the Compose network, but not the host
directories `shared/` or `data/`.

## Security boundary and limitations

The issue and selected source context are intentionally sent to the configured
provider. The scanner and path checks reduce exposure but cannot prove that
content is free of confidential information; do not enable this workflow unless
that data-sharing policy is acceptable. Repository files and issue text are
treated as untrusted instructions. The simple worker's direct API requests
contain no GitHub or Hatchet credentials. Codex/Claude CLI agents can run
commands against the repository as UID 10001 inside the developer container.
The CLI subprocess receives only its selected provider API key, not the
worker's GitHub or Hatchet credentials. Since the developer container has
network access, repository code and CLI subprocesses can use that provider key;
use a dedicated, restricted provider key if that risk is not acceptable.

Codex/Claude CLI workers receive a full repository archive as their workspace.
They may create or modify any UTF-8 text file in it, including hidden files,
dependency lockfiles, CI, deployment, and infrastructure files. They cannot
modify `.git` metadata or delete files; binary changes are not supported. The
API-backed worker continues to use the narrower source-path allowlist. All
changed content is still checked for known secret patterns and size limits, but
this does not guarantee that content is safe. Review high-impact changes
carefully and configure GitHub branch protection and required CI checks before
allowing merges.
