"""In-memory dispatch and sequential use-case harness for domain tests only."""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.domain.models import IssueInput, PullInput
from factory.adapters.telemetry import correlation


@dataclass
class LocalDispatch:
    requests: list[tuple[str, IssueInput | PullInput]] = field(default_factory=list)

    def enqueue_issue(self, issue):
        self.requests.append(("issue", issue))
        return True

    def wait_for_discussion(self, issue):
        self.requests.append(("discussion", issue))

    def wait_for_review(self, pull):
        self.requests.append(("review", pull))

    def enqueue_feedback(self, pull):
        self.requests.append(("feedback", pull))

    def verify_merge(self, pull):
        self.requests.append(("verify", pull))


class LocalRunner:
    def __init__(self, service):
        self.service = service

    def run_issue(self, command: IssueInput):
        with correlation(command.factory_run_id):
            loaded = self.service.load_issue(command)
            planned = self.service.plan_issue(command, loaded)
            triaged = self.service.triage_issue(command, loaded, planned)
            published = self.service.generate_and_open_draft_pr(command, triaged)
            reviewed = self.service.review_opened_pull_request(command, published)
            return self.service.record_workflow_result(command, reviewed)
