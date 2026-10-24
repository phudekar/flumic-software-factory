"""Assignment-authoritative, idempotent and bounded project history ingestion."""

import hashlib
import json
import hmac
import math
import time
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.database import (
    Assignment,
    StageAttempt,
    WorkflowRun,
    Event,
    Conversation,
    ActionContextSnapshot,
)
from app.projects import ProjectScope, require_owned
from app.project_context import decrypt, encrypt
from app.workflow_definitions import read_definition_pin
from app.workflow_state import transition_lock
from app.worker_pools import authenticate, lease_token, LeaseProof
from factory.application.telemetry import ModelUsage
from factory.workflows.redaction import secret_scope
from factory.workflows.references import json_bytes
from factory.adapters.shared_history import event_payload

MAX_BATCH = 524288


class HistoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    kind: Literal["attempt.started", "attempt.completed", "model.completed", "log"]
    occurred_at: float = Field(ge=0, allow_inf_nan=False)
    run_id: str | None = Field(default=None, max_length=120)
    attempt_id: str | None = Field(default=None, max_length=120)
    stage: str | None = Field(default=None, max_length=100)
    attribution: dict = Field(default_factory=dict)
    payload: dict = Field(default_factory=dict)


class HistoryBatch(LeaseProof):
    events: list[HistoryEvent] = Field(min_length=1, max_length=50)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")


def attribution(db, assignment, settings, *, job_id=None):
    scope = ProjectScope(assignment.project_id)
    attempt = require_owned(db, scope, StageAttempt, assignment.execution_id)
    run = require_owned(db, scope, WorkflowRun, assignment.run_id)
    if attempt.assignment_id != assignment.id or attempt.run_id != run.id:
        raise HTTPException(409, "Assignment history authority does not match")
    pin = read_definition_pin(db, scope, run.snapshot_id, settings)
    return {
        "project_id": assignment.project_id,
        "worker_id": assignment.worker_id,
        "repository_id": assignment.repository_id,
        "definition_id": pin["definition_id"],
        "workflow_revision_id": pin["workflow_revision_id"],
        "run_id": run.id,
        "stage_id": attempt.stage_id,
        "execution_id": attempt.id,
        "assignment_id": assignment.id,
        "attempt": attempt.generation,
        "executor": assignment.executor,
        "job_id": job_id,
    }


def pinned_context(db, assignment, settings):
    pin = require_owned(
        db,
        ProjectScope(assignment.project_id),
        ActionContextSnapshot,
        assignment.context_id or "",
    )
    if pin.execution_id != assignment.execution_id:
        raise HTTPException(409, "History context does not match assignment")
    document = decrypt(pin.ciphertext, settings)
    mode = json.loads(document["context"]["settings_json"]).get(
        "conversation_capture", "redacted"
    )
    if mode not in {"redacted", "metadata_only", "disabled"}:
        raise HTTPException(409, "Pinned capture policy is unavailable")
    return document, mode


def validate_usage(payload):
    usage = {
        key: payload[key] for key in ModelUsage.__dataclass_fields__ if key in payload
    }
    ModelUsage(**usage)
    for key in ("estimated_usd", "reported_cost_usd"):
        amount = payload.get(key)
        if amount is not None and (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < 0
        ):
            raise ValueError("Invalid monetary usage")
    for key in ("call_id", "parent_call_id", "provider", "model", "agent"):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, str) or not 1 <= len(value) <= 120
        ):
            raise ValueError("Invalid bounded model metadata")
    if "accounting_leaf" in payload and type(payload["accounting_leaf"]) is not bool:
        raise ValueError("Invalid accounting leaf")


def insert_event(db, settings, worker_id, metadata, event, payload):
    payload = dict(payload)
    payload.pop("conversation_available", None)
    if event.kind == "model.completed":
        validate_usage(payload)
    canonical = {
        "assignment_id": metadata["assignment_id"],
        "kind": event.kind,
        "occurred_at": event.occurred_at,
        "payload": payload,
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    call_id = (
        (payload.get("call_id") or event.id)
        if event.kind == "model.completed"
        else None
    )
    prior = db.get(Event, event.id)
    if prior is None and call_id:
        prior = db.scalar(
            select(Event).where(
                Event.project_id == metadata["project_id"],
                Event.execution_id == metadata["execution_id"],
                Event.call_id == call_id,
            )
        )
    if prior is not None:
        if (
            prior.assignment_id != metadata["assignment_id"]
            or prior.ingest_digest != digest
        ):
            raise HTTPException(409, "History identity conflicts with a stored event")
        return
    payload = dict(payload)
    conversation = payload.pop("conversation", None)
    if conversation is not None:
        db.add(
            Conversation(
                id=event.id,
                project_id=metadata["project_id"],
                ciphertext=encrypt(conversation, settings),
                expires_at=time.time() + 604800,
            )
        )
        payload["conversation_available"] = True
    db.add(
        Event(
            id=event.id,
            project_id=metadata["project_id"],
            repository_id=metadata["repository_id"],
            kind=event.kind,
            worker_id=worker_id,
            run_id=metadata["run_id"],
            attempt_id=metadata["execution_id"],
            stage=metadata["stage_id"],
            occurred_at=event.occurred_at,
            payload=payload,
            **{
                key: metadata[key]
                for key in (
                    "assignment_id",
                    "execution_id",
                    "definition_id",
                    "workflow_revision_id",
                    "executor",
                    "job_id",
                )
            },
            call_id=call_id,
            ingest_digest=digest,
        )
    )
    db.flush()


def install(app, sessions, settings):
    @app.post("/api/v1/shared-worker/heartbeat", status_code=202)
    def heartbeat(body: Heartbeat, request: Request):
        with sessions.begin() as db:
            worker = authenticate(db, request)
            worker.last_heartbeat_at = time.time()
        return {"accepted": True}

    @app.post("/api/v1/shared-worker/assignments/{identifier}/events", status_code=202)
    def ingest(identifier: str, body: HistoryBatch, request: Request):
        if len(json_bytes(body.model_dump())) > MAX_BATCH:
            raise HTTPException(413, "History batch exceeds limit")
        with sessions.begin() as db:
            transition_lock(db)
            worker = authenticate(db, request)
            assignment = db.get(Assignment, identifier)
            if assignment is None or assignment.worker_id != worker.id:
                raise HTTPException(404, "Assignment not found")
            if not hmac.compare_digest(
                body.lease_token, lease_token(settings, assignment)
            ):
                raise HTTPException(409, "Invalid history assignment proof")
            if assignment.executor != "builtin@1" or assignment.started_at is None:
                raise HTTPException(
                    409, "History requires a started builtin assignment"
                )
            metadata = attribution(db, assignment, settings)
            secrets = []
            document, mode = pinned_context(db, assignment, settings)
            for version in document["grants"].values():
                from app.database import SecretVersion

                secrets.append(
                    decrypt(
                        require_owned(
                            db,
                            ProjectScope(assignment.project_id),
                            SecretVersion,
                            version,
                        ).ciphertext,
                        settings,
                    )
                )
            budget = MAX_BATCH - len(
                json_bytes({"lease_token": body.lease_token, "events": []})
            )
            for event in body.events:
                assertions = [
                    (key, value)
                    for key, value in (
                        ("run_id", event.run_id),
                        ("execution_id", event.attempt_id),
                        ("stage_id", event.stage),
                    )
                    if value is not None
                ]
                assertions.extend(event.attribution.items())
                if any(
                    key not in metadata or metadata[key] != value
                    for key, value in assertions
                ):
                    raise HTTPException(
                        409, "History attribution does not match assignment"
                    )
                protected = {
                    **metadata,
                    "attempt_id": metadata["execution_id"],
                    "stage": metadata["stage_id"],
                }
                if any(
                    key in protected and value != protected[key]
                    for key, value in event.payload.items()
                ):
                    raise HTTPException(
                        409, "Payload attribution does not match assignment"
                    )
                if {
                    "engine_workflow_run_id",
                    "engine_task_run_id",
                } & event.payload.keys():
                    raise HTTPException(
                        409, "Worker engine labels cannot establish history authority"
                    )
                try:
                    if event.kind == "model.completed" and mode == "disabled":
                        continue
                    with secret_scope(secrets):
                        payload = event_payload(event.payload)
                    if mode != "redacted":
                        payload.pop("conversation", None)
                        payload["capture_status"] = mode
                    budget -= len(
                        json_bytes({**event.model_dump(), "payload": payload})
                    )
                    if budget < 0:
                        raise HTTPException(413, "Redacted history exceeds limit")
                    insert_event(db, settings, worker.id, metadata, event, payload)
                except ValueError:
                    raise HTTPException(
                        422, "Invalid bounded history payload"
                    ) from None
        return {"accepted": len(body.events)}


def record_job_usage(db, settings, job, result):
    """Broker-confirmed declared AI usage; resource measurements are unrelated."""
    import uuid

    assignment = require_owned(
        db, ProjectScope(job.project_id), Assignment, job.assignment_id
    )
    metadata = attribution(db, assignment, settings, job_id=job.id)
    _, mode = pinned_context(db, assignment, settings)
    if mode == "disabled":
        return
    for usage in result.usage:
        identifier = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, "flumic-job-usage/" + job.id + "/" + usage.id
            )
        )
        prior = db.get(Event, identifier)
        payload = {
            **usage.model_dump(exclude={"id"}),
            "call_id": usage.id,
            "accounting_leaf": True,
            "usage_source": "external-action",
        }
        event = HistoryEvent(
            id=identifier,
            kind="model.completed",
            occurred_at=prior.occurred_at if prior else job.updated_at,
            payload=payload,
        )
        insert_event(db, settings, assignment.worker_id, metadata, event, payload)
