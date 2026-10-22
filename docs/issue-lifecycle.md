# Issue lifecycle and GitHub integration

[Back to the README](../README.md)

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
