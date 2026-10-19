from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

from hatchet_sdk import (
    Context,
    DesiredWorkerLabel,
    DurableContext,
    EmptyModel,
    Hatchet,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from github_client import GitHubClient, GitHubError
from guardrails import (
    assert_safe_text,
    find_sensitive_categories,
    is_allowed_source_path,
    is_safe_repository_path,
)
from cli_agent import generate_patch as generate_cli_patch
from model_client import (
    MAX_PATCH_FILES,
    IssueWorkPlan,
    ModelClient,
    ModelError,
    PatchPlan,
    PullRequestReview,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("issue-worker")

REPOSITORY = os.getenv("GITHUB_REPOSITORY", "phudekar/software-factory")
SCAN_CRON = "*/5 * * * *"
ISSUE_EVENT = "issue-agent:issue"
PR_REVIEW_EVENT = "github:pull_request_review:submitted"
PR_CLOSED_EVENT = "github:pull_request:closed"
PR_FEEDBACK_EVENTS = [
    "github:issue_comment:created",
    "github:pull_request_review_comment:created",
    PR_REVIEW_EVENT,
]
DISCUSSION_POLL_INTERVAL = timedelta(seconds=30)
WORKER_TYPE = os.getenv("WORKER_TYPE", "simple")
if WORKER_TYPE not in {"simple", "developer"}:
    raise ValueError("WORKER_TYPE must be 'simple' or 'developer'")
DEVELOPER_AGENT = os.getenv("DEVELOPER_AGENT", "api")
if DEVELOPER_AGENT not in {"api", "codex", "claude"}:
    raise ValueError("DEVELOPER_AGENT must be 'api', 'codex', or 'claude'")
SIMPLE_WORKER_LABEL = DesiredWorkerLabel(
    key="worker_type", value="simple", required=True
)
DEVELOPER_WORKER_LABEL = DesiredWorkerLabel(
    key="worker_type", value="developer", required=True
)

NEEDS_DISCUSSION = "needs-discussion"
READY_FOR_AGENT = "ready-for-agent"
PR_OPEN = "agent-pr-open"
PR_REVIEWED = "agent-pr-reviewed"
PLAN_CREATED = "agent-plan-created"
PLAN_SUBTASK = "agent-subtask"
PLAN_MARKER_PREFIX = "<!-- hatchet-issue-work-plan:"

hatchet = Hatchet()


class IssueInput(BaseModel):
    repository: str = REPOSITORY
    issue_number: int = Field(gt=0)


class IssueState(BaseModel):
    issue_number: int
    state: str
    labels: list[str]


class TriageState(BaseModel):
    status: str
    issue_number: int
    source_paths: list[str] = Field(default_factory=list)


class IssuePlanState(BaseModel):
    status: str
    issue_number: int
    sub_issue_numbers: list[int] = Field(default_factory=list)


class PublishState(BaseModel):
    status: str
    issue_number: int
    pull_number: int | None = None
    pull_url: str | None = None
    branch: str | None = None


class GitHubIssue(BaseModel):
    number: int
    title: str
    body: str | None = None
    state: str
    labels: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    pull_request: dict[str, Any] | None = None


class GitHubRepository(BaseModel):
    full_name: str


class GitHubWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    repository: GitHubRepository
    issue: GitHubIssue | None = None
    pull_request: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    label: dict[str, Any] | None = None


class PullInput(BaseModel):
    repository: str
    issue_number: int
    pull_number: int
    pull_url: str
    branch: str


class EventResult(BaseModel):
    status: str
    issue_number: int | None = None
    pull_number: int | None = None


class ScanResult(BaseModel):
    status: str
    issue_number: int | None = None


class FeedbackScanResult(BaseModel):
    status: str
    pull_requests_queued: int = 0


def labels_for(issue: dict[str, Any]) -> set[str]:
    return {
        str(label.get("name", ""))
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }


def validate_repository(repository: str) -> None:
    if repository != REPOSITORY:
        raise ValueError("Event repository does not match configured repository")


def _issue_snapshot(issue: dict[str, Any]) -> IssueState:
    return IssueState(
        issue_number=int(issue["number"]),
        state=str(issue["state"]),
        labels=sorted(labels_for(issue)),
    )


issue_workflow = hatchet.workflow(
    name="software-factory-issue-agent",
    input_validator=IssueInput,
    concurrency=3,
)


@issue_workflow.task(
    name="load-issue",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    execution_timeout=timedelta(minutes=2),
    retries=2,
    backoff_factor=2.0,
    backoff_max_seconds=30,
)
def load_issue(input: IssueInput, ctx: Context) -> IssueState:
    validate_repository(input.repository)
    issue = GitHubClient(input.repository).get_issue(input.issue_number)
    title = str(issue.get("title") or "")
    logger.info(
        "Loaded issue: repository=%s issue=%d state=%s title=%s",
        input.repository,
        input.issue_number,
        issue.get("state"),
        _log_title(title),
    )
    return _issue_snapshot(issue)


@issue_workflow.task(
    name="plan-issue-and-create-sub-issues",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    parents=[load_issue],
    execution_timeout=timedelta(minutes=8),
    retries=2,
    backoff_factor=2.0,
    backoff_max_seconds=30,
    concurrency=1,
)
def plan_issue(input: IssueInput, ctx: Context) -> IssuePlanState:
    state = ctx.task_output(load_issue)
    if state.state != "open":
        return IssuePlanState(status="issue-not-open", issue_number=input.issue_number)

    github = GitHubClient(input.repository)
    issue = github.get_issue(input.issue_number)
    labels = labels_for(issue)
    if PR_OPEN in labels:
        return IssuePlanState(
            status="issue-already-in-progress", issue_number=input.issue_number
        )
    if PLAN_SUBTASK in labels:
        return IssuePlanState(status="subtask", issue_number=input.issue_number)
    if PLAN_CREATED in labels:
        return IssuePlanState(status="already-planned", issue_number=input.issue_number)
    if NEEDS_DISCUSSION in labels and READY_FOR_AGENT not in labels:
        logger.info(
            "Deferring planning until issue discussion is resolved: "
            "repository=%s issue=%d",
            input.repository,
            input.issue_number,
        )
        _start_discussion_wait(input)
        return IssuePlanState(
            status="waiting-for-discussion", issue_number=input.issue_number
        )

    comments = github.get_issue_comments(input.issue_number)
    source_paths = github.list_source_paths()
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    assert_safe_text(title, body, *comments, "\n".join(source_paths))

    discussion_resolved = READY_FOR_AGENT in labels
    triage = ModelClient().triage(
        title=title,
        body=body,
        comments=comments,
        source_paths=source_paths,
        discussion_resolved=discussion_resolved,
    )
    logger.info(
        "Planning pre-check completed: repository=%s issue=%d "
        "needs_discussion=%s selected_paths=%d",
        input.repository,
        input.issue_number,
        triage.needs_discussion,
        len(triage.source_paths),
    )
    if triage.needs_discussion:
        _start_issue_discussion(
            github,
            input,
            title,
            triage.questions,
        )
        return IssuePlanState(
            status="waiting-for-discussion",
            issue_number=input.issue_number,
        )
    if discussion_resolved:
        github.remove_label(input.issue_number, NEEDS_DISCUSSION)
        github.remove_label(input.issue_number, READY_FOR_AGENT)

    plan = _stored_issue_plan(comments)
    if plan is None:
        plan = ModelClient().plan_issue(
            title=title,
            body=body,
            comments=comments,
            source_paths=source_paths,
        )
        plan_comment = _format_issue_plan_comment(plan)
        github.comment_once(
            input.issue_number,
            marker=f"{PLAN_MARKER_PREFIX}",
            comment=plan_comment,
        )

    markers = {
        _sub_issue_marker(input.issue_number, index)
        for index in range(1, len(plan.tasks) + 1)
    }
    existing = github.find_issues_by_markers(markers)
    linked_issue_ids = github.list_sub_issue_ids(input.issue_number)
    github.ensure_label(PLAN_SUBTASK)
    created_numbers: list[int] = []
    parent_url = str(issue.get("html_url") or "")
    for index, task in enumerate(plan.tasks, start=1):
        marker = _sub_issue_marker(input.issue_number, index)
        existing_issue = existing.get(marker)
        if existing_issue is None:
            child_body = _format_sub_issue_body(
                parent_number=input.issue_number,
                parent_url=parent_url,
                objective=task.objective,
                acceptance_criteria=task.acceptance_criteria,
                marker=marker,
            )
            created = github.create_issue(
                title=task.title,
                body=child_body,
                labels=[PLAN_SUBTASK],
            )
            sub_issue_number = int(created["number"])
            sub_issue_id = int(created["id"])
            logger.info(
                "Created planned sub-issue: repository=%s parent_issue=%d "
                "sub_issue=%d step=%d title=%s",
                input.repository,
                input.issue_number,
                sub_issue_number,
                index,
                _log_title(task.title),
            )
        else:
            sub_issue_number = existing_issue["number"]
            sub_issue_id = existing_issue["id"]
            logger.info(
                "Found existing planned sub-issue: repository=%s parent_issue=%d "
                "sub_issue=%d step=%d",
                input.repository,
                input.issue_number,
                sub_issue_number,
                index,
            )
        if sub_issue_id not in linked_issue_ids:
            github.add_sub_issue(input.issue_number, sub_issue_id)
            linked_issue_ids.add(sub_issue_id)
            logger.info(
                "Linked GitHub sub-issue: repository=%s parent_issue=%d "
                "sub_issue=%d step=%d",
                input.repository,
                input.issue_number,
                sub_issue_number,
                index,
            )
        created_numbers.append(sub_issue_number)

    github.ensure_label(PLAN_CREATED)
    github.add_label(input.issue_number, PLAN_CREATED)
    logger.info(
        "Issue plan completed: repository=%s issue=%d sub_issues=%s",
        input.repository,
        input.issue_number,
        created_numbers,
    )
    return IssuePlanState(
        status="sub-issues-created",
        issue_number=input.issue_number,
        sub_issue_numbers=created_numbers,
    )


@issue_workflow.task(
    name="triage-issue",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    parents=[load_issue, plan_issue],
    execution_timeout=timedelta(minutes=3),
    retries=2,
    backoff_factor=2.0,
    backoff_max_seconds=30,
)
def triage_issue(input: IssueInput, ctx: Context) -> TriageState:
    state = ctx.task_output(load_issue)
    plan_state = ctx.task_output(plan_issue)
    if plan_state.status in {"sub-issues-created", "already-planned"}:
        return TriageState(status=plan_state.status, issue_number=input.issue_number)
    if plan_state.status == "waiting-for-discussion":
        return TriageState(
            status="waiting-for-discussion", issue_number=input.issue_number
        )
    if plan_state.status in {"issue-not-open", "issue-already-in-progress"}:
        return TriageState(status="not-pending", issue_number=input.issue_number)
    if state.state != "open" or PR_OPEN in state.labels:
        return TriageState(status="not-pending", issue_number=input.issue_number)

    github = GitHubClient(input.repository)
    issue = github.get_issue(input.issue_number)
    labels = labels_for(issue)
    discussion_resolved = READY_FOR_AGENT in labels

    if NEEDS_DISCUSSION in labels and not discussion_resolved:
        logger.info(
            "Issue requires discussion: repository=%s issue=%d title=%s",
            input.repository,
            input.issue_number,
            _log_title(str(issue.get("title") or "")),
        )
        github.ensure_label(READY_FOR_AGENT)
        github.ensure_label(NEEDS_DISCUSSION)
        _start_discussion_wait(input)
        return TriageState(
            status="waiting-for-discussion", issue_number=input.issue_number
        )

    title = str(issue["title"])
    body = str(issue.get("body") or "")
    comments = github.get_issue_comments(input.issue_number)
    inventory = github.list_source_paths()
    assert_safe_text(title, body, *comments, "\n".join(inventory))
    logger.info(
        "Processing issue: repository=%s issue=%d title=%s",
        input.repository,
        input.issue_number,
        _log_title(title),
    )
    plan = ModelClient().triage(
        title=title,
        body=body,
        comments=comments,
        source_paths=inventory,
        discussion_resolved=discussion_resolved,
    )
    logger.info(
        "Issue triage completed: repository=%s issue=%d comments=%d discussion_resolved=%s "
        "needs_discussion=%s selected_paths=%d",
        input.repository,
        input.issue_number,
        len(comments),
        discussion_resolved,
        plan.needs_discussion,
        len(plan.source_paths),
    )

    if discussion_resolved:
        github.remove_label(input.issue_number, NEEDS_DISCUSSION)
        github.remove_label(input.issue_number, READY_FOR_AGENT)

    if plan.needs_discussion:
        _start_issue_discussion(github, input, title, plan.questions)
        return TriageState(
            status="waiting-for-discussion", issue_number=input.issue_number
        )

    logger.info(
        "Issue ready for patch generation: repository=%s issue=%d selected_paths=%d",
        input.repository,
        input.issue_number,
        len(plan.source_paths),
    )
    return TriageState(
        status="ready",
        issue_number=input.issue_number,
        source_paths=plan.source_paths,
    )


@issue_workflow.task(
    name="generate-and-open-draft-pr",
    desired_worker_labels=[DEVELOPER_WORKER_LABEL],
    parents=[triage_issue],
    execution_timeout=timedelta(minutes=60),
    retries=1,
    backoff_factor=2.0,
    backoff_max_seconds=30,
)
def generate_and_open_draft_pr(input: IssueInput, ctx: Context) -> PublishState:
    triage = ctx.task_output(triage_issue)
    if triage.status != "ready":
        return PublishState(status=triage.status, issue_number=input.issue_number)

    github = GitHubClient(input.repository)
    existing = github.find_issue_pull_request(input.issue_number)
    if existing is not None:
        logger.info(
            "Found existing pull request for issue: repository=%s issue=%d "
            "pull_request=%d title=%s url=%s",
            input.repository,
            input.issue_number,
            int(existing["number"]),
            _log_title(str(existing.get("title") or "")),
            existing.get("html_url"),
        )
        pull_input = PullInput(
            repository=input.repository,
            issue_number=input.issue_number,
            pull_number=int(existing["number"]),
            pull_url=str(existing["html_url"]),
            branch=str(existing["head"]["ref"]),
        )
        _register_pull_wait(github, pull_input)
        return PublishState(
            status="draft-pr-exists",
            issue_number=input.issue_number,
            pull_number=pull_input.pull_number,
            pull_url=pull_input.pull_url,
            branch=pull_input.branch,
        )

    issue = github.get_issue(input.issue_number)
    title = str(issue["title"])
    body = str(issue.get("body") or "")
    comments = github.get_issue_comments(input.issue_number)
    issue_paths = triage.source_paths
    files = github.get_source_files(issue_paths) if DEVELOPER_AGENT == "api" else {}
    logger.info(
        "Generating pull-request patch: repository=%s issue=%d title=%s "
        "selected_paths=%d",
        input.repository,
        input.issue_number,
        _log_title(title),
        len(files),
    )
    plan = _generate_code_patch(
        github=github,
        title=title,
        body=body,
        comments=comments,
        files=files,
        existing_paths=github.list_repository_paths(),
    )
    _validate_patch(plan, allow_all_repository_paths=DEVELOPER_AGENT != "api")
    if not plan.changes:
        logger.info(
            "No safe changes generated: repository=%s issue=%d title=%s",
            input.repository,
            input.issue_number,
            _log_title(title),
        )
        github.comment_once(
            input.issue_number,
            marker="<!-- hatchet-issue-agent-no-change -->",
            comment="The automated agent found no safe source changes to propose.",
        )
        return PublishState(status="no-changes", issue_number=input.issue_number)

    branch = f"hatchet/issue-{input.issue_number}"
    logger.info(
        "Publishing draft pull request: repository=%s issue=%d branch=%s "
        "changed_files=%d",
        input.repository,
        input.issue_number,
        branch,
        len(plan.changes),
    )
    pull = github.create_draft_pull_request(
        issue_number=input.issue_number,
        title=plan.pull_title,
        body=plan.pull_body,
        branch=branch,
        changes=plan.changes,
    )
    logger.info(
        "Created draft pull request: repository=%s issue=%d issue_title=%s "
        "pull_request=%d pull_title=%s url=%s",
        input.repository,
        input.issue_number,
        _log_title(title),
        int(pull["number"]),
        _log_title(str(pull.get("title") or "")),
        pull.get("html_url"),
    )
    pull_input = PullInput(
        repository=input.repository,
        issue_number=input.issue_number,
        pull_number=int(pull["number"]),
        pull_url=str(pull["html_url"]),
        branch=branch,
    )
    _register_pull_wait(github, pull_input)
    return PublishState(
        status="draft-pr-created",
        issue_number=input.issue_number,
        pull_number=pull_input.pull_number,
        pull_url=pull_input.pull_url,
        branch=branch,
    )


@issue_workflow.task(
    name="review-opened-pull-request",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    parents=[generate_and_open_draft_pr],
    execution_timeout=timedelta(minutes=5),
    retries=2,
    backoff_factor=2.0,
    backoff_max_seconds=30,
)
def review_opened_pull_request(input: IssueInput, ctx: Context) -> EventResult:
    result = ctx.task_output(generate_and_open_draft_pr)
    if result.status != "draft-pr-created" or result.pull_number is None:
        return EventResult(
            status=f"review-skipped-{result.status}",
            issue_number=input.issue_number,
            pull_number=result.pull_number,
        )

    github = GitHubClient(input.repository)
    pull = github.get_pull_request(result.pull_number)
    if PR_REVIEWED in labels_for(pull):
        logger.info(
            "Pull request already has review label: repository=%s issue=%d pull_request=%d",
            input.repository,
            input.issue_number,
            result.pull_number,
        )
        return EventResult(
            status="review-already-labeled",
            issue_number=input.issue_number,
            pull_number=result.pull_number,
        )

    marker = f"<!-- hatchet-issue-agent-review-{result.pull_number} -->"
    if any(marker in body for body in github.get_issue_comments(result.pull_number)):
        _mark_pull_reviewed(github, result.pull_number)
        logger.info(
            "Pull-request review already posted and labeled: repository=%s "
            "issue=%d pull_request=%d label=%s",
            input.repository,
            input.issue_number,
            result.pull_number,
            PR_REVIEWED,
        )
        return EventResult(
            status="review-already-posted",
            issue_number=input.issue_number,
            pull_number=result.pull_number,
        )

    issue = github.get_issue(input.issue_number)
    issue_title = str(issue.get("title") or "")
    issue_body = str(issue.get("body") or "")
    issue_comments = github.get_issue_comments(input.issue_number)
    files = github.get_pull_request_files(result.pull_number)
    branch = str((pull.get("head") or {}).get("ref") or "")
    if not branch:
        raise GitHubError("Pull request has no head branch")
    branch_paths = github.list_source_paths(branch)
    assert_safe_text(
        issue_title,
        issue_body,
        *issue_comments,
        str(pull.get("title") or ""),
        str(pull.get("body") or ""),
        "\n".join(branch_paths),
        *(f"{file['path']}\n{file['patch']}" for file in files),
    )
    context_plan = ModelClient().triage(
        title=issue_title,
        body=issue_body,
        comments=issue_comments,
        source_paths=branch_paths,
        discussion_resolved=True,
    )
    changed_paths = {file["path"] for file in files}
    changed_source_paths = [
        file["path"] for file in files if file["path"] in branch_paths
    ]
    related_paths = [
        path
        for path in context_plan.source_paths
        if path not in changed_paths and path in branch_paths
    ][:3]
    current_files = github.get_source_files(
        changed_source_paths + related_paths,
        branch=branch,
    )
    review = ModelClient().review_pull_request(
        issue_title=issue_title,
        issue_body=issue_body,
        issue_comments=issue_comments,
        pull_title=str(pull.get("title") or ""),
        pull_body=str(pull.get("body") or ""),
        diff=files,
        current_files=current_files,
    )
    comment = _format_pull_request_review(review)
    github.comment_once(
        result.pull_number,
        marker=marker,
        comment=comment,
    )
    _mark_pull_reviewed(github, result.pull_number)
    logger.info(
        "Post-creation pull-request review completed and labeled: repository=%s "
        "issue=%d pull_request=%d findings=%d suggestions=%d label=%s",
        input.repository,
        input.issue_number,
        result.pull_number,
        len(review.findings),
        sum(finding.kind == "suggestion" for finding in review.findings),
        PR_REVIEWED,
    )
    return EventResult(
        status="review-posted",
        issue_number=input.issue_number,
        pull_number=result.pull_number,
    )


@issue_workflow.task(
    name="record-workflow-result",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    parents=[review_opened_pull_request],
    execution_timeout=timedelta(seconds=30),
)
def record_workflow_result(input: IssueInput, ctx: Context) -> EventResult:
    result = ctx.task_output(review_opened_pull_request)
    logger.info(
        "Issue workflow finished: repository=%s issue=%d status=%s pr=%s",
        input.repository,
        input.issue_number,
        result.status,
        result.pull_number,
    )
    return EventResult(
        status=result.status,
        issue_number=input.issue_number,
        pull_number=result.pull_number,
    )


@hatchet.durable_task(
    name="wait-for-ready-for-agent",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=IssueInput,
    execution_timeout=timedelta(days=30),
)
async def wait_for_ready_for_agent(
    input: IssueInput, ctx: DurableContext
) -> dict[str, str]:
    github = GitHubClient(input.repository)
    logger.info(
        "Waiting for issue resolution label: repository=%s issue=%d label=%s",
        input.repository,
        input.issue_number,
        READY_FOR_AGENT,
    )
    while True:
        issue = github.get_issue(input.issue_number)
        labels = labels_for(issue)
        if issue.get("state") != "open":
            logger.info(
                "Discussion wait ended because issue %d is not open",
                input.issue_number,
            )
            return {"status": "issue-not-open"}
        if READY_FOR_AGENT in labels:
            logger.info(
                "Issue discussion resolved; resuming workflow: repository=%s issue=%d",
                input.repository,
                input.issue_number,
            )
            break
        await ctx.aio_sleep_for(
            DISCUSSION_POLL_INTERVAL,
            label=f"waiting for {READY_FOR_AGENT} on issue {input.issue_number}",
        )

    issue_workflow.run(input, wait_for_result=False)
    return {"status": "resumed"}


@hatchet.durable_task(
    name="wait-for-pr-review-and-merge",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=PullInput,
    execution_timeout=timedelta(days=30),
)
async def wait_for_pull_review(input: PullInput, ctx: DurableContext) -> dict[str, str]:
    logger.info(
        "Waiting for pull-request approval: repository=%s issue=%d "
        "pull_request=%d url=%s",
        input.repository,
        input.issue_number,
        input.pull_number,
        input.pull_url,
    )
    await ctx.aio_wait_for_event(
        PR_REVIEW_EVENT,
        expression=(
            f"input.pull_request.number == {input.pull_number} "
            "&& input.review.state == 'approved'"
        ),
    )
    logger.info(
        "Pull request approved; waiting for merge: repository=%s issue=%d "
        "pull_request=%d",
        input.repository,
        input.issue_number,
        input.pull_number,
    )
    await ctx.aio_wait_for_event(
        PR_CLOSED_EVENT,
        expression=(
            f"input.pull_request.number == {input.pull_number} "
            "&& input.pull_request.merged == true"
        ),
    )
    logger.info(
        "Pull request merged; starting verification: repository=%s issue=%d "
        "pull_request=%d",
        input.repository,
        input.issue_number,
        input.pull_number,
    )
    verify_merged_pr.run(input, wait_for_result=False)
    return {"status": "approved-and-merged"}


@hatchet.task(
    name="verify-merged-pr-and-close-issue",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=PullInput,
    execution_timeout=timedelta(minutes=2),
    retries=3,
    backoff_factor=2.0,
    backoff_max_seconds=60,
)
def verify_merged_pr(input: PullInput, ctx: Context) -> EventResult:
    github = GitHubClient(input.repository)
    pull = github.get_pull_request(input.pull_number)
    logger.info(
        "Verifying merged pull request: repository=%s issue=%d "
        "pull_request=%d title=%s url=%s",
        input.repository,
        input.issue_number,
        input.pull_number,
        _log_title(str(pull.get("title") or "")),
        pull.get("html_url"),
    )
    if pull.get("state") != "closed" or not pull.get("merged"):
        raise GitHubError("Pull request is not merged")
    merge_sha = pull.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
        raise GitHubError("Merged pull request has no valid merge commit")
    github.verify_commit_checks(merge_sha)
    github.comment_once(
        input.issue_number,
        marker="<!-- hatchet-issue-agent-merged -->",
        comment=f"Verified the merged change: {input.pull_url}",
    )
    github.close_issue(input.issue_number)
    github.remove_label(input.issue_number, PR_OPEN)
    logger.info(
        "Pull request verified and issue closed: repository=%s issue=%d "
        "pull_request=%d merge_sha=%s",
        input.repository,
        input.issue_number,
        input.pull_number,
        merge_sha,
    )
    return EventResult(
        status="verified-merged",
        issue_number=input.issue_number,
        pull_number=input.pull_number,
    )


@hatchet.task(
    name="handle-github-issue-event",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=GitHubWebhook,
    on_events=[
        "github:issues:opened",
        "github:issues:reopened",
        "github:issues:labeled",
    ],
    execution_timeout=timedelta(minutes=1),
    retries=2,
)
def handle_github_issue_event(input: GitHubWebhook, ctx: Context) -> EventResult:
    repository = input.repository.full_name
    validate_repository(repository)
    if input.issue is None:
        return EventResult(status="ignored")
    if input.action == "labeled" and (
        not input.label or input.label.get("name") != READY_FOR_AGENT
    ):
        return EventResult(status="ignored", issue_number=input.issue.number)
    if input.action not in {"opened", "reopened", "labeled"}:
        return EventResult(status="ignored", issue_number=input.issue.number)
    if (
        input.action == "labeled"
        and input.label
        and input.label.get("name") == READY_FOR_AGENT
        and NEEDS_DISCUSSION in labels_for({"labels": input.issue.labels})
    ):
        return EventResult(
            status="durable-discussion-waiter-will-resume",
            issue_number=input.issue.number,
        )
    logger.info(
        "Received issue event: repository=%s action=%s issue=%d title=%s",
        repository,
        input.action,
        input.issue.number,
        _log_title(input.issue.title),
    )
    issue_workflow.run(
        IssueInput(repository=repository, issue_number=input.issue.number),
        wait_for_result=False,
    )
    return EventResult(status="queued", issue_number=input.issue.number)


@hatchet.task(
    name="trigger-issue-from-sample-event",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=IssueInput,
    on_events=[ISSUE_EVENT],
    execution_timeout=timedelta(minutes=1),
    retries=2,
)
def trigger_issue_from_sample_event(input: IssueInput, ctx: Context) -> EventResult:
    validate_repository(input.repository)
    logger.info(
        "Received sample issue event: repository=%s issue=%d",
        input.repository,
        input.issue_number,
    )
    issue_workflow.run(input, wait_for_result=False)
    return EventResult(status="queued", issue_number=input.issue_number)


@hatchet.task(
    name="scan-pending-github-issues",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=EmptyModel,
    on_crons=[SCAN_CRON],
    execution_timeout=timedelta(minutes=4),
    retries=2,
    concurrency=1,
)
def scan_pending_issues(input: EmptyModel, ctx: Context) -> ScanResult:
    github = GitHubClient(REPOSITORY)
    for issue in github.list_open_issues():
        labels = labels_for(issue)
        if PR_OPEN in labels or NEEDS_DISCUSSION in labels or PLAN_CREATED in labels:
            continue
        number = int(issue["number"])
        title = str(issue.get("title") or "")
        logger.info(
            "Queueing pending issue: repository=%s issue=%d title=%s",
            REPOSITORY,
            number,
            _log_title(title),
        )
        issue_workflow.run(
            IssueInput(repository=REPOSITORY, issue_number=number),
            wait_for_result=False,
        )
        return ScanResult(status="queued", issue_number=number)
    return ScanResult(status="no-pending-issues")


@hatchet.task(
    name="scan-open-pr-feedback",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=EmptyModel,
    on_crons=[SCAN_CRON],
    execution_timeout=timedelta(minutes=1),
    retries=2,
    concurrency=1,
)
def scan_open_pr_feedback(input: EmptyModel, ctx: Context) -> FeedbackScanResult:
    github = GitHubClient(REPOSITORY)
    queued = 0
    for pull in github.list_open_pull_requests():
        pull_input = _pull_input_from_api(github, pull)
        if pull_input is None:
            continue
        logger.info(
            "Queueing pull-request feedback check: repository=%s issue=%d "
            "pull_request=%d title=%s url=%s",
            REPOSITORY,
            pull_input.issue_number,
            pull_input.pull_number,
            _log_title(str(pull.get("title") or "")),
            pull.get("html_url"),
        )
        process_pull_request_feedback.run(pull_input, wait_for_result=False)
        queued += 1
    return FeedbackScanResult(status="queued", pull_requests_queued=queued)


@hatchet.task(
    name="handle-github-pull-request-feedback-event",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=GitHubWebhook,
    on_events=PR_FEEDBACK_EVENTS,
    execution_timeout=timedelta(minutes=1),
    retries=2,
    concurrency=1,
)
def handle_pull_request_feedback_event(
    input: GitHubWebhook, ctx: Context
) -> EventResult:
    validate_repository(input.repository.full_name)
    if input.action not in {"created", "submitted"}:
        return EventResult(status="ignored")
    pull_data = input.pull_request
    if pull_data is None and input.issue is not None:
        pull_data = input.issue.pull_request
    if not pull_data:
        return EventResult(status="ignored")
    github = GitHubClient(input.repository.full_name)
    pull_number = pull_data.get("number")
    if not isinstance(pull_number, int) and input.issue is not None:
        pull_number = input.issue.number
    if not isinstance(pull_number, int):
        return EventResult(status="ignored")
    pull_input = _pull_input_from_api(github, github.get_pull_request(pull_number))
    if pull_input is None:
        return EventResult(status="ignored")
    process_pull_request_feedback.run(pull_input, wait_for_result=False)
    return EventResult(
        status="queued",
        issue_number=pull_input.issue_number,
        pull_number=pull_input.pull_number,
    )


@hatchet.task(
    name="process-open-pull-request-feedback",
    desired_worker_labels=[DEVELOPER_WORKER_LABEL],
    input_validator=PullInput,
    execution_timeout=timedelta(minutes=60),
    retries=2,
    backoff_factor=2.0,
    backoff_max_seconds=30,
    concurrency=1,
)
def process_pull_request_feedback(input: PullInput, ctx: Context) -> EventResult:
    validate_repository(input.repository)
    github = GitHubClient(input.repository)
    pull = github.get_pull_request(input.pull_number)
    if pull.get("state") != "open":
        return EventResult(
            status="pull-request-not-open", pull_number=input.pull_number
        )
    actual_input = _pull_input_from_api(github, pull)
    if actual_input is None or actual_input.issue_number != input.issue_number:
        return EventResult(
            status="pull-request-not-in-progress", pull_number=input.pull_number
        )

    issue = github.get_issue(actual_input.issue_number)
    issue_title = str(issue.get("title") or "")
    pull_title = str(pull.get("title") or "")
    pull_url = str(pull.get("html_url") or actual_input.pull_url)
    logger.info(
        "Checking pull-request feedback: repository=%s issue=%d issue_title=%s "
        "pull_request=%d pull_title=%s url=%s",
        input.repository,
        actual_input.issue_number,
        _log_title(issue_title),
        input.pull_number,
        _log_title(pull_title),
        pull_url,
    )
    feedback = github.get_pull_feedback(input.pull_number)
    cursor = github.pull_feedback_cursor(input.pull_number, actual_input.branch)
    pending = _pending_pull_feedback(feedback, cursor, input.pull_number)
    if not pending:
        logger.info(
            "No new pull-request feedback: repository=%s issue=%d pull_request=%d",
            input.repository,
            input.issue_number,
            input.pull_number,
        )
        return EventResult(status="no-new-feedback", pull_number=input.pull_number)

    if issue.get("state") != "open" or PR_OPEN not in labels_for(issue):
        return EventResult(
            status="issue-not-in-progress", pull_number=input.pull_number
        )

    feedback_text = [_format_review_feedback(item) for item in pending]
    logger.info(
        "Processing pull-request feedback: repository=%s issue=%d "
        "pull_request=%d feedback_items=%d",
        input.repository,
        actual_input.issue_number,
        input.pull_number,
        len(pending),
    )
    branch_paths = github.list_source_paths(actual_input.branch)
    assert_safe_text(
        str(issue.get("title") or ""),
        str(issue.get("body") or ""),
        *feedback_text,
        "\n".join(branch_paths),
    )
    existing_paths = github.list_repository_paths(actual_input.branch)
    if DEVELOPER_AGENT == "api":
        triage = ModelClient().triage(
            title=str(issue["title"]),
            body=str(issue.get("body") or ""),
            comments=feedback_text,
            source_paths=branch_paths,
            discussion_resolved=True,
        )
        files = github.get_source_files(triage.source_paths, branch=actual_input.branch)
    else:
        files = {}
    plan = _generate_code_patch(
        github=github,
        title=str(issue["title"]),
        body=str(issue.get("body") or ""),
        comments=[],
        files=files,
        existing_paths=existing_paths,
        review_feedback=feedback_text,
        branch=actual_input.branch,
    )
    _validate_patch(plan, allow_all_repository_paths=DEVELOPER_AGENT != "api")
    processed_cursor = dict(cursor)
    for item in pending:
        source = str(item["source"])
        processed_cursor[source] = max(processed_cursor[source], int(item["id"]))
    if not plan.changes:
        github.record_pull_feedback(
            input.pull_number,
            processed_cursor,
            "The automated agent reviewed the new feedback and found no safe, actionable code changes.",
        )
        logger.info(
            "Pull-request feedback reviewed with no changes: repository=%s "
            "issue=%d pull_request=%d feedback_items=%d",
            input.repository,
            actual_input.issue_number,
            input.pull_number,
            len(pending),
        )
        return EventResult(
            status="feedback-reviewed-no-changes",
            issue_number=actual_input.issue_number,
            pull_number=input.pull_number,
        )

    github.update_pull_request_branch(
        branch=actual_input.branch,
        issue_number=actual_input.issue_number,
        feedback_cursor=processed_cursor,
        changes=plan.changes,
    )
    github.record_pull_feedback(
        input.pull_number,
        processed_cursor,
        "I pushed a follow-up commit addressing the actionable pull-request feedback. "
        "Please review the updated branch.",
    )
    logger.info(
        "Pull-request feedback addressed: repository=%s issue=%d "
        "pull_request=%d changed_files=%d feedback_items=%d",
        input.repository,
        actual_input.issue_number,
        input.pull_number,
        len(plan.changes),
        len(pending),
    )
    return EventResult(
        status="feedback-addressed",
        issue_number=actual_input.issue_number,
        pull_number=input.pull_number,
    )


@hatchet.task(
    name="inspect-github-pull-request-event",
    desired_worker_labels=[SIMPLE_WORKER_LABEL],
    input_validator=GitHubWebhook,
    on_events=[PR_REVIEW_EVENT, PR_CLOSED_EVENT],
    execution_timeout=timedelta(seconds=30),
)
def inspect_pull_request_event(input: GitHubWebhook, ctx: Context) -> EventResult:
    pull = input.pull_request
    if not pull:
        return EventResult(status="ignored")
    return EventResult(
        status="observed", pull_number=int(pull.get("number", 0)) or None
    )


def _start_discussion_wait(issue: IssueInput) -> None:
    wait_for_ready_for_agent.run(
        issue,
        wait_for_result=False,
        child_key=f"discussion-{issue.issue_number}",
    )


def _start_issue_discussion(
    github: GitHubClient,
    issue_input: IssueInput,
    title: str,
    questions: list[str],
) -> None:
    logger.info(
        "Waiting for issue clarification: repository=%s issue=%d title=%s",
        issue_input.repository,
        issue_input.issue_number,
        _log_title(title),
    )
    question_list = "\n".join(f"- {question}" for question in questions)
    comment = (
        "I need clarification before preparing a code change:\n\n"
        f"{question_list}\n\n"
        f"Once resolved, add the `{READY_FOR_AGENT}` label."
    )
    assert_safe_text(comment)
    _start_discussion_wait(issue_input)
    github.ensure_label(READY_FOR_AGENT)
    github.ensure_label(NEEDS_DISCUSSION)
    github.add_label(issue_input.issue_number, NEEDS_DISCUSSION)
    github.comment_once(
        issue_input.issue_number,
        marker="<!-- hatchet-issue-agent-discussion -->",
        comment=comment,
    )


def _register_pull_wait(github: GitHubClient, pull: PullInput) -> None:
    logger.info(
        "Registering pull-request review wait: repository=%s issue=%d "
        "pull_request=%d url=%s branch=%s",
        pull.repository,
        pull.issue_number,
        pull.pull_number,
        pull.pull_url,
        pull.branch,
    )
    github.ensure_label(PR_OPEN)
    github.add_label(pull.issue_number, PR_OPEN)
    github.comment_once(
        pull.issue_number,
        marker=f"<!-- hatchet-issue-agent-pr-{pull.pull_number} -->",
        comment=f"Draft pull request created: {pull.pull_url}",
    )
    wait_for_pull_review.run(
        pull,
        wait_for_result=False,
        child_key=f"pull-review-{pull.pull_number}",
    )


def _mark_pull_reviewed(github: GitHubClient, pull_number: int) -> None:
    github.ensure_label(PR_REVIEWED)
    github.add_label(pull_number, PR_REVIEWED)


def _pull_input_from_api(
    github: GitHubClient, pull: dict[str, Any]
) -> PullInput | None:
    number = pull.get("number")
    url = pull.get("html_url")
    branch = (pull.get("head") or {}).get("ref")
    body = str(pull.get("body") or "")
    linked_issue = re.search(
        r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(\d+)\b",
        body,
    )
    if (
        not isinstance(number, int)
        or not isinstance(url, str)
        or not isinstance(branch, str)
        or linked_issue is None
    ):
        return None
    issue_number = int(linked_issue.group(1))
    issue = github.get_issue(issue_number)
    if issue.get("state") != "open" or PR_OPEN not in labels_for(issue):
        return None
    return PullInput(
        repository=github.repository,
        issue_number=issue_number,
        pull_number=number,
        pull_url=url,
        branch=branch,
    )


def _format_review_feedback(item: dict[str, Any]) -> str:
    body = re.sub(
        r"\s*<!-- hatchet-issue-agent-review-\d+ -->",
        "",
        str(item["body"]),
    ).strip()
    location = ""
    if item.get("path"):
        location = f" on {item['path']}"
        if item.get("line") is not None:
            location += f" at line {item['line']}"
    author = str(item.get("author") or "unknown")
    return f"Comment {item['id']} ({item['source']}) by {author}{location}:\n{body}"


def _sub_issue_marker(parent_number: int, step_number: int) -> str:
    return f"<!-- hatchet-parent-issue-{parent_number}-step-{step_number} -->"


def _stored_issue_plan(comments: list[str]) -> IssueWorkPlan | None:
    marker_pattern = re.compile(r"<!-- hatchet-issue-work-plan:([A-Za-z0-9_-]+) -->")
    for comment in comments:
        match = marker_pattern.search(comment)
        if not match:
            continue
        encoded = match.group(1)
        try:
            padding = "=" * (-len(encoded) % 4)
            plan_json = base64.urlsafe_b64decode(encoded + padding)
            return IssueWorkPlan.model_validate_json(plan_json)
        except (ValueError, ValidationError):
            raise ModelError("Stored issue plan is invalid") from None
    return None


def _format_issue_plan_comment(plan: IssueWorkPlan) -> str:
    lines = ["Automated implementation plan; each subtask can be worked independently:"]
    for index, task in enumerate(plan.tasks, start=1):
        lines.append(f"{index}. **{task.title}** — {task.objective}")
        lines.extend(f"   - {criterion}" for criterion in task.acceptance_criteria)
    encoded = (
        base64.urlsafe_b64encode(plan.model_dump_json().encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return "\n".join(lines) + f"\n\n{PLAN_MARKER_PREFIX}{encoded} -->"


def _format_sub_issue_body(
    *,
    parent_number: int,
    parent_url: str,
    objective: str,
    acceptance_criteria: list[str],
    marker: str,
) -> str:
    parent_reference = (
        f"[#{parent_number}]({parent_url})" if parent_url else f"#{parent_number}"
    )
    lines = [
        f"Part of parent issue {parent_reference}.",
        "",
        "## Objective",
        objective,
        "",
        "## Acceptance criteria",
        *(f"- {criterion}" for criterion in acceptance_criteria),
        "",
        marker,
    ]
    body = "\n".join(lines)
    assert_safe_text(body)
    return body


def _pending_pull_feedback(
    feedback: list[dict[str, Any]],
    cursor: dict[str, int],
    pull_number: int,
) -> list[dict[str, Any]]:
    review_marker = f"<!-- hatchet-issue-agent-review-{pull_number} -->"
    pending = []
    for item in feedback:
        body = str(item["body"])
        is_worker_review = review_marker in body
        has_agent_marker = "<!-- hatchet-" in body
        if int(item["id"]) > cursor[str(item["source"])] and (
            not has_agent_marker or is_worker_review
        ):
            pending.append(item)
    return pending


def _generate_code_patch(
    *,
    github: GitHubClient,
    title: str,
    body: str,
    comments: list[str],
    files: dict[str, str],
    existing_paths: set[str],
    review_feedback: list[str] | None = None,
    branch: str | None = None,
) -> PatchPlan:
    if DEVELOPER_AGENT == "api":
        return ModelClient().generate_patch(
            title=title,
            body=body,
            comments=comments,
            files=files,
            existing_paths=existing_paths,
            review_feedback=review_feedback,
        )
    logger.info(
        "Starting isolated CLI code generation: agent=%s branch=%s",
        DEVELOPER_AGENT,
        branch or "default",
    )
    return generate_cli_patch(
        title=title,
        body=body,
        comments=comments,
        existing_paths=existing_paths,
        archive=github.get_repository_archive(branch),
        review_feedback=review_feedback,
    )


def _log_title(title: str) -> str:
    if find_sensitive_categories(title):
        return "[redacted]"
    return re.sub(r"\s+", " ", title).strip()[:160]


def _format_pull_request_review(review: PullRequestReview) -> str:
    if not review.findings:
        return (
            "Automated review completed. No actionable defects were identified "
            "in the pull-request diff."
        )
    lines = ["Automated review found the following actionable concerns:"]
    for finding in review.findings:
        location = f"`{finding.path}`"
        if finding.line is not None:
            location += f" (line {finding.line})"
        lines.append(
            f"- **{finding.kind.upper()} / {finding.severity.upper()}** "
            f"{location}: {finding.description}"
        )
    comment = "\n".join(lines)
    assert_safe_text(comment)
    return comment


def _validate_patch(
    plan: PatchPlan, *, allow_all_repository_paths: bool = False
) -> None:
    if not plan.changes:
        return
    if len(plan.changes) > MAX_PATCH_FILES:
        raise ModelError("Patch contains too many files")
    seen: set[str] = set()
    for change in plan.changes:
        path_is_allowed = (
            is_safe_repository_path(change.path)
            if allow_all_repository_paths
            else is_allowed_source_path(change.path)
        )
        if not path_is_allowed or change.path in seen:
            raise ModelError("Patch contains a disallowed or duplicate path")
        seen.add(change.path)
        if len(change.content.encode("utf-8")) > 32_768:
            raise ModelError("Patch file exceeds the size limit")
        assert_safe_text(change.content)


worker = hatchet.worker(
    os.getenv("HATCHET_WORKER_NAME", f"software-factory-{WORKER_TYPE}-worker"),
    labels={"worker_type": WORKER_TYPE},
    workflows=[
        issue_workflow,
        handle_github_issue_event,
        trigger_issue_from_sample_event,
        scan_pending_issues,
        wait_for_ready_for_agent,
        wait_for_pull_review,
        verify_merged_pr,
        inspect_pull_request_event,
        scan_open_pr_feedback,
        handle_pull_request_feedback_event,
        process_pull_request_feedback,
    ],
)


if __name__ == "__main__":
    worker.start()
