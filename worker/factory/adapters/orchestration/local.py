"""Synchronous one-shot runner. Wait requests are returned, never held in memory as timers."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from factory.domain.models import IssueInput, PullInput
from factory.adapters.telemetry import correlation


@dataclass
class LocalDispatch:
    requests: list[tuple[str, IssueInput | PullInput]] = field(default_factory=list)

    def enqueue_issue(self, issue):
        self.requests.append(("issue", issue))

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


def main(settings):
    from factory.bootstrap import build_service

    parser = argparse.ArgumentParser(
        description="Run one software-factory issue without Hatchet. This can create GitHub issues and PRs."
    )
    parser.add_argument("issue_number", type=int)
    args = parser.parse_args()
    dispatch = LocalDispatch()
    service = build_service(settings, dispatch)
    result = LocalRunner(service).run_issue(
        IssueInput(repository=settings.repository, issue_number=args.issue_number)
    )
    print(
        json.dumps(
            {
                "result": result.model_dump(mode="json"),
                "pending_requests": [
                    {"kind": kind, "input": command.model_dump(mode="json")}
                    for kind, command in dispatch.requests
                ],
            }
        )
    )
