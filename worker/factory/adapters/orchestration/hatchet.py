"""Hatchet registration and execution semantics. Importing does not create a client."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from hatchet_sdk import Context, DesiredWorkerLabel, DurableContext, EmptyModel

from hatchet_sdk.types.concurrency import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
)
from hatchet_sdk.types.idempotency import StatusBasedIdempotencyConfig
from hatchet_sdk.exceptions import IdempotencyCollisionError

from factory.domain.models import (
    IssueInput,
    IssueState,
    IssuePlanState,
    TriageState,
    PublishState,
    EventResult,
    ScanResult,
    FeedbackScanResult,
    PullInput,
)
from factory.adapters.github_events import GitHubWebhook, normalize_webhook
from factory.adapters.telemetry import correlation
from factory.application.telemetry import capture_trace, incoming_trace
from factory.adapters.control import engine_context

ISSUE_KEY = "input.repository + ':issue:' + string(input.issue_number)"


def issue_concurrency():
    return ConcurrencyExpression(
        expression=ISSUE_KEY,
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
        name="software-factory-work-item",
        is_tenant_scoped=True,
    )


ISSUE_EVENT = "issue-agent:issue"
PR_REVIEW_EVENT = "github:pull_request_review:submitted"
PR_CLOSED_EVENT = "github:pull_request:closed"
PR_FEEDBACK_EVENTS = [
    "github:issue_comment:created",
    "github:pull_request_review_comment:created",
    PR_REVIEW_EVENT,
]


def create_client(settings):
    from hatchet_sdk import Hatchet
    from hatchet_sdk.config import ClientConfig, ClientTLSConfig

    return Hatchet(
        config=ClientConfig(
            tls_config=ClientTLSConfig(strategy=settings.hatchet_tls_strategy)
        )
    )


class HatchetDispatch:
    def __init__(self):
        self.tasks = None
        self.observer = None

    @contextmanager
    def _prepare(self, command):
        if self.observer is None:
            yield command
            return
        with (
            incoming_trace(command.trace_context),
            self.observer.observe(
                "dispatch", {"factory_run_id": command.factory_run_id}
            ),
        ):
            yield command.model_copy(
                update={"trace_context": capture_trace() or command.trace_context}
            )

    def enqueue_issue(self, issue):
        with self._prepare(issue) as issue:
            try:
                self.tasks.issue_workflow.run(issue, wait_for_result=False)
            except IdempotencyCollisionError as collision:
                logging.getLogger("issue-worker.dispatch").info(
                    "Issue workflow already active: repository=%s issue=%d existing_run_id=%s",
                    issue.repository,
                    issue.issue_number,
                    collision.existing_run_external_id,
                )
                return False
            return True

    def wait_for_discussion(self, issue):
        with self._prepare(issue) as issue:
            self.tasks.wait_for_ready_for_agent.run(
                issue,
                wait_for_result=False,
                child_key=f"discussion-{issue.issue_number}",
            )

    def wait_for_review(self, pull):
        with self._prepare(pull) as pull:
            self.tasks.wait_for_pull_review.run(
                pull, wait_for_result=False, child_key=f"pull-review-{pull.pull_number}"
            )

    def enqueue_feedback(self, pull):
        with self._prepare(pull) as pull:
            self.tasks.process_pull_request_feedback.run(pull, wait_for_result=False)

    def verify_merge(self, pull):
        with self._prepare(pull) as pull:
            self.tasks.verify_merged_pr.run(pull, wait_for_result=False)


def build_worker(hatchet, service, settings, dispatch):
    dispatch.observer = service.observer
    SCAN_CRON = settings.scan_cron

    def task_options(name, *, serialize_issue=False):
        task = settings.task(name)
        options = {
            "desired_worker_labels": [
                DesiredWorkerLabel(
                    key=settings.worker_label_key, value=task.worker_type, required=True
                )
            ],
            "execution_timeout": timedelta(seconds=task.timeout_seconds),
            "schedule_timeout": timedelta(seconds=task.schedule_timeout_seconds),
            "retries": task.retries,
            "backoff_factor": task.backoff_factor,
            "backoff_max_seconds": task.backoff_max_seconds,
        }
        if task.concurrency is not None:
            options["concurrency"] = task.concurrency
        if serialize_issue:
            options["concurrency"] = [issue_concurrency()]
            if task.concurrency is not None:
                options["concurrency"].append(
                    ConcurrencyExpression.from_int(task.concurrency)
                )
        return options

    issue_workflow = hatchet.workflow(
        name="software-factory-issue-agent",
        input_validator=IssueInput,
        concurrency=[
            issue_concurrency(),
            ConcurrencyExpression.from_int(settings.workflow_concurrency),
        ],
        idempotency=StatusBasedIdempotencyConfig(
            key_expression=ISSUE_KEY,
            fallback_ttl=timedelta(seconds=settings.workflow_dedup_ttl_seconds),
        ),
    )

    @issue_workflow.task(
        **task_options("load_issue"),
        name="load-issue",
    )
    @engine_context
    def load_issue(input: IssueInput, ctx: Context) -> IssueState:
        with correlation(input.factory_run_id):
            return service.load_issue(input)

    @issue_workflow.task(
        **task_options("plan_issue"),
        name="plan-issue-and-create-sub-issues",
        parents=[load_issue],
    )
    @engine_context
    def plan_issue(input: IssueInput, ctx: Context) -> IssuePlanState:
        with correlation(input.factory_run_id):
            return service.plan_issue(input, ctx.task_output(load_issue))

    @issue_workflow.task(
        **task_options("triage_issue"),
        name="triage-issue",
        parents=[load_issue, plan_issue],
    )
    @engine_context
    def triage_issue(input: IssueInput, ctx: Context) -> TriageState:
        with correlation(input.factory_run_id):
            return service.triage_issue(
                input, ctx.task_output(load_issue), ctx.task_output(plan_issue)
            )

    @issue_workflow.task(
        **task_options("generate_and_open_draft_pr"),
        name="generate-and-open-draft-pr",
        parents=[triage_issue],
    )
    @engine_context
    def generate_and_open_draft_pr(input: IssueInput, ctx: Context) -> PublishState:
        with correlation(input.factory_run_id):
            return service.generate_and_open_draft_pr(
                input, ctx.task_output(triage_issue)
            )

    @issue_workflow.task(
        **task_options("review_opened_pull_request"),
        name="review-opened-pull-request",
        parents=[generate_and_open_draft_pr],
    )
    @engine_context
    def review_opened_pull_request(input: IssueInput, ctx: Context) -> EventResult:
        with correlation(input.factory_run_id):
            return service.review_opened_pull_request(
                input, ctx.task_output(generate_and_open_draft_pr)
            )

    @issue_workflow.task(
        **task_options("record_workflow_result"),
        name="record-workflow-result",
        parents=[review_opened_pull_request],
    )
    @engine_context
    def record_workflow_result(input: IssueInput, ctx: Context) -> EventResult:
        with correlation(input.factory_run_id):
            return service.record_workflow_result(
                input, ctx.task_output(review_opened_pull_request)
            )

    @hatchet.task(
        **task_options("verify_merged_pr", serialize_issue=True),
        name="verify-merged-pr-and-close-issue",
        input_validator=PullInput,
    )
    @engine_context
    def verify_merged_pr(input: PullInput, ctx: Context) -> EventResult:
        with correlation(input.factory_run_id):
            return service.verify_merged_pr(input)

    @hatchet.task(
        **task_options("process_pull_request_feedback", serialize_issue=True),
        name="process-open-pull-request-feedback",
        input_validator=PullInput,
    )
    @engine_context
    def process_pull_request_feedback(input: PullInput, ctx: Context) -> EventResult:
        with correlation(input.factory_run_id):
            return service.process_pull_request_feedback(input)

    @hatchet.task(
        **task_options("scan_pending_issues"),
        name="scan-pending-github-issues",
        input_validator=EmptyModel,
        on_crons=[SCAN_CRON],
    )
    @engine_context
    def scan_pending_issues(input: EmptyModel, ctx: Context) -> ScanResult:
        with service.observer.observe("scan_pending_issues", {}):
            return service.scan_pending_issues()

    @hatchet.task(
        **task_options("scan_open_pr_feedback"),
        name="scan-open-pr-feedback",
        input_validator=EmptyModel,
        on_crons=[SCAN_CRON],
    )
    @engine_context
    def scan_open_pr_feedback(input: EmptyModel, ctx: Context) -> FeedbackScanResult:
        with service.observer.observe("scan_open_pr_feedback", {}):
            return service.scan_open_pr_feedback()

    @hatchet.task(
        **task_options("handle_github_issue_event"),
        name="handle-github-issue-event",
        input_validator=GitHubWebhook,
        on_events=[
            "github:issues:opened",
            "github:issues:reopened",
            "github:issues:labeled",
        ],
    )
    @engine_context
    def handle_github_issue_event(input: GitHubWebhook, ctx: Context) -> EventResult:
        with service.observer.observe("handle_github_issue_event", {}):
            return service.handle_event(
                normalize_webhook(
                    input, "issues", ready_label=settings.labels.ready_for_agent
                )
            )

    @hatchet.task(
        **task_options("handle_pull_request_feedback_event"),
        name="handle-github-pull-request-feedback-event",
        input_validator=GitHubWebhook,
        on_events=PR_FEEDBACK_EVENTS,
    )
    @engine_context
    def handle_pull_request_feedback_event(
        input: GitHubWebhook, ctx: Context
    ) -> EventResult:
        with service.observer.observe("handle_pull_request_feedback_event", {}):
            return service.handle_event(normalize_webhook(input, "feedback"))

    @hatchet.task(
        **task_options("inspect_pull_request_event"),
        name="inspect-github-pull-request-event",
        input_validator=GitHubWebhook,
        on_events=[PR_REVIEW_EVENT, PR_CLOSED_EVENT],
    )
    @engine_context
    def inspect_pull_request_event(input: GitHubWebhook, ctx: Context) -> EventResult:
        with service.observer.observe("inspect_pull_request_event", {}):
            return service.handle_event(normalize_webhook(input, "pull_request"))

    @hatchet.task(
        **task_options("trigger_issue_from_sample_event"),
        name="trigger-issue-from-sample-event",
        input_validator=IssueInput,
        on_events=[ISSUE_EVENT],
    )
    @engine_context
    def trigger_issue_from_sample_event(input: IssueInput, ctx: Context) -> EventResult:
        with service.observer.observe(
            "trigger_issue_from_sample_event", {"factory_run_id": input.factory_run_id}
        ):
            with correlation(input.factory_run_id):
                service.validate_repository(input.repository)
                dispatch.enqueue_issue(input)
                return EventResult(status="queued", issue_number=input.issue_number)

    @hatchet.durable_task(
        **task_options("wait_for_ready_for_agent"),
        name="wait-for-ready-for-agent",
        input_validator=IssueInput,
    )
    @engine_context
    async def wait_for_ready_for_agent(
        input: IssueInput, ctx: DurableContext
    ) -> dict[str, str]:
        with correlation(input.factory_run_id):
            while True:
                status = service.discussion_status(input)
                if status == "ready" and dispatch.enqueue_issue(input):
                    return {"status": "resumed"}
                if status == "issue-not-open":
                    return {"status": status}
                await ctx.aio_sleep_for(
                    timedelta(seconds=settings.discussion_poll_seconds),
                    label=f"waiting for clarification on issue {input.issue_number}",
                )

    @hatchet.durable_task(
        **task_options("wait_for_pull_review"),
        name="wait-for-pr-review-and-merge",
        input_validator=PullInput,
    )
    @engine_context
    async def wait_for_pull_review(
        input: PullInput, ctx: DurableContext
    ) -> dict[str, str]:
        with correlation(input.factory_run_id):
            while True:
                status = service.pull_wait_status(input)
                if status == "merged":
                    dispatch.verify_merge(input)
                    return {"status": "merged"}
                if status == "closed-unmerged":
                    return {"status": status}
                await ctx.aio_sleep_for(
                    timedelta(seconds=settings.discussion_poll_seconds),
                    label=f"waiting for review and merge of PR {input.pull_number}",
                )

    dispatch.tasks = SimpleNamespace(
        issue_workflow=issue_workflow,
        wait_for_ready_for_agent=wait_for_ready_for_agent,
        wait_for_pull_review=wait_for_pull_review,
        process_pull_request_feedback=process_pull_request_feedback,
        verify_merged_pr=verify_merged_pr,
    )
    return hatchet.worker(
        settings.worker_name or f"software-factory-{settings.worker_type}-worker",
        labels={settings.worker_label_key: settings.worker_type},
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
