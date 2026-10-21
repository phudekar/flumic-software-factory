from __future__ import annotations

from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field

MAX_PATCH_FILES_PER_ITERATION = 5
MAX_PATCH_ITERATIONS = 10
MAX_PATCH_FILES = MAX_PATCH_ITERATIONS * MAX_PATCH_FILES_PER_ITERATION


class AgentError(RuntimeError):
    pass


class TriagePlan(BaseModel):
    needs_discussion: bool
    questions: list[str] = Field(default_factory=list, max_length=5)
    source_paths: list[str] = Field(default_factory=list, max_length=5)


class FileChange(BaseModel):
    path: str
    content: str


class PatchPlan(BaseModel):
    pull_title: str = Field(min_length=1, max_length=120)
    pull_body: str = Field(min_length=1, max_length=4_000)
    is_complete: bool
    progress: str = Field(max_length=1_000)
    changes: list[FileChange] = Field(max_length=100)


class PatchIteration(BaseModel):
    pull_title: str = Field(min_length=1, max_length=120)
    pull_body: str = Field(min_length=1, max_length=4_000)
    is_complete: bool
    progress: str = Field(max_length=1_000)
    changes: list[FileChange] = Field(max_length=MAX_PATCH_FILES_PER_ITERATION)


class ReviewFinding(BaseModel):
    kind: Literal["bug", "suggestion"]
    severity: str = Field(pattern="^(critical|high|medium|low)$")
    path: str
    line: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1, max_length=1_000)


class PullRequestReview(BaseModel):
    findings: list[ReviewFinding] = Field(max_length=10)


class PlannedSubtask(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=5)


class IssueWorkPlan(BaseModel):
    tasks: list[PlannedSubtask] = Field(min_length=1, max_length=6)


ModelError = AgentError  # Compatibility name for existing adapters.


class RepositoryError(RuntimeError):
    pass


class StaleRevisionError(RepositoryError):
    pass


class IssueInput(BaseModel):
    trace_context: dict[str, str] = Field(default_factory=dict)
    factory_run_id: str = Field(default_factory=lambda: str(uuid4()))
    repository: str
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


class PullInput(BaseModel):
    trace_context: dict[str, str] = Field(default_factory=dict)
    factory_run_id: str = Field(default_factory=lambda: str(uuid4()))
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
