# Hatchet GitHub Issue Worker

This project runs an Alpine-based Python worker that monitors issues in
`phudekar/software-factory`, asks for clarification when needed, proposes code
changes on an isolated branch, and opens a draft pull request. Hatchet handles
the task DAG, three concurrent issue workflows, five-minute scan schedule,
event triggers, and durable waits.

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
`GITHUB_REPOSITORY` should remain `phudekar/software-factory` unless the worker
is intentionally being redirected.

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

Start one worker:

```sh
./scripts/start-workers.sh -d
```

Scale to three worker containers:

```sh
./scripts/start-workers.sh --scale worker=3 -d
```

The script loads the `development` profile using the CLI's checked two-step
environment export, then builds and starts the worker. Each container joins
`worker-network` and has the shared directories mounted read/write at `/shared`
and `/data`.

To check Hatchet connectivity, authenticated GitHub repository access, and
OpenAI model access from an ephemeral container without starting the worker:

```sh
./scripts/check-hatchet-connectivity.sh
```

The check uses the configured Hatchet profile endpoint and reports DNS
resolution, TCP connection, and gRPC channel readiness. It also makes an
authenticated read request for `GITHUB_REPOSITORY` using `GITHUB_TOKEN`. The
token is never printed. Finally, it sends a small, standalone JSON-mode
completion request to the configured `OPENAI_MODEL` using `OPENAI_API_KEY`;
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
   can use up to `MAX_PATCH_ITERATIONS` incremental model rounds (default 10,
   configurable from 1 to 20), each changing at most five files. Earlier
   changes and progress carry forward; the worker publishes only after the
   model reports the requirements complete. Set the variable in `.env` to
   customize it for Docker Compose.
5. A fail-closed scanner checks issue text, selected files, outgoing model
   prompts, model responses, and proposed comments/changes. If configured
   secret patterns are detected, processing stops; logs include finding
   categories only, not the matched text.
6. The model has no GitHub tools, shell, or credentials. The worker validates
   its structured response and allowed paths, then writes a dedicated
   `hatchet/issue-N` branch through the GitHub API and opens a draft PR. It does
   not clone or execute repository code.
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
9. Durable Hatchet tasks wait for approval and merge events, verify the merged
   commit and checks, comment on the issue, and close it. Cron processing then
   advances to the next pending issue.
10. A separate five-minute scan and GitHub comment/review webhooks inspect open
   PRs linked to issues labeled `agent-pr-open`. New PR conversation comments,
   inline review comments, and submitted review bodies are passed as untrusted
   feedback alongside selected files from the PR branch. For actionable code
   requests—including findings in the worker's own post-creation review—the
   worker commits changes to that same branch; it never opens a second PR. A
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

The scan schedule is declared in `worker/worker.py` as `*/5 * * * *` (UTC).
Change `SCAN_CRON` and restart/redeploy workers to change it. List registered
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
OpenAI model. The scanner and allowlist reduce exposure but cannot prove that
content is free of confidential information; do not enable this workflow unless
that data-sharing policy is acceptable. Repository files and issue text are
treated as untrusted instructions. The model request never includes API
credentials, and model output is not executed as a command.

The worker can replace existing files only when they were included in the
approved context. It can also create new UTF-8 text files at safe repository
paths, provided the path does not already exist. After a PR is merged, those
new files are included in later issue inventories automatically if they pass
the same safety and size filters.

The worker does not create or modify secret/credential files, hidden files,
dependency manifests or lockfiles, CI files, deployment/infrastructure files,
or binary assets. Review generated patches and configure GitHub branch
protection and required CI checks before allowing merges.
