# Hello World: a recorded live example

This is the actual run of [parent issue #43](https://github.com/phudekar/software-factory-test/issues/43),
[implementation issue #44](https://github.com/phudekar/software-factory-test/issues/44)
and [draft PR #45](https://github.com/phudekar/software-factory-test/pull/45) in
`phudekar/software-factory-test`. Those links require access to the private
repository. Screenshots below record observed states, not recreated labels.

The initial planning call ran on the existing profile-based worker. The local
installation was then backed up and explicitly migrated to project execution;
implementation resumed under Default using the existing child issue. That
migration was specific to this installation, not a step for every new issue.

## 1. Ready to process — parent issue

The parent initially had `ready-for-agent`. This is a readiness signal, not proof
of execution. The example was explicitly triggered; automatic launch depends on
configured repository triggers.

![Parent issue 43 with ready-for-agent before processing](images/github-hello-world-issue-43.png)

## 2. Plan created — parent issue

The planner produced one implementation task. The parent acquired
`agent-plan-created`; its original readiness label was consumed. This does not
mean the implementation is finished.

![Parent issue 43 after planning, with agent-plan-created](images/github-hello-world-43-plan-created.png)

## 3. Implementation work identified — child issue

The worker created #44 with `agent-subtask` and linked it to #43. Follow this
child for implementation progress; PR labels do not all accumulate on the parent.

![Generated child issue 44 with agent-subtask](images/github-hello-world-44-subtask.png)

## 4. Clarification requested — child issue

The repository already contained `hello_world/`. Triage asked whether to preserve
that directory and added `needs-discussion`. The control run completed its stage
calls, but its business outcome was waiting for discussion, not a generated PR.

![Issue 44 paused with needs-discussion](images/github-hello-world-44-needs-discussion.png)

## 5. Clarification resolved — child issue timeline

The reply confirmed a new root-level `hello.py`, preserving existing files with
no requested CI/documentation changes. Adding `ready-for-agent` resumed the
stored discussion follow-up. The agent then removed the discussion/readiness
labels as it continued. This screenshot shows GitHub's actual timeline event
because readiness was transient, rather than recreating the old state.

![Timeline records readiness and discussion-label removal](images/github-hello-world-44-discussion-resolved.png)

## 6. Draft PR created — child issue

Implementation created draft PR #45 and added `agent-pr-open` to #44. The PR
contains only `hello.py`. Its CI build checks passed. Independently checking
commit `7a49fda15eff5ba5abc8f13757a00be837e15bee` confirmed stdout exactly
`Hello, world!` plus a newline, exit code 0 and empty stderr.

![Child issue 44 with agent-pr-open and its draft PR link](images/github-hello-world-44-pr-open.png)

## 7. Automated review posted — pull request

The automated review added `agent-pr-reviewed` to **PR #45**, not the parent issue.
It posted concerns about import-time execution. This label means a review was
posted; it does not mean the findings were accepted, the PR was approved or the
change merged. The PR remains a draft awaiting human review.

![Draft PR 45 and its agent-pr-reviewed label](images/github-hello-world-pr-45-reviewed.png)

## Inspect the successful model exchange

In **Default project → Model history**, open the planning exchange. The captured
call used `openai / gpt-6-luna`, recorded **802 input / 234 output tokens**, and
an **estimated $0.0001972** for that call. Reported provider cost is unknown.
These numbers are not the cost of the complete workflow.

The user request contained the Hello World issue requirements and repository
context. The actual structured response contained one task titled **Add the
root-level Python Hello World script**, with criteria for the exact greeting,
newline and exit code. The [captured request excerpt and response](examples/walkthrough/hello-world-model-exchange.json)
retain those fields; the excerpt omits system instructions and the source-path
inventory. It is an actual planning response, not an invented example or evidence
that the model itself ran tests.

![Actual captured API response for the Hello World planning call](images/hello-world-model-response-live.png)

## Find the project runs

After the approved runtime migration, Default recorded the deterministic runtime
check, the parent run, the child discussion run and the resumed implementation.
The resumed implementation run is `7d53f1f2-ec3c-448a-80fd-f8e576a9fba7`.

![Real Default project workflow runs](images/default-project-runs-live.png)

![Completed resumed implementation run under Default](images/default-project-hello-world-completed.png)

A completed run means its stage invocations finished. Read operation results,
issue labels and follow-up receipts to distinguish clarification, no-op planning,
draft publication and actual merge verification. This example stops at a reviewed
draft PR; human approval and merging have not been performed.
