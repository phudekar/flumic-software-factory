"""GitLab wire signals; remote IDs, paths and event kinds remain explicit."""

import hashlib
import json
import re
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from factory.domain.events import FactoryEvent
from factory.domain.repositories import RepositoryLocation

PositiveId = Annotated[StrictInt, Field(gt=0, le=2**63 - 1)]


class GitLabProject(BaseModel):
    id: PositiveId
    path_with_namespace: str = Field(min_length=3, max_length=1125)


class GitLabWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")
    object_kind: str = Field(max_length=40, pattern=r"^[a-z_]+$")
    project: GitLabProject
    object_attributes: dict[str, Any] = Field(default_factory=dict)
    merge_request: dict[str, Any] | None = None


class GitLabEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1, max_length=200)
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: PositiveId
    repository: str
    object_kind: Literal["issue", "merge_request", "note", "ignored"]
    action: str = Field(max_length=100)
    issue_number: PositiveId | None = None
    pull_number: PositiveId | None = None
    note_id: PositiveId | None = None
    system: bool = False

    def assert_repository(self, path, project_id):
        if self.repository != path or self.project_id != project_id:
            raise ValueError(
                "GitLab event does not match configured repository identity"
            )

    def as_factory_event(self):
        kind = "ignored"
        if self.object_kind == "issue" and self.action in {"open", "reopen", "update"}:
            kind = "work-item-ready"
        elif self.object_kind == "merge_request":
            kind = "change-state-observed"
        elif (
            self.object_kind == "note"
            and self.pull_number is not None
            and not self.system
        ):
            kind = "change-feedback-received"
        return FactoryEvent(
            event_id=self.event_id,
            kind=kind,
            repository=self.repository,
            issue_number=self.issue_number,
            pull_number=self.pull_number,
        )


def _positive(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError("GitLab event requires a positive IID/ID")
    return value


def normalize_webhook(payload: GitLabWebhook, *, delivery_id=None):
    path = payload.project.path_with_namespace
    owner, _, name = path.rpartition("/")
    location = RepositoryLocation(
        provider="gitlab", host="gitlab.com", owner=owner, name=name
    )
    digest = hashlib.sha256(
        json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    identifier = delivery_id or digest
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,200}", identifier
    ):
        raise ValueError("Invalid GitLab delivery identity")
    attributes = payload.object_attributes
    action = attributes.get(
        "action", "comment" if payload.object_kind == "note" else ""
    )
    if not isinstance(action, str) or len(action) > 100:
        raise ValueError("Invalid GitLab event action")
    kind = (
        payload.object_kind
        if payload.object_kind in {"issue", "merge_request", "note"}
        else "ignored"
    )
    issue = pull = note = None
    system = False
    if kind == "issue":
        issue = _positive(attributes.get("iid"))
    elif kind == "merge_request":
        pull = _positive(attributes.get("iid"))
    elif kind == "note":
        if (
            attributes.get("noteable_type") == "MergeRequest"
            and payload.merge_request is not None
        ):
            pull = _positive(payload.merge_request.get("iid"))
            note = _positive(attributes.get("id"))
            if type(attributes.get("system", False)) is not bool:
                raise ValueError("Invalid GitLab note system flag")
            system = attributes.get("system", False)
        else:
            kind = "ignored"
    return GitLabEvent(
        event_id=identifier,
        payload_digest=digest,
        project_id=payload.project.id,
        repository=location.path,
        object_kind=kind,
        action=action,
        issue_number=issue,
        pull_number=pull,
        note_id=note,
        system=system,
    )
