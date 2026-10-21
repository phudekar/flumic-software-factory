"""GitHub wire models and translation into factory events."""

from __future__ import annotations
import hashlib
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from factory.domain.events import FactoryEvent


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


def normalize_webhook(
    payload: GitHubWebhook, source: str, *, ready_label="ready-for-agent"
) -> FactoryEvent:
    issue_number = payload.issue.number if payload.issue else None
    pull_number = None
    kind = "ignored"
    if (
        source == "issues"
        and payload.issue is not None
        and payload.issue.pull_request is None
    ):
        if payload.action in {"opened", "reopened"} or (
            payload.action == "labeled"
            and payload.label
            and payload.label.get("name") == ready_label
        ):
            kind = "work-item-ready"
    elif source == "feedback" and payload.action in {"created", "submitted"}:
        pull = payload.pull_request or (
            payload.issue.pull_request if payload.issue else None
        )
        if pull:
            pull_number = pull.get("number") or issue_number
            if isinstance(pull_number, int):
                kind = "change-feedback-received"
    elif source == "pull_request" and payload.pull_request:
        pull_number = payload.pull_request.get("number")
        if isinstance(pull_number, int):
            kind = "change-state-observed"
    # Hatchet's current webhook payload has no required delivery header. Use a
    # content digest if the ingress did not supply an explicit delivery ID.
    event_id = str(
        getattr(payload, "delivery_id", "")
        or hashlib.sha256((source + payload.model_dump_json()).encode()).hexdigest()
    )
    return FactoryEvent(
        event_id=event_id,
        kind=kind,
        repository=payload.repository.full_name,
        issue_number=issue_number,
        pull_number=pull_number,
    )
