"""Integration contracts owned by the application, independent of vendor SDKs."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from factory.domain.models import (
    IssueInput,
    IssueWorkPlan,
    PatchPlan,
    PullInput,
    PullRequestReview,
    TriagePlan,
)


@dataclass(frozen=True)
class CodingRequest:
    title: str
    body: str
    comments: list[str]
    files: dict[str, str]
    existing_paths: set[str]
    archive: bytes | None = None
    review_feedback: list[str] = field(default_factory=list)
    base_commit: str = ""


@dataclass(frozen=True)
class AgentCapabilities:
    context: str  # "selected-files" or "repository-archive"

    def __post_init__(self):
        if self.context not in {"selected-files", "repository-archive"}:
            raise ValueError("Unsupported coding agent context capability")


class CodingAgent(Protocol):
    capabilities: AgentCapabilities

    def generate(self, request: CodingRequest) -> PatchPlan: ...


class Planner(Protocol):
    def triage(
        self,
        *,
        title: str,
        body: str,
        comments: list[str],
        source_paths: list[str],
        discussion_resolved: bool = False,
    ) -> TriagePlan: ...
    def plan_issue(
        self, *, title: str, body: str, comments: list[str], source_paths: list[str]
    ) -> IssueWorkPlan: ...


class ChangeReviewer(Protocol):
    def review_pull_request(
        self,
        *,
        issue_title: str,
        issue_body: str,
        issue_comments: list[str],
        pull_title: str,
        pull_body: str,
        diff: list[dict[str, str]],
        current_files: dict[str, str],
    ) -> PullRequestReview: ...


class Repository(Protocol):
    """Initial issue/source-host contract; transport dictionaries stay at this boundary."""

    repository: str

    def get_issue(self, number: int) -> dict[str, Any]: ...
    def get_issue_comments(self, number: int) -> list[str]: ...
    def list_open_issues(self) -> list[dict[str, Any]]: ...
    def find_issues_by_markers(
        self, markers: set[str]
    ) -> dict[str, dict[str, int]]: ...
    def create_issue(
        self, *, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]: ...
    def list_sub_issue_ids(self, parent_number: int) -> set[int]: ...
    def add_sub_issue(self, parent_number: int, sub_issue_id: int) -> None: ...
    def list_source_paths(self, branch: str | None = None) -> list[str]: ...
    def list_repository_paths(self, branch: str | None = None) -> set[str]: ...
    def resolve_revision(self, ref: str | None = None) -> str: ...
    def get_repository_archive(self, ref: str | None = None) -> bytes: ...
    def get_source_files(
        self, paths: list[str], branch: str | None = None
    ) -> dict[str, str]: ...
    def list_open_pull_requests(self) -> list[dict[str, Any]]: ...
    def get_pull_comments(self, number: int) -> list[str]: ...
    def comment_pull_once(self, number: int, *, marker: str, comment: str) -> None: ...
    def add_pull_label(self, number: int, label: str) -> None: ...
    def get_pull_feedback(self, pull_number: int) -> list[dict[str, Any]]: ...
    def pull_feedback_cursor(self, pull_number: int, branch: str) -> dict[str, int]: ...
    def record_pull_feedback(
        self, pull_number: int, cursor: dict[str, int], summary: str
    ) -> None: ...
    def ensure_label(self, label: str) -> None: ...
    def add_label(self, number: int, label: str) -> None: ...
    def remove_label(self, number: int, label: str) -> None: ...
    def comment_once(self, number: int, *, marker: str, comment: str) -> None: ...
    def find_issue_pull_request(self, issue_number: int) -> dict[str, Any] | None: ...
    def create_draft_pull_request(
        self,
        *,
        issue_number: int,
        title: str,
        body: str,
        branch: str,
        changes: list,
        expected_base_sha: str | None = None,
    ) -> dict[str, Any]: ...
    def update_pull_request_branch(
        self,
        *,
        branch: str,
        issue_number: int,
        feedback_cursor: dict[str, int],
        changes: list,
        expected_base_sha: str | None = None,
    ) -> str: ...
    def get_pull_request(self, number: int) -> dict[str, Any]: ...
    def get_pull_request_files(self, number: int) -> list[dict[str, str]]: ...
    def verify_commit_checks(self, sha: str, *, ref: str | None = None) -> None: ...
    def close_issue(self, number: int) -> None: ...


class WorkflowDispatch(Protocol):
    """Factory-specific requests; durable timing and execution remain engine-owned."""

    def enqueue_issue(self, issue: IssueInput) -> bool:
        """True when submitted; False when an issue run is already active."""
        ...

    def wait_for_discussion(self, issue: IssueInput) -> None: ...
    def wait_for_review(self, pull: PullInput) -> None: ...
    def enqueue_feedback(self, pull: PullInput) -> None: ...
    def verify_merge(self, pull: PullInput) -> None: ...


class FactoryStateStore(Protocol):
    def claim(self, resource: str, operation: str) -> AbstractContextManager[None]: ...
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, value: dict[str, Any]) -> None: ...
    def delete(self, key: str) -> None: ...


class OperationObserver(Protocol):
    def observe(
        self, stage: str, attributes: dict[str, Any]
    ) -> AbstractContextManager[None]: ...


RepositoryFactory = Callable[[str], Repository]
