from __future__ import annotations

import logging
import re
from typing import Callable

from factory.domain.models import (
    EventResult,
    FeedbackScanResult,
    IssueInput,
    IssuePlanState,
    IssueState,
    IssueWorkPlan,
    ModelError,
    PatchPlan,
    PublishState,
    PullInput,
    RepositoryError,
    ScanResult,
    StaleRevisionError,
    TriageState,
)
from factory.domain.policy import ExecutionPolicy
from factory.configuration import Labels, TaskSettings
from factory.domain.safety import assert_safe_text
from factory.application.ports import (
    ChangeReviewer,
    CodingAgent,
    CodingRequest,
    FactoryStateStore,
    OperationObserver,
    Planner,
    Repository,
    RepositoryFactory,
    WorkflowDispatch,
)
from factory.application.operations import operation
from factory.application.helpers import (
    PLAN_MARKER_PREFIX,
    labels_for,
    _issue_snapshot,
    _log_title,
    _stored_issue_plan,
    _format_issue_plan_comment,
    _sub_issue_marker,
    _format_sub_issue_body,
    _format_pull_request_review,
    _pull_input_from_api,
    _pending_pull_feedback,
    _format_review_feedback,
)

logger = logging.getLogger("issue-worker.application")


class FactoryService:
    def __init__(
        self,
        *,
        repository: str,
        repositories: RepositoryFactory,
        planner: Callable[[], Planner],
        reviewer: Callable[[], ChangeReviewer],
        coding_agent: Callable[[], CodingAgent],
        dispatch: WorkflowDispatch,
        policy: ExecutionPolicy,
        state: FactoryStateStore,
        observer: OperationObserver,
        labels: Labels | None = None,
        task_settings=None,
    ):
        self.repository = repository
        self.repositories = repositories
        self.planner = planner
        self.reviewer = reviewer
        self.coding_agent = coding_agent
        self.dispatch = dispatch
        self.policy = policy
        self.state = state
        self.observer = observer
        self.labels = labels or Labels()
        self.task_settings = task_settings or (lambda stage: TaskSettings())

    def validate_repository(self, repository: str) -> None:
        if repository != self.repository:
            raise ValueError("Event repository does not match configured repository")

    @operation("load_issue", exclusive=False)
    def load_issue(self, input: IssueInput) -> IssueState:
        self.validate_repository(input.repository)
        issue = self.repositories(input.repository).get_issue(input.issue_number)
        title = str(issue.get("title") or "")
        logger.info(
            "Loaded issue: repository=%s issue=%d state=%s title=%s",
            input.repository,
            input.issue_number,
            issue.get("state"),
            _log_title(title),
        )
        return _issue_snapshot(issue)

    @operation("plan_issue", exclusive=True)
    def plan_issue(self, input: IssueInput, state: IssueState) -> IssuePlanState:
        if state.state != "open":
            return IssuePlanState(
                status="issue-not-open", issue_number=input.issue_number
            )

        github = self.repositories(input.repository)
        issue = github.get_issue(input.issue_number)
        labels = labels_for(issue)
        if self.labels.pr_open in labels:
            return IssuePlanState(
                status="issue-already-in-progress", issue_number=input.issue_number
            )
        if self.labels.plan_subtask in labels:
            return IssuePlanState(status="subtask", issue_number=input.issue_number)
        if self.labels.plan_created in labels:
            return IssuePlanState(
                status="already-planned", issue_number=input.issue_number
            )
        if (
            self.labels.needs_discussion in labels
            and self.labels.ready_for_agent not in labels
        ):
            logger.info(
                "Deferring planning until issue discussion is resolved: "
                "repository=%s issue=%d",
                input.repository,
                input.issue_number,
            )
            self.dispatch.wait_for_discussion(input)
            return IssuePlanState(
                status="waiting-for-discussion", issue_number=input.issue_number
            )

        comments = github.get_issue_comments(input.issue_number)
        source_paths = github.list_source_paths()
        title = str(issue.get("title") or "")
        body = str(issue.get("body") or "")
        assert_safe_text(title, body, *comments, "\n".join(source_paths))

        discussion_resolved = self.labels.ready_for_agent in labels
        triage = self.planner().triage(
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
            self._start_issue_discussion(
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
            github.remove_label(input.issue_number, self.labels.needs_discussion)
            github.remove_label(input.issue_number, self.labels.ready_for_agent)

        plan_key = f"plan:v1:{input.repository}:{input.issue_number}"
        stored_plan = self.state.get(plan_key)
        plan = (
            IssueWorkPlan.model_validate(stored_plan)
            if stored_plan
            else _stored_issue_plan(comments)
        )
        if plan is None:
            plan = self.planner().plan_issue(
                title=title,
                body=body,
                comments=comments,
                source_paths=source_paths,
            )
            self.state.put(plan_key, plan.model_dump(mode="json"))
        if not any(PLAN_MARKER_PREFIX in comment for comment in comments):
            plan_comment = _format_issue_plan_comment(plan)
            github.comment_once(
                input.issue_number,
                marker=f"{PLAN_MARKER_PREFIX}",
                comment=plan_comment,
            )

        self.state.put(plan_key, plan.model_dump(mode="json"))
        markers = {
            _sub_issue_marker(input.issue_number, index)
            for index in range(1, len(plan.tasks) + 1)
        }
        existing = github.find_issues_by_markers(markers)
        linked_issue_ids = github.list_sub_issue_ids(input.issue_number)
        github.ensure_label(self.labels.plan_subtask)
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
                    labels=[self.labels.plan_subtask],
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

        github.ensure_label(self.labels.plan_created)
        github.add_label(input.issue_number, self.labels.plan_created)
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

    @operation("triage_issue", exclusive=True)
    def triage_issue(
        self, input: IssueInput, state: IssueState, plan_state: IssuePlanState
    ) -> TriageState:
        if plan_state.status in {"sub-issues-created", "already-planned"}:
            return TriageState(
                status=plan_state.status, issue_number=input.issue_number
            )
        if plan_state.status == "waiting-for-discussion":
            return TriageState(
                status="waiting-for-discussion", issue_number=input.issue_number
            )
        if plan_state.status in {"issue-not-open", "issue-already-in-progress"}:
            return TriageState(status="not-pending", issue_number=input.issue_number)
        if state.state != "open" or self.labels.pr_open in state.labels:
            return TriageState(status="not-pending", issue_number=input.issue_number)

        github = self.repositories(input.repository)
        issue = github.get_issue(input.issue_number)
        labels = labels_for(issue)
        discussion_resolved = self.labels.ready_for_agent in labels

        if self.labels.needs_discussion in labels and not discussion_resolved:
            logger.info(
                "Issue requires discussion: repository=%s issue=%d title=%s",
                input.repository,
                input.issue_number,
                _log_title(str(issue.get("title") or "")),
            )
            github.ensure_label(self.labels.ready_for_agent)
            github.ensure_label(self.labels.needs_discussion)
            self.dispatch.wait_for_discussion(input)
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
        plan = self.planner().triage(
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
            github.remove_label(input.issue_number, self.labels.needs_discussion)
            github.remove_label(input.issue_number, self.labels.ready_for_agent)

        if plan.needs_discussion:
            self._start_issue_discussion(github, input, title, plan.questions)
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

    @operation("generate_and_open_draft_pr", exclusive=True)
    def generate_and_open_draft_pr(
        self, input: IssueInput, triage: TriageState
    ) -> PublishState:
        if triage.status != "ready":
            return PublishState(status=triage.status, issue_number=input.issue_number)

        github = self.repositories(input.repository)
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
                trace_context=input.trace_context,
                factory_run_id=input.factory_run_id,
                repository=input.repository,
                issue_number=input.issue_number,
                pull_number=int(existing["number"]),
                pull_url=str(existing["html_url"]),
                branch=str(existing["head"]["ref"]),
            )
            self._register_pull_wait(github, pull_input)
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
        patch_key = f"patch:v1:{input.repository}:{input.issue_number}"
        prepared = self.state.get(patch_key)
        if prepared is not None:
            base_commit = prepared["base_commit"]
            plan = PatchPlan.model_validate(prepared["plan"])
        else:
            base_commit = github.resolve_revision()
            files = (
                github.get_source_files(triage.source_paths, branch=base_commit)
                if self.coding_agent().capabilities.context == "selected-files"
                else {}
            )
            plan = self._generate_code_patch(
                github=github,
                title=title,
                body=body,
                comments=comments,
                files=files,
                existing_paths=github.list_repository_paths(base_commit),
                branch=base_commit,
            )
            self.policy.validate_patch(plan)
            if plan.changes:
                self.state.put(
                    patch_key,
                    {"base_commit": base_commit, "plan": plan.model_dump(mode="json")},
                )
        self.policy.validate_patch(plan)
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
        try:
            pull = github.create_draft_pull_request(
                issue_number=input.issue_number,
                title=plan.pull_title,
                body=plan.pull_body,
                branch=branch,
                changes=plan.changes,
                expected_base_sha=base_commit,
            )
        except StaleRevisionError:
            self.state.delete(patch_key)
            raise
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
            trace_context=input.trace_context,
            factory_run_id=input.factory_run_id,
            repository=input.repository,
            issue_number=input.issue_number,
            pull_number=int(pull["number"]),
            pull_url=str(pull["html_url"]),
            branch=branch,
        )
        self._register_pull_wait(github, pull_input)
        return PublishState(
            status="draft-pr-created",
            issue_number=input.issue_number,
            pull_number=pull_input.pull_number,
            pull_url=pull_input.pull_url,
            branch=branch,
        )

    @operation("review_opened_pull_request", exclusive=True)
    def review_opened_pull_request(
        self, input: IssueInput, result: PublishState
    ) -> EventResult:
        if (
            result.status not in {"draft-pr-created", "draft-pr-exists"}
            or result.pull_number is None
        ):
            return EventResult(
                status=f"review-skipped-{result.status}",
                issue_number=input.issue_number,
                pull_number=result.pull_number,
            )

        github = self.repositories(input.repository)
        pull = github.get_pull_request(result.pull_number)
        if self.labels.pr_reviewed in labels_for(pull):
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
        if any(
            marker in body for body in github.get_issue_comments(result.pull_number)
        ):
            self._mark_pull_reviewed(github, result.pull_number)
            logger.info(
                "Pull-request review already posted and labeled: repository=%s "
                "issue=%d pull_request=%d label=%s",
                input.repository,
                input.issue_number,
                result.pull_number,
                self.labels.pr_reviewed,
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
            raise RepositoryError("Pull request has no head branch")
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
        context_plan = self.planner().triage(
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
        assert_safe_text(
            *(f"{path}\n{content}" for path, content in current_files.items())
        )
        review = self.reviewer().review_pull_request(
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
        self._mark_pull_reviewed(github, result.pull_number)
        logger.info(
            "Post-creation pull-request review completed and labeled: repository=%s "
            "issue=%d pull_request=%d findings=%d suggestions=%d label=%s",
            input.repository,
            input.issue_number,
            result.pull_number,
            len(review.findings),
            sum(finding.kind == "suggestion" for finding in review.findings),
            self.labels.pr_reviewed,
        )
        return EventResult(
            status="review-posted",
            issue_number=input.issue_number,
            pull_number=result.pull_number,
        )

    @operation("record_workflow_result", exclusive=False)
    def record_workflow_result(
        self, input: IssueInput, result: EventResult
    ) -> EventResult:
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

    @operation("verify_merged_pr", exclusive=True)
    def verify_merged_pr(self, input: PullInput) -> EventResult:
        github = self.repositories(input.repository)
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
            raise RepositoryError("Pull request is not merged")
        merge_sha = pull.get("merge_commit_sha")
        if not isinstance(merge_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", merge_sha
        ):
            raise RepositoryError("Merged pull request has no valid merge commit")
        github.verify_commit_checks(merge_sha)
        github.comment_once(
            input.issue_number,
            marker="<!-- hatchet-issue-agent-merged -->",
            comment=f"Verified the merged change: {input.pull_url}",
        )
        github.close_issue(input.issue_number)
        github.remove_label(input.issue_number, self.labels.pr_open)
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

    def scan_pending_issues(self) -> ScanResult:
        github = self.repositories(self.repository)
        for issue in github.list_open_issues():
            labels = labels_for(issue)
            if (
                self.labels.pr_open in labels
                or self.labels.needs_discussion in labels
                or self.labels.plan_created in labels
            ):
                continue
            number = int(issue["number"])
            title = str(issue.get("title") or "")
            logger.info(
                "Queueing pending issue: repository=%s issue=%d title=%s",
                self.repository,
                number,
                _log_title(title),
            )
            self.dispatch.enqueue_issue(
                IssueInput(repository=self.repository, issue_number=number),
            )
            return ScanResult(status="queued", issue_number=number)
        return ScanResult(status="no-pending-issues")

    def scan_open_pr_feedback(self) -> FeedbackScanResult:
        github = self.repositories(self.repository)
        queued = 0
        for pull in github.list_open_pull_requests():
            pull_input = _pull_input_from_api(
                github, pull, pr_open_label=self.labels.pr_open
            )
            if pull_input is None:
                continue
            logger.info(
                "Queueing pull-request feedback check: repository=%s issue=%d "
                "pull_request=%d title=%s url=%s",
                self.repository,
                pull_input.issue_number,
                pull_input.pull_number,
                _log_title(str(pull.get("title") or "")),
                pull.get("html_url"),
            )
            self.dispatch.enqueue_feedback(pull_input)
            queued += 1
        return FeedbackScanResult(status="queued", pull_requests_queued=queued)

    @operation("process_pull_request_feedback", exclusive=True)
    def process_pull_request_feedback(self, input: PullInput) -> EventResult:
        self.validate_repository(input.repository)
        github = self.repositories(input.repository)
        pull = github.get_pull_request(input.pull_number)
        if pull.get("state") != "open":
            return EventResult(
                status="pull-request-not-open", pull_number=input.pull_number
            )
        actual_input = _pull_input_from_api(
            github, pull, pr_open_label=self.labels.pr_open
        )
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
        cursor_key = f"feedback:v1:{input.repository}:{input.pull_number}"
        stored_cursor = self.state.get(cursor_key) or {}
        for source in cursor:
            cursor[source] = max(cursor[source], int(stored_cursor.get(source, 0)))
        pending = _pending_pull_feedback(feedback, cursor, input.pull_number)
        if not pending:
            logger.info(
                "No new pull-request feedback: repository=%s issue=%d pull_request=%d",
                input.repository,
                input.issue_number,
                input.pull_number,
            )
            return EventResult(status="no-new-feedback", pull_number=input.pull_number)

        if issue.get("state") != "open" or self.labels.pr_open not in labels_for(issue):
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
        base_commit = github.resolve_revision(actual_input.branch)
        branch_paths = github.list_source_paths(base_commit)
        assert_safe_text(
            str(issue.get("title") or ""),
            str(issue.get("body") or ""),
            *feedback_text,
            "\n".join(branch_paths),
        )
        existing_paths = github.list_repository_paths(base_commit)
        if self.coding_agent().capabilities.context == "selected-files":
            triage = self.planner().triage(
                title=str(issue["title"]),
                body=str(issue.get("body") or ""),
                comments=feedback_text,
                source_paths=branch_paths,
                discussion_resolved=True,
            )
            files = github.get_source_files(triage.source_paths, branch=base_commit)
        else:
            files = {}
        plan = self._generate_code_patch(
            github=github,
            title=str(issue["title"]),
            body=str(issue.get("body") or ""),
            comments=[],
            files=files,
            existing_paths=existing_paths,
            review_feedback=feedback_text,
            branch=base_commit,
        )
        self.policy.validate_patch(plan)
        processed_cursor = dict(cursor)
        for item in pending:
            source = str(item["source"])
            processed_cursor[source] = max(processed_cursor[source], int(item["id"]))
        if not plan.changes:
            self.state.put(cursor_key, processed_cursor)
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
            expected_base_sha=base_commit,
        )
        self.state.put(cursor_key, processed_cursor)
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

    def _start_issue_discussion(
        self,
        github: Repository,
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
            f"Once resolved, add the `{self.labels.ready_for_agent}` label."
        )
        assert_safe_text(comment)
        self.dispatch.wait_for_discussion(issue_input)
        github.ensure_label(self.labels.ready_for_agent)
        github.ensure_label(self.labels.needs_discussion)
        github.add_label(issue_input.issue_number, self.labels.needs_discussion)
        github.comment_once(
            issue_input.issue_number,
            marker="<!-- hatchet-issue-agent-discussion -->",
            comment=comment,
        )

    def _register_pull_wait(self, github: Repository, pull: PullInput) -> None:
        self.state.put(
            f"pull:v1:{pull.repository}:{pull.issue_number}",
            pull.model_dump(mode="json"),
        )
        logger.info(
            "Registering pull-request review wait: repository=%s issue=%d "
            "pull_request=%d url=%s branch=%s",
            pull.repository,
            pull.issue_number,
            pull.pull_number,
            pull.pull_url,
            pull.branch,
        )
        github.ensure_label(self.labels.pr_open)
        github.add_label(pull.issue_number, self.labels.pr_open)
        github.comment_once(
            pull.issue_number,
            marker=f"<!-- hatchet-issue-agent-pr-{pull.pull_number} -->",
            comment=f"Draft pull request created: {pull.pull_url}",
        )
        self.dispatch.wait_for_review(
            pull,
        )

    def _mark_pull_reviewed(self, github: Repository, pull_number: int) -> None:
        github.ensure_label(self.labels.pr_reviewed)
        github.add_label(pull_number, self.labels.pr_reviewed)

    def _generate_code_patch(
        self,
        *,
        github: Repository,
        title: str,
        body: str,
        comments: list[str],
        files: dict[str, str],
        existing_paths: set[str],
        review_feedback: list[str] | None = None,
        branch: str | None = None,
    ) -> PatchPlan:
        assert_safe_text(title, body, *comments, *(review_feedback or []))
        for path, content in files.items():
            if path not in existing_paths:
                raise ModelError("Source file is outside the approved inventory")
            assert_safe_text(path, content)
        agent = self.coding_agent()
        archive = (
            github.get_repository_archive(branch)
            if agent.capabilities.context == "repository-archive"
            else None
        )
        return agent.generate(
            CodingRequest(
                title=title,
                body=body,
                comments=comments,
                files=files,
                existing_paths=existing_paths,
                archive=archive,
                review_feedback=review_feedback or [],
                base_commit=branch or "",
            )
        )

    def discussion_status(self, input: IssueInput) -> str:
        self.validate_repository(input.repository)
        issue = self.repositories(input.repository).get_issue(input.issue_number)
        if issue.get("state") != "open":
            return "issue-not-open"
        return (
            "ready" if self.labels.ready_for_agent in labels_for(issue) else "waiting"
        )

    def pull_wait_status(self, input: PullInput) -> str:
        self.validate_repository(input.repository)
        github = self.repositories(input.repository)
        pull = github.get_pull_request(input.pull_number)
        if pull.get("merged"):
            return "merged"
        if pull.get("state") == "closed":
            self.state.put(
                f"pull-lifecycle:v1:{input.repository}:{input.pull_number}",
                {"status": "closed-unmerged", "issue_number": input.issue_number},
            )
            return "closed-unmerged"
        return "waiting"

    def handle_event(self, event) -> EventResult:
        self.validate_repository(event.repository)
        if event.kind == "work-item-ready" and event.issue_number:
            self.dispatch.enqueue_issue(
                IssueInput(
                    repository=event.repository,
                    issue_number=event.issue_number,
                    factory_run_id=event.correlation_id,
                )
            )
            return EventResult(status="queued", issue_number=event.issue_number)
        if event.kind == "change-feedback-received" and event.pull_number:
            github = self.repositories(event.repository)
            pull = _pull_input_from_api(
                github,
                github.get_pull_request(event.pull_number),
                pr_open_label=self.labels.pr_open,
            )
            if pull:
                pull.factory_run_id = event.correlation_id
                self.dispatch.enqueue_feedback(pull)
                return EventResult(
                    status="queued",
                    issue_number=pull.issue_number,
                    pull_number=pull.pull_number,
                )
        return EventResult(
            status="observed" if event.kind == "change-state-observed" else "ignored",
            pull_number=event.pull_number,
        )
