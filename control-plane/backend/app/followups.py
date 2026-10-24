"""Pinned follow-up acceptance and resumable waits; never caller-supplied authority."""

import json
from contextlib import contextmanager
from fastapi import HTTPException
from sqlalchemy import select, or_
from app.database import (
    ProjectFollowup,
    StageAttempt,
    Assignment,
    WorkflowRun,
    ActionContextSnapshot,
)
from app.projects import ProjectScope, require_owned
from app.project_context import encrypt, decrypt, resolve_secret
from app.workflow_state import transition_lock, TERMINAL
from factory.workflows.project_dispatch import (
    ProjectBindings,
    ProjectDispatch,
    FollowupRequest,
)
from factory.workflows.references import json_bytes
from factory.workflows.contracts import ActionContext, RunMetadata
from factory.domain.models import IssueInput, PullInput


def accept(db, row, context, request, settings):
    try:
        return _accept(db, row, context, request, settings)
    except ValueError:
        raise HTTPException(
            422, "Invalid follow-up command or pinned bindings"
        ) from None


def _accept(db, row, context, request, settings):
    from factory.workflows.actions import _pull_scope

    if len(json_bytes(request.command)) > 65536:
        raise HTTPException(422, "Follow-up command exceeds limit")
    model = IssueInput if request.kind in {"issue", "discussion"} else PullInput
    if set(request.command) - set(model.model_fields):
        raise HTTPException(422, "Unknown command fields")
    command = model.model_validate(request.command)
    RunMetadata(
        factory_run_id=command.factory_run_id, trace_context=command.trace_context
    )
    if isinstance(command, PullInput):
        if command.issue_number < 1 or command.pull_number < 1:
            raise HTTPException(422, "Positive work-item identifiers required")
        _pull_scope(command, context.settings.repository)

    class Capture:
        def submit_followup(self, context, value):
            self.value = value
            return True

    capture = Capture()
    ProjectDispatch(
        context,
        ProjectBindings.model_validate_json(context.followup_bindings_json),
        capture,
    )._request(request.kind, command)
    expected = capture.value
    if request.model_dump(exclude={"command"}) != expected.model_dump(
        exclude={"command"}
    ) or {k: v for k, v in request.command.items() if k != "trace_context"} != {
        k: v for k, v in expected.command.items() if k != "trace_context"
    }:
        raise HTTPException(409, "Follow-up does not match pinned assignment bindings")
    from factory.workflows.redaction import secret_scope, redact

    with secret_scope(context.credentials.values()):
        if any(
            redact(value) != value
            for value in command.model_dump(exclude={"trace_context"}).values()
        ):
            raise HTTPException(422, "Follow-up data contains a granted credential")
        safe_trace = redact(command.trace_context)
        RunMetadata(trace_context=safe_trace)
    existing = db.scalar(
        select(ProjectFollowup).where(
            ProjectFollowup.project_id == row.project_id,
            ProjectFollowup.request_key == expected.idempotency_key,
        )
    )
    if existing is None:
        existing = ProjectFollowup(
            project_id=row.project_id,
            source_execution_id=row.execution_id,
            source_run_id=row.run_id,
            request_key=expected.idempotency_key,
            kind=expected.kind,
            ciphertext=encrypt(
                expected.model_copy(
                    update={
                        "command": {
                            **expected.command,
                            "trace_context": safe_trace,
                        }
                    }
                ).model_dump(mode="json"),
                settings,
            ),
        )
        db.add(existing)
        db.flush()
    return {"accepted": True, "followup_id": existing.id, "state": existing.state}


def reconcile(runtime):
    with runtime.sessions() as db:
        records = list(
            db.scalars(
                select(ProjectFollowup.id)
                .join(
                    StageAttempt, ProjectFollowup.source_execution_id == StageAttempt.id
                )
                .join(WorkflowRun, ProjectFollowup.source_run_id == WorkflowRun.id)
                .where(
                    ProjectFollowup.state == "pending",
                    or_(
                        StageAttempt.state.in_(TERMINAL),
                        WorkflowRun.state.in_(["cancelling", "cancelled"]),
                    ),
                )
                .order_by(ProjectFollowup.id)
                .limit(32)
            )
        )
    for identifier in records:
        try:
            _reconcile_one(runtime, identifier)
        except (ValueError, HTTPException):
            with runtime.sessions.begin() as db:
                transition_lock(db)
                record = db.get(ProjectFollowup, identifier)
                if record.state == "pending":
                    record.state = "blocked"
                    record.error = "Pinned follow-up requires operator reconciliation"
        except Exception:
            # A transient backend failure leaves this intent retryable and does
            # not prevent independent projects from making progress.
            continue
    return bool(records)


def _reconcile_one(runtime, identifier):
    with runtime.sessions.begin() as db:
        transition_lock(db)
        record = db.get(ProjectFollowup, identifier)
        if record.state != "pending":
            return
        source = db.get(WorkflowRun, record.source_run_id)
        attempt = db.get(StageAttempt, record.source_execution_id)
        if source.state in {"cancelling", "cancelled"} or attempt.state == "cancelled":
            record.state = "cancelled"
            return
        if record.kind in {"discussion", "review"}:
            runtime.enqueue(
                db,
                ProjectScope(record.project_id),
                "wait",
                record.id,
                {"wait_id": record.id},
            )
            record.state = "waiting"
            return
    _launch(runtime, identifier)


def _launch(runtime, identifier):
    with runtime.sessions.begin() as db:
        transition_lock(db)
        record = db.get(ProjectFollowup, identifier)
        scope = ProjectScope(record.project_id)
        source = require_owned(db, scope, WorkflowRun, record.source_run_id)
        attempt = require_owned(db, scope, StageAttempt, record.source_execution_id)
        if source.state in {"cancelling", "cancelled"} or attempt.state == "cancelled":
            record.state = "cancelled"
            return False
        if record.result_run_id is not None:
            return True
        request = FollowupRequest.model_validate(
            decrypt(record.ciphertext, runtime.settings)
        )
        child = runtime.store.start_pinned_run(
            scope,
            request.target.snapshot_id,
            request.command,
            key="followup-" + identifier,
            db=db,
        )
        runtime.enqueue(
            db,
            scope,
            "run",
            child.id,
            {"project_id": scope.project_id, "run_id": child.id},
        )
        record.result_run_id, record.state = child.id, "completed"
    return True


def _waiting(runtime, identifier):
    with runtime.sessions.begin() as db:
        record = db.get(ProjectFollowup, identifier)
        if record is None or record.kind not in {"discussion", "review"}:
            raise ValueError("Unknown project wait")
        source = require_owned(
            db, ProjectScope(record.project_id), WorkflowRun, record.source_run_id
        )
        if source.state in {"cancelling", "cancelled"}:
            record.state = "cancelled"
        return record


def check(runtime, identifier):
    try:
        return _check(runtime, identifier)
    except (ValueError, HTTPException):
        with runtime.sessions.begin() as db:
            transition_lock(db)
            record = db.get(ProjectFollowup, identifier)
            if record is not None and record.state == "waiting":
                record.state = "blocked"
                record.error = "Pinned wait requires operator reconciliation"
        raise ValueError("Pinned wait requires operator reconciliation") from None


def _check(runtime, identifier):
    from factory.workflows.project_waits import WaitCheck, check_project_wait

    record = _waiting(runtime, identifier)
    if record.state == "completed":
        return WaitCheck(
            kind=record.kind, state="ready" if record.kind == "discussion" else "merged"
        )
    if record.state in {"cancelled", "issue-not-open", "closed-unmerged"}:
        return WaitCheck(kind=record.kind, state=record.state)
    if record.state == "blocked":
        raise ValueError("Pinned wait requires operator reconciliation")
    with runtime.sessions() as db:
        scope = ProjectScope(record.project_id)
        request = FollowupRequest.model_validate(
            decrypt(record.ciphertext, runtime.settings)
        )
        attempt = require_owned(db, scope, StageAttempt, record.source_execution_id)
        assignment = require_owned(db, scope, Assignment, attempt.assignment_id)
        pin = require_owned(db, scope, ActionContextSnapshot, assignment.context_id)
        document = decrypt(pin.ciphertext, runtime.settings)
        context = ActionContext.model_validate_json(json.dumps(document["context"]))
        if context.assignment_id != assignment.id or context.execution_id != attempt.id:
            raise ValueError("Stored wait context does not match authority")
        github = document["grants"].get("github")
        if not github:
            raise ValueError("Wait requires a pinned GitHub grant")
        credential = resolve_secret(
            db, scope, github, "github", runtime.settings, context.repository_id
        )
        context = context.with_credentials({"github": credential})
    model = IssueInput if record.kind == "discussion" else PullInput
    status = check_project_wait(
        context,
        record.kind,
        model.model_validate(request.command),
        dispatch=None,
        state=WaitState(runtime, context),
        service_factory=wait_service,
    )
    if status.state in {"issue-not-open", "closed-unmerged"}:
        with runtime.sessions.begin() as db:
            transition_lock(db)
            saved = db.get(ProjectFollowup, identifier)
            if saved.state == "waiting":
                saved.state = status.state
    return status


def resume(runtime, identifier):
    record = _waiting(runtime, identifier)
    if record.state == "cancelled":
        return False
    if record.state == "completed":
        return True
    if record.state != "waiting":
        return False
    return _launch(runtime, identifier)


@contextmanager
def wait_service(context, **kwargs):
    """Read-only GitHub checks never import or instantiate model adapters."""
    from github_client import GitHubClient
    from factory.application.service import FactoryService
    from factory.domain.policy import ExecutionPolicy
    from factory.adapters.telemetry import LoggingObserver
    from factory.workflows.redaction import secret_scope

    def unavailable():
        raise ValueError("Agents are unavailable in wait checks")

    settings = context.settings

    def repository(name):
        if name != settings.repository:
            raise ValueError("Repository crosses wait scope")
        return GitHubClient(
            name,
            token=context.credential("github"),
            max_file_bytes=settings.max_file_bytes,
        )

    with secret_scope(context.credentials.values()):
        yield FactoryService(
            repository=settings.repository,
            repositories=repository,
            planner=unavailable,
            reviewer=unavailable,
            coding_agent=unavailable,
            dispatch=None,
            state=kwargs.get("state"),
            observer=LoggingObserver(),
            policy=ExecutionPolicy(
                path_scope=settings.path_policy,
                max_files=settings.max_patch_iterations
                * settings.max_files_per_iteration,
                max_file_bytes=settings.max_file_bytes,
            ),
            labels=settings.labels,
            task_settings=settings.task,
        )


class WaitState:
    def __init__(self, runtime, context):
        self.store, self.scope, self.repository = (
            runtime.store,
            ProjectScope(context.project_id),
            context.repository_id,
        )

    def get(self, key):
        return self.store.get_state(self.scope, self.repository, key)

    def put(self, key, value):
        return self.store.put_state(self.scope, self.repository, key, value)

    def delete(self, key):
        return self.store.delete_state(self.scope, self.repository, key)
