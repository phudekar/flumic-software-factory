"""Runtime requests derive all scope from an authenticated assignment lease."""

from typing import Any, Literal
from fastapi import HTTPException, Request
from pydantic import Field
from app.worker_pools import LeaseProof, authenticate, require_lease, resolve_context
from app.database import StageAttempt, RuntimeJob
from app.projects import ProjectScope, require_owned
from app.workflow_state import transition_lock
from factory.workflows.references import json_bytes
from factory.workflows.project_dispatch import FollowupRequest
from factory.workflows.redaction import secret_scope, redact


class StateCall(LeaseProof):
    operation: Literal["get", "put", "delete"]
    key: str = Field(min_length=1, max_length=512)
    value: Any = None


class ResourceCall(LeaseProof):
    resource: str = Field(min_length=1, max_length=512)


class FollowupCall(LeaseProof):
    request: FollowupRequest


class JobCall(LeaseProof):
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def authorize(db, request, identifier, proof, settings, *, terminal=False):
    transition_lock(db)
    worker = authenticate(db, request)
    row = require_lease(db, worker, identifier, proof, settings, terminal=terminal)
    attempt = require_owned(
        db, ProjectScope(row.project_id), StageAttempt, row.execution_id
    )
    if (
        attempt.assignment_id != row.id
        or attempt.run_id != row.run_id
        or attempt.stage_id != row.stage_id
    ):
        raise HTTPException(409, "Assignment authority does not match execution")
    return worker, row


def install(app, sessions, settings, runtime):
    @app.post(
        "/api/v1/shared-worker/assignments/{identifier}/followups", status_code=202
    )
    def followup(identifier: str, body: FollowupCall, request: Request):
        from app.followups import accept

        with sessions.begin() as db:
            worker, row = authorize(db, request, identifier, body.lease_token, settings)
            if row.executor != "builtin@1":
                raise HTTPException(409, "Follow-ups require a builtin action")
            context = resolve_context(
                db,
                ProjectScope(row.project_id),
                row.execution_id,
                worker.id,
                settings,
                lease_proof=body.lease_token,
            )
            return accept(db, row, context, body.request, settings)

    @app.post("/api/v1/shared-worker/assignments/{identifier}/state")
    def state(identifier: str, body: StateCall, request: Request):
        with sessions.begin() as db:
            worker, row = authorize(db, request, identifier, body.lease_token, settings)
            if row.executor != "builtin@1":
                raise HTTPException(409, "State access requires a builtin action")
            context = resolve_context(
                db,
                ProjectScope(row.project_id),
                row.execution_id,
                worker.id,
                settings,
                lease_proof=body.lease_token,
            )
            with secret_scope(context.credentials.values()):
                value = redact(body.value)
                json_bytes(value)
                result = runtime.store._state(
                    ProjectScope(row.project_id),
                    row.repository_id,
                    body.key,
                    body.operation,
                    value,
                    db=db,
                )
                return {"value": result}

    @app.post("/api/v1/shared-worker/assignments/{identifier}/resource")
    def resource(identifier: str, body: ResourceCall, request: Request):
        with sessions.begin() as db:
            _, row = authorize(db, request, identifier, body.lease_token, settings)
            if row.executor != "builtin@1":
                raise HTTPException(409, "Resource access requires a builtin action")
            runtime.store.claim_resource(
                ProjectScope(row.project_id), row.execution_id, body.resource, db=db
            )
            return {"accepted": True}

    @app.post("/api/v1/shared-worker/assignments/{identifier}/jobs", status_code=202)
    def submit(identifier: str, body: JobCall, request: Request):
        with sessions.begin() as db:
            _, row = authorize(db, request, identifier, body.lease_token, settings)
            scope = ProjectScope(row.project_id)
        return runtime.broker.submit(scope, row.id, body.spec_digest).model_dump()

    @app.post("/api/v1/shared-worker/assignments/{identifier}/jobs/{job_id}/status")
    def status(identifier: str, job_id: str, body: LeaseProof, request: Request):
        with sessions.begin() as db:
            _, row = authorize(
                db, request, identifier, body.lease_token, settings, terminal=True
            )
            job = require_owned(db, ProjectScope(row.project_id), RuntimeJob, job_id)
            if job.assignment_id != row.id or job.execution_id != row.execution_id:
                raise HTTPException(404, "Job does not belong to assignment")
            return runtime.broker._receipt(job).model_dump()
