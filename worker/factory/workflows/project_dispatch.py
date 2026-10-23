"""Immutable project follow-up intents; transport owns durable acceptance/dedup.

No global engine/repository fallback. The receiver must authenticate the source
assignment and compare this request against its stored pinned bindings (#11).
"""

import hashlib
import json
from typing import Literal
from pydantic import Field
from factory.workflows.contracts import Contract
from factory.domain.models import IssueInput, PullInput
from factory.application.telemetry import capture_trace
from factory.workflows.references import json_bytes


class WorkflowTarget(Contract):
    snapshot_id: str = Field(min_length=1, max_length=120)
    workflow_revision_id: str = Field(min_length=1, max_length=120)


class ProjectBindings(Contract):
    project_id: str = Field(min_length=1, max_length=120)
    repository_id: str = Field(min_length=1, max_length=120)
    issue: WorkflowTarget
    feedback: WorkflowTarget
    verify: WorkflowTarget


class FollowupRequest(Contract):
    project_id: str = Field(min_length=1, max_length=120)
    repository_id: str = Field(min_length=1, max_length=120)
    source_execution_id: str = Field(min_length=1, max_length=120)
    kind: Literal["issue", "discussion", "review", "feedback", "verify"]
    target: WorkflowTarget
    command: dict
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectDispatch:
    def __init__(self, context, bindings, transport):
        bindings = ProjectBindings.model_validate(bindings)
        if (bindings.project_id, bindings.repository_id) != (
            context.project_id,
            context.repository_id,
        ):
            raise ValueError("Follow-up bindings do not match action context")
        self.context, self.bindings, self.transport = context, bindings, transport

    def _request(self, kind, command):
        context = self.context
        if command.repository != context.settings.repository:
            raise ValueError("Follow-up command crosses repository scope")
        expected = IssueInput if kind in {"issue", "discussion"} else PullInput
        if not isinstance(command, expected):
            raise ValueError("Invalid follow-up command type")
        target = {
            "issue": self.bindings.issue,
            "discussion": self.bindings.issue,
            "review": self.bindings.verify,
            "feedback": self.bindings.feedback,
            "verify": self.bindings.verify,
        }[kind]
        value = command.model_dump(mode="json")
        value["factory_run_id"] = context.factory_run_id or context.run_id
        value["trace_context"] = (
            capture_trace()
            or dict(command.trace_context)
            or dict(context.trace_context)
        )
        if len(json_bytes(value)) > 65536:
            raise ValueError("Follow-up command exceeds 64 KiB")
        identity = {key: item for key, item in value.items() if key != "trace_context"}
        encoded = json.dumps(
            [
                context.project_id,
                context.repository_id,
                context.run_id,
                context.stage_id,
                kind,
                target.model_dump(),
                identity,
            ],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        request = FollowupRequest(
            project_id=context.project_id,
            repository_id=context.repository_id,
            source_execution_id=context.execution_id,
            kind=kind,
            target=target,
            command=value,
            idempotency_key=hashlib.sha256(encoded).hexdigest(),
        )
        accepted = self.transport.submit_followup(context, request)
        if type(accepted) is not bool:
            raise ValueError("Follow-up transport must confirm durable acceptance")
        return accepted

    def enqueue_issue(self, issue):
        return self._request("issue", issue)

    def wait_for_discussion(self, issue):
        self._request("discussion", issue)

    def wait_for_review(self, pull):
        self._request("review", pull)

    def enqueue_feedback(self, pull):
        self._request("feedback", pull)

    def verify_merge(self, pull):
        self._request("verify", pull)
